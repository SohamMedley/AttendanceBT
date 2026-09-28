"""
Google Cloud Firestore backend (REST transport, no SDK required).

Why REST instead of ``firebase-admin``?
---------------------------------------
Firestore's REST API is a plain JSON/HTTPS interface. Using it directly means:

* the project still runs on a lab PC with only the standard library installed;
* you can *see* the exact HTTP calls in ``/api/firestore/probe`` -- which is far
  more useful in a viva than "the library does it";
* if ``firebase-admin`` *is* installed we use it for authentication, so you get
  the convenience without the hard dependency.

Authentication
--------------
A Firebase service account gives us a private key; we sign a JWT assertion and
exchange it for an OAuth 2.0 access token (see ``_rs256.py``). Tokens are cached
in memory until 60 seconds before expiry.

Setup
-----
1. Firebase console -> Project settings -> Service accounts -> *Generate new
   private key*. Save the JSON as ``firebase-service-account.json`` in the
   project root (it is git-ignored -- never commit it).
2. Put your project id in ``config.json`` or the ``FIREBASE_PROJECT_ID`` env var.
3. Run ``python manage.py firebase-check``.

Data representation
-------------------
All timestamps are stored as Firestore ``doubleValue`` (epoch seconds). This
keeps the wire format identical to the local JSON backend, so records can be
moved between the two backends without conversion.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

from . import _rs256
from .base import (
    ALL_COLLECTIONS,
    SCHEMA_VERSION,
    Store,
    StoreError,
    matches_filters,
    sort_documents,
)

API_ROOT = "https://firestore.googleapis.com/v1"
PAGE_SIZE = 300
TOKEN_SAFETY_MARGIN = 60  # refresh a minute before real expiry


# --------------------------------------------------------------------------
# Firestore <-> Python value conversion
# --------------------------------------------------------------------------
def to_firestore_value(value: Any) -> dict[str, Any]:
    """Convert a Python value into Firestore's tagged ``Value`` JSON."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):  # must precede int -- bool is a subclass of int
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [to_firestore_value(item) for item in value]}}
    if isinstance(value, Mapping):
        return {
            "mapValue": {
                "fields": {str(k): to_firestore_value(v) for k, v in value.items()}
            }
        }
    return {"stringValue": str(value)}  # last resort: stringify


def from_firestore_value(value: Mapping[str, Any]) -> Any:
    """Inverse of :func:`to_firestore_value`."""
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "timestampValue" in value:
        # Kept for records written by other tools.
        return value["timestampValue"]
    if "arrayValue" in value:
        return [from_firestore_value(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            key: from_firestore_value(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    if "referenceValue" in value:
        return value["referenceValue"]
    return None


def to_firestore_document(data: Mapping[str, Any]) -> dict[str, Any]:
    return {"fields": {str(k): to_firestore_value(v) for k, v in data.items()}}


def from_firestore_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a Firestore document into a flat Python dict (id included)."""
    result = {
        key: from_firestore_value(value)
        for key, value in document.get("fields", {}).items()
    }
    name = document.get("name", "")
    if name:
        result["_id"] = name.rsplit("/", 1)[-1]
        result["_path"] = name.split("/documents/", 1)[-1]
    if document.get("updateTime"):
        result["_updated_at_server"] = document["updateTime"]
    return result


def _sanitise_document_id(doc_id: str) -> str:
    """Firestore rejects ids containing '/' or longer than 1500 bytes."""
    cleaned = str(doc_id).replace("/", "_").strip()
    if cleaned in ("", ".", ".."):
        raise StoreError(f"Invalid Firestore document id: {doc_id!r}")
    return cleaned[:1500]


class FirestoreStore(Store):
    """Firestore document store over the REST API."""

    name = "firestore"

    def __init__(
        self,
        project_id: str,
        *,
        credentials: Mapping[str, Any] | None = None,
        credentials_path: str | None = None,
        database: str = "(default)",
        timeout: int = 45,
        prefix: str = "",
    ) -> None:
        super().__init__(project_id=project_id)
        self.project_id = project_id
        self.database = database
        self.timeout = timeout
        self.prefix = prefix.strip("/")
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._lock = threading.RLock()
        self.credentials: dict[str, Any] = {}

        emulator = _emulator_host()
        self.emulator_host = emulator
        if emulator:
            self.base_url = f"http://{emulator}/v1/projects/{project_id}/databases/{database}/documents"
            self.emulator = True
        else:
            self.base_url = f"{API_ROOT}/projects/{project_id}/databases/{database}/documents"
            self.emulator = False
            if credentials:
                self.credentials = dict(credentials)
            else:
                loaded = _rs256.load_service_account()
                if loaded is None:
                    raise StoreError(
                        "No Firebase credentials found. Download a service-account "
                        "JSON and save it as firebase-service-account.json, or set "
                        "FIREBASE_SERVICE_ACCOUNT_PATH."
                    )
                self.credentials = loaded
            if credentials_path:
                with open(credentials_path, "r", encoding="utf-8") as handle:
                    self.credentials = json.load(handle)

    # ------------------------------------------------------------------
    # Paths & auth
    # ------------------------------------------------------------------
    @property
    def parent(self) -> str:
        base = self.base_url
        return base

    def _collection_url(self, collection: str) -> str:
        name = f"{self.prefix}/{collection}" if self.prefix else collection
        return f"{self.base_url}/{urllib.parse.quote(name, safe='/')}"

    def _document_url(self, collection: str, doc_id: str) -> str:
        return f"{self._collection_url(collection)}/{urllib.parse.quote(_sanitise_document_id(doc_id), safe='')}"

    def _document_name(self, collection: str, doc_id: str) -> str:
        """The fully-qualified resource name used inside batch ``commit`` writes."""
        name = f"{self.prefix}/{collection}" if self.prefix else collection
        return (
            f"projects/{self.project_id}/databases/{self.database}/documents/"
            f"{name}/{_sanitise_document_id(doc_id)}"
        )

    def _access_token(self, force_refresh: bool = False) -> str | None:
        """Return a cached OAuth 2.0 token, refreshing it when close to expiry."""
        if self.emulator:
            return None  # the emulator does not check credentials

        with self._lock:
            if not force_refresh and self._token and time.time() < self._token_expiry:
                return self._token

            # Prefer google-auth / firebase-admin when the user has them installed.
            token = self._token_via_google_auth()
            if token is None:
                token, expiry = _rs256.service_account_access_token(self.credentials)
            else:
                expiry = time.time() + 3500

            self._token = token
            self._token_expiry = expiry - TOKEN_SAFETY_MARGIN
            return token

    def _token_via_google_auth(self) -> str | None:
        """Use google-auth if present; return ``None`` to fall back to our signer."""
        try:  # pragma: no cover - depends on the environment
            from google.oauth2 import service_account  # type: ignore
            from google.auth.transport.requests import Request  # type: ignore
        except Exception:
            return None
        try:
            credentials = service_account.Credentials.from_service_account_info(
                self.credentials,
                scopes=["https://www.googleapis.com/auth/datastore"],
            )
            credentials.refresh(Request())
            return credentials.token
        except Exception:
            return None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        payload: Any = None,
        *,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        token = self._access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403) and retry_on_401 and not self.emulator:
                # Token may have expired between calls -- refresh once and retry.
                self._access_token(force_refresh=True)
                return self._request(method, url, payload, retry_on_401=False)
            if exc.code == 404:
                return {}
            raise StoreError(
                f"Firestore {method} {url} failed ({exc.code}): {detail[:600]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise StoreError(f"Cannot reach Firestore: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def init(self) -> None:
        """Collections are implicit in Firestore; just prove we can talk to it."""
        try:
            self._request("GET", f"{self._collection_url('meta')}?pageSize=1")
        except StoreError:
            raise
        self.put("meta", "schema", {"version": SCHEMA_VERSION, "updated_at": time.time()})

    def health(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for collection in ALL_COLLECTIONS:
            try:
                counts[collection] = self.count(collection)
            except StoreError as exc:
                counts[collection] = -1
                counts.setdefault("error", str(exc))  # type: ignore[arg-type]
        return {
            "backend": self.name,
            "ok": all(value >= 0 for value in counts.values()),
            "project_id": self.project_id,
            "database": self.database,
            "emulator": self.emulator,
            "collections": counts,
            "total_documents": sum(v for v in counts.values() if v > 0),
        }

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------
    def put(self, collection: str, doc_id: str, data: Mapping[str, Any]) -> None:
        document = dict(data)
        document.setdefault("_id", str(doc_id))
        document["_updated_at"] = time.time()
        self._request(
            "PATCH",
            self._document_url(collection, doc_id),
            to_firestore_document(document),
        )

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        response = self._request("GET", self._document_url(collection, doc_id))
        if not response or "fields" not in response:
            return None
        return from_firestore_document(response)

    def update(self, collection: str, doc_id: str, fields: Mapping[str, Any]) -> None:
        existing = self.get(collection, doc_id)
        if existing is None:
            return
        merged = {k: v for k, v in existing.items() if not k.startswith("_")}
        merged.update(dict(fields))
        self.put(collection, doc_id, merged)

    def delete(self, collection: str, doc_id: str) -> None:
        self._request("DELETE", self._document_url(collection, doc_id))

    def list(
        self,
        collection: str,
        filters: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        """List documents.

        Exact-match filters are pushed down to Firestore as an
        ``AND``-combined query. Comparison filters (tuples) are applied
        client-side, which keeps the query builder small and avoids composite
        index requirements -- fine at this project's data volume.
        """
        server_filters: dict[str, Any] = {}
        client_filters: dict[str, Any] = {}
        for field, condition in (filters or {}).items():
            if isinstance(condition, tuple):
                client_filters[field] = condition
            else:
                server_filters[field] = condition

        documents = self._run_query(collection, server_filters, order_by, descending)

        if client_filters:
            documents = [d for d in documents if matches_filters(d, client_filters)]
        if order_by or descending:
            documents = sort_documents(documents, order_by, descending)
        if limit is not None:
            documents = documents[:limit]
        return documents

    def _run_query(
        self,
        collection: str,
        server_filters: Mapping[str, Any],
        order_by: str | None,
        descending: bool,
    ) -> list[dict[str, Any]]:
        """Page through ``runQuery`` until the collection is exhausted."""
        structured: dict[str, Any] = {
            "from": [{"collectionId": f"{self.prefix}/{collection}" if self.prefix else collection}]
        }

        if server_filters:
            structured["where"] = {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {
                            "fieldFilter": {
                                "field": {"fieldPath": str(field)},
                                "op": "EQUAL",
                                "value": to_firestore_value(value),
                            }
                        }
                        for field, value in server_filters.items()
                    ],
                }
            }

        # Firestore requires the first orderBy to be __name__ when paginating.
        order_field = str(order_by) if order_by else "__name__"
        structured["orderBy"] = [
            {
                "field": {"fieldPath": order_field},
                "direction": "DESCENDING" if descending else "ASCENDING",
            }
        ]
        if order_field != "__name__":
            structured["orderBy"].append(
                {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}
            )

        documents: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = dict(structured)
            query["limit"] = PAGE_SIZE
            query["offset"] = offset
            response = self._request("POST", f"{self.parent}:runQuery", {"structuredQuery": query})

            page: list[dict[str, Any]] = []
            for entry in response if isinstance(response, list) else [response]:
                if not isinstance(entry, dict):
                    continue
                if "document" in entry:
                    page.append(from_firestore_document(entry["document"]))

            documents.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE  # simple offset paging; datasets here are small

        return documents

    def count(self, collection: str, filters: Mapping[str, Any] | None = None) -> int:
        return len(self.list(collection, filters))

    # ------------------------------------------------------------------
    # Bulk
    # ------------------------------------------------------------------
    def put_many(self, collection: str, documents: Mapping[str, Mapping[str, Any]]) -> int:
        """Write up to 500 documents per ``commit`` batch."""
        items = list(documents.items())
        written = 0
        for start in range(0, len(items), 400):
            chunk = items[start : start + 400]
            writes = []
            for doc_id, data in chunk:
                document = dict(data)
                document.setdefault("_id", str(doc_id))
                document["_updated_at"] = time.time()
                writes.append(
                    {
                        "update": {
                            "name": self._document_name(collection, doc_id),
                            **to_firestore_document(document),
                        }
                    }
                )
            self._request("POST", f"{self.parent}:commit", {"writes": writes})
            written += len(chunk)
        return written

    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(doc_ids)
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            response = self._request(
                "POST",
                f"{self.parent}:batchGet",
                {"documents": [self._document_url(collection, i) for i in chunk]},
            )
            entries = response if isinstance(response, list) else ()
            for entry in entries:
                document = entry.get("found") if isinstance(entry, dict) else None
                if document and "fields" in document:
                    parsed = from_firestore_document(document)
                    found[str(parsed.get("_id", ""))] = parsed
        return found

    # ------------------------------------------------------------------
    # Migration helper
    # ------------------------------------------------------------------
    def import_snapshot(
        self, snapshot: Mapping[str, Mapping[str, Any]], *, collections: Iterable[str] | None = None
    ) -> dict[str, int]:
        """Push a local snapshot (``LocalStore.snapshot()``) into Firestore."""
        summary: dict[str, int] = {}
        for collection in collections or snapshot.keys():
            documents = snapshot.get(collection) or {}
            if documents:
                summary[collection] = self.put_many(collection, documents)
        return summary


def _emulator_host() -> str | None:
    import os

    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    return host or None


__all__ = [
    "FirestoreStore",
    "to_firestore_value",
    "from_firestore_value",
    "to_firestore_document",
    "from_firestore_document",
]
