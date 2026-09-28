"""
Persistence: one document-store contract with two implementations.

The application talks to a small document database (collections, documents,
a few filter operators) and never to a vendor SDK. That is what lets the same
code run on a local JSON file during a demo and on Google Cloud Firestore in
production, and what makes the move in between a configuration change rather
than a rewrite.

Firestore is reached over its REST API with urllib, and the service-account
JWT is signed by the pure-Python RS256 implementation in this file, so neither
firebase-admin nor google-auth has to be installed.
"""

from __future__ import annotations

import abc
import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path
from typing import Any
from typing import Any, Iterable, Mapping
from typing import Any, Iterable, Mapping, Sequence


# ============================================================================
# The document-store contract
# ============================================================================
#
# Storage abstraction.
#
# Both backends (local JSON file and Google Firestore) expose the *same* small
# document API: ``put`` / ``get`` / ``list`` / ``update`` / ``delete`` / ``count``
# over named collections. Domain-specific queries live in
# ``app/services/repository.py`` so they are written once and work identically on
# either backend.
#
# Collections used by the project
# ------------------------------
# ``students``     roll number -> profile, public key, wallet address
# ``faculty``      employee id -> profile, public key, subjects taught
# ``subjects``     subject code -> name, department, semester, credits
# ``sessions``     session id  -> one lecture's roll-call window + QR secret
# ``attendance``   tx id       -> one student's record for one session (index)
# ``blocks``       block index -> the authoritative block data
# ``anchors``      anchor id   -> public-chain anchoring proof
# ``audit``        audit id    -> append-only trail of sensitive actions
# ``meta``         key         -> schema version, chain settings, counters
#
# The ``blocks`` collection is the source of truth for attendance; ``attendance``
# is a query index that can always be rebuilt from the chain.

# Collection name constants -- used instead of loose strings so a typo is a
# NameError at import time rather than a silently empty query.
STUDENTS = "students"
FACULTY = "faculty"
SUBJECTS = "subjects"
SESSIONS = "sessions"
ATTENDANCE = "attendance"
BLOCKS = "blocks"
ANCHORS = "anchors"
AUDIT = "audit"
META = "meta"
KEYSTORE = "keystore"

ALL_COLLECTIONS = (
    STUDENTS,
    FACULTY,
    SUBJECTS,
    SESSIONS,
    ATTENDANCE,
    BLOCKS,
    ANCHORS,
    AUDIT,
    META,
    # The custodial keystore is mirrored here by KeyStore.save(), so private
    # keys survive a redeploy on a host with an ephemeral filesystem.
    KEYSTORE,
)

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Any storage backend failure, normalised into one exception type."""


class Store(abc.ABC):
    """Abstract document store.

    Implementations only need to be correct for these operations; every
    higher-level feature (attendance marking, auditing, anchoring) is built on
    top of them.
    """

    #: Human-readable backend name shown in the UI badge.
    name: str = "store"
    #: True when data survives the process (always true here, but explicit).
    durable: bool = True

    def __init__(self, **options: Any) -> None:
        self.options = options

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def init(self) -> None:
        """Create anything the backend needs (directories, collections)."""

    def health(self) -> dict[str, Any]:
        """Cheap connectivity probe used by the settings screen."""
        counts = {collection: self.count(collection) for collection in ALL_COLLECTIONS}
        return {
            "backend": self.name,
            "ok": True,
            "collections": counts,
            "total_documents": sum(counts.values()),
        }

    def close(self) -> None:  # pragma: no cover - overridden when needed
        return None

    # -- single document operations ---------------------------------------
    @abc.abstractmethod
    def put(self, collection: str, doc_id: str, data: Mapping[str, Any]) -> None:
        """Create or fully replace a document."""

    @abc.abstractmethod
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Fetch one document, or ``None``."""

    @abc.abstractmethod
    def update(self, collection: str, doc_id: str, fields: Mapping[str, Any]) -> None:
        """Merge ``fields`` into an existing document."""

    @abc.abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """Remove a document if it exists."""

    @abc.abstractmethod
    def list(
        self,
        collection: str,
        filters: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        """List documents, optionally filtered by exact field matches.

        Filters are ANDed. ``filters`` may also use a ``("field", "op", value)``
        tuple form for ``in`` / ``>=`` / ``<=`` comparisons, e.g.
        ``{"marked_at": (">=", start)}``.
        """

    def count(self, collection: str, filters: Mapping[str, Any] | None = None) -> int:
        return len(self.list(collection, filters))

    # -- bulk helpers (default implementations; override for speed) --------
    def put_many(self, collection: str, documents: Mapping[str, Mapping[str, Any]]) -> int:
        for doc_id, data in documents.items():
            self.put(collection, doc_id, data)
        return len(documents)

    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for doc_id in doc_ids:
            document = self.get(collection, doc_id)
            if document is not None:
                found[doc_id] = document
        return found

    # -- schema ------------------------------------------------------------
    def schema_version(self) -> int:
        meta = self.get(META, "schema") or {}
        return int(meta.get("version", 0))

    def write_schema_version(self, version: int = SCHEMA_VERSION) -> None:
        self.put(META, "schema", {"version": version, "updated_at": _now()})


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _now() -> float:
    import time

    return time.time()


def matches_filters(document: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """Apply the filter grammar used by ``Store.list``.

    ``{"status": "PRESENT"}``                  exact match
    ``{"marked_at": (">=", 1000)}``            comparison
    ``{"roll_no": ("in", ["A", "B"])}``        membership
    ``{"room": (None,)}``                      "field is absent / null"
    """
    for field, condition in filters.items():
        value = document.get(field)
        if isinstance(condition, tuple):
            if len(condition) == 1:
                if value not in (None, ""):
                    return False
                continue
            operator, operand = condition[0], condition[1]
            if operator == "in":
                if value not in operand:
                    return False
            elif operator == "!=":
                if value == operand:
                    return False
            elif operator == ">=":
                if value is None or value < operand:
                    return False
            elif operator == "<=":
                if value is None or value > operand:
                    return False
            elif operator == ">":
                if value is None or value <= operand:
                    return False
            elif operator == "<":
                if value is None or value >= operand:
                    return False
            elif operator == "contains":
                if operand not in (value or []):
                    return False
            else:
                raise StoreError(f"Unsupported filter operator: {operator}")
        else:
            if value != condition:
                return False
    return True


def sort_documents(
    documents: Sequence[dict[str, Any]],
    order_by: str | None,
    descending: bool,
) -> list[dict[str, Any]]:
    if not order_by:
        return list(documents)
    return sorted(
        documents,
        key=lambda doc: (doc.get(order_by) is None, doc.get(order_by)),
        reverse=descending,
    )


# ============================================================================
# Local JSON store (the offline default)
# ============================================================================
#
# Local JSON-file store -- the zero-setup fallback backend.
#
# Data lives in a single human-readable JSON file (default
# ``data/attendance_ledger.json``) so a student can open it in a text editor,
# break a byte, and watch the chain validator catch it. That is a *feature* for
# this project, not a limitation: it makes tamper-evidence visible.
#
# Writes are atomic (temp file + ``os.replace``) and guarded by a re-entrant lock,
# so two browser tabs cannot interleave and corrupt the file. An in-memory cache
# keeps reads fast and mirrors exactly what is on disk.

class LocalStore(Store):
    """JSON-file backed implementation of :class:`~app.storage.base.Store`."""

    name = "local-json"

    def __init__(self, path: str | os.PathLike[str] = "data/attendance_ledger.json") -> None:
        super().__init__(path=str(path))
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {
            collection: {} for collection in ALL_COLLECTIONS
        }
        self._dirty = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            self._load()
        else:
            self._flush()
        self.write_schema_version(SCHEMA_VERSION)

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StoreError(
                f"{self.path} is not valid JSON: {exc}. "
                "Restore it from a backup or delete it to start fresh."
            ) from exc

        # The on-disk shape is {"schema_version", "saved_at", "collections": {...}}.
        # Older/other writers may store collections at the top level, so accept
        # both -- silently losing the register on reload would be catastrophic.
        payload = raw.get("collections") if isinstance(raw.get("collections"), dict) else raw

        with self._lock:
            for collection in ALL_COLLECTIONS:
                # Each collection is a dict of doc_id -> document
                loaded = payload.get(collection)
                self._data[collection] = dict(loaded) if isinstance(loaded, dict) else {}
            self._data.setdefault("meta", {})
        self._dirty = False

    def _flush(self) -> None:
        """Atomically write the whole dataset to disk."""
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "collections": self._data,
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, default=str, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._dirty = False

    def close(self) -> None:
        if self._dirty:
            self._flush()

    def _autosave(self) -> None:
        self._dirty = True
        # Small datasets: flushing every write keeps the on-disk file always in
        # sync with memory, which is what makes the tamper demo predictable.
        self._flush()

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------
    def _bucket(self, collection: str) -> dict[str, dict[str, Any]]:
        if collection not in self._data:
            self._data[collection] = {}
        return self._data[collection]

    def put(self, collection: str, doc_id: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            document = json.loads(json.dumps(dict(data), default=str))  # deep, JSON-safe copy
            document.setdefault("_id", doc_id)
            document["_updated_at"] = time.time()
            self._bucket(collection)[str(doc_id)] = document
            self._autosave()

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            document = self._bucket(collection).get(str(doc_id))
            return json.loads(json.dumps(document)) if document is not None else None

    def update(self, collection: str, doc_id: str, fields: Mapping[str, Any]) -> None:
        with self._lock:
            existing = self._bucket(collection).get(str(doc_id))
            if existing is None:
                return
            merged = dict(existing)
            merged.update(dict(fields))
            merged["_updated_at"] = time.time()
            self._bucket(collection)[str(doc_id)] = json.loads(
                json.dumps(merged, default=str)
            )
            self._autosave()

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            removed = self._bucket(collection).pop(str(doc_id), None)
            if removed is not None:
                self._autosave()

    def list(
        self,
        collection: str,
        filters: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            documents = [json.loads(json.dumps(doc)) for doc in self._bucket(collection).values()]

        if filters:
            documents = [doc for doc in documents if matches_filters(doc, filters)]

        documents = sort_documents(documents, order_by, descending)
        if limit is not None:
            documents = documents[:limit]
        return documents

    def count(self, collection: str, filters: Mapping[str, Any] | None = None) -> int:
        with self._lock:
            if not filters:
                return len(self._bucket(collection))
            return sum(
                1 for doc in self._bucket(collection).values() if matches_filters(doc, filters)
            )

    # ------------------------------------------------------------------
    # Bulk
    # ------------------------------------------------------------------
    def put_many(self, collection: str, documents: Mapping[str, Mapping[str, Any]]) -> int:
        """Single flush for a batch -- used when seeding the whole register."""
        with self._lock:
            bucket = self._bucket(collection)
            for doc_id, data in documents.items():
                document = json.loads(json.dumps(dict(data), default=str))
                document.setdefault("_id", doc_id)
                document["_updated_at"] = time.time()
                bucket[str(doc_id)] = document
            self._autosave()
        return len(documents)

    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            bucket = self._bucket(collection)
            return {
                str(doc_id): json.loads(json.dumps(bucket[str(doc_id)]))
                for doc_id in doc_ids
                if str(doc_id) in bucket
            }

    # ------------------------------------------------------------------
    # Extras used by the admin screens
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Everything, for export, backup or pushing to Firestore."""
        with self._lock:
            return json.loads(json.dumps(self._data, default=str))

    def replace_all(self, data: Mapping[str, Mapping[str, Any]]) -> None:
        with self._lock:
            self._data = {collection: {} for collection in ALL_COLLECTIONS}
            for collection, documents in data.items():
                self._data[collection] = dict(documents)
            self._flush()

    def file_size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def health(self) -> dict[str, Any]:
        info = super().health()
        info.update(
            {
                "path": str(self.path.resolve()),
                "file_size_bytes": self.file_size_bytes(),
                "mode": "offline",
            }
        )
        return info


# ============================================================================
# Google Cloud Firestore over REST
# ============================================================================
#
# Google Cloud Firestore backend (REST transport, no SDK required).
#
# Why REST instead of ``firebase-admin``?
# ---------------------------------------
# Firestore's REST API is a plain JSON/HTTPS interface. Using it directly means:
#
# * the project still runs on a lab PC with only the standard library installed;
# * you can *see* the exact HTTP calls in ``/api/firestore/probe`` -- which is far
#   more useful in a viva than "the library does it";
# * if ``firebase-admin`` *is* installed we use it for authentication, so you get
#   the convenience without the hard dependency.
#
# Authentication
# --------------
# A Firebase service account gives us a private key; we sign a JWT assertion and
# exchange it for an OAuth 2.0 access token (see ``py``). Tokens are cached
# in memory until 60 seconds before expiry.
#
# Setup
# -----
# 1. Firebase console -> Project settings -> Service accounts -> *Generate new
#    private key*. Save the JSON as ``firebase-service-account.json`` in the
#    project root (it is git-ignored -- never commit it).
# 2. Put your project id in ``config.json`` or the ``FIREBASE_PROJECT_ID`` env var.
# 3. Run ``python manage.py firebase-check``.
#
# Data representation
# -------------------
# All timestamps are stored as Firestore ``doubleValue`` (epoch seconds). This
# keeps the wire format identical to the local JSON backend, so records can be
# moved between the two backends without conversion.

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
                loaded = load_service_account()
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
                token, expiry = service_account_access_token(self.credentials)
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


# ============================================================================
# Pure-Python RS256 (Firebase service-account auth)
# ============================================================================
#
# Minimal, dependency-free RS256 JWT signer.
#
# Firestore's REST API needs an OAuth 2.0 access token. Google expects a signed
# JWT "assertion" (the standard two-legged service-account flow). The official
# ``google-auth`` library does this, but we also ship this ~150-line implementation
# so the project can talk to Firestore with *nothing installed* beyond the Python
# standard library -- which matters when you have to demo on a college lab machine.
#
# Only what Firestore needs is implemented:
#   * DER/PKCS#8 parsing of the service-account private key (RSA)
#   * RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017)
#   * Compact JWS serialisation (JWT = base64url(header).base64url(claims).sig)
#
# If ``firebase-admin`` or ``google-auth`` is installed, we prefer those -- see
# ``firestore_store.py``. This module is the fallback.

# ASN.1 DER DigestInfo prefix for SHA-256 (RFC 8017, section 9.2, notes)
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class JWTError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tiny ASN.1 DER reader
# --------------------------------------------------------------------------
def _der_read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one TLV triple. Returns (tag, value_bytes, next_offset)."""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        num_bytes = length & 0x7F
        length = int.from_bytes(data[offset : offset + num_bytes], "big")
        offset += num_bytes
    value = data[offset : offset + length]
    return tag, value, offset + length


def _der_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def parse_pkcs8_rsa_private_key(pem: str) -> tuple[int, int]:
    """Extract ``(n, d)`` -- the RSA modulus and private exponent -- from a PKCS#8 PEM.

    Structure we walk::

        PrivateKeyInfo ::= SEQUENCE {
            version Integer,
            privateKeyAlgorithm AlgorithmIdentifier,
            privateKey OCTET STRING   -- contains RSAPrivateKey
        }
        RSAPrivateKey ::= SEQUENCE {
            version, modulus(n), publicExponent(e), privateExponent(d), ...
        }

    We only need ``n`` and ``d`` to sign.
    """
    body = "".join(
        line.strip()
        for line in pem.splitlines()
        if "-----" not in line and line.strip()
    )
    der = base64.b64decode(body)

    tag, seq, _ = _der_read(der, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE at the top of the key")

    offset = 0
    _, _version, offset = _der_read(seq, offset)
    _, _algorithm, offset = _der_read(seq, offset)
    tag, octet, _ = _der_read(seq, offset)
    if tag != 0x04:
        raise JWTError("Expected an OCTET STRING wrapping the RSA key")

    offset = 0
    tag, rsa_seq, _ = _der_read(octet, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE inside the private key")

    offset = 0
    _, _v, offset = _der_read(rsa_seq, offset)          # version
    _, n_bytes, offset = _der_read(rsa_seq, offset)     # modulus
    _, _e_bytes, offset = _der_read(rsa_seq, offset)    # public exponent
    _, d_bytes, offset = _der_read(rsa_seq, offset)     # private exponent

    return _der_int(n_bytes), _der_int(d_bytes)


# --------------------------------------------------------------------------
# RSASSA-PKCS1-v1_5 signing
# --------------------------------------------------------------------------
def _pkcs1_v15_pad(message: bytes, key_length: int) -> bytes:
    """EMSA-PKCS1-v1_5 encoding: 0x00 || 0x01 || PS(0xFF...) || 0x00 || DigestInfo."""
    padding_length = key_length - len(message) - 3
    if padding_length < 8:
        raise JWTError("RSA key is too small for a SHA-256 signature")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + message


def rsa_sha256_sign(message: bytes, n: int, d: int) -> bytes:
    """Sign with RSA: signature = EM^d mod n."""
    key_length = (n.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    encoded = _pkcs1_v15_pad(digest_info, key_length)
    signature = pow(int.from_bytes(encoded, "big"), d, n)
    return signature.to_bytes(key_length, "big")


def rsa_sha256_verify(message: bytes, signature: bytes, n: int, e: int = 65537) -> bool:
    """Verify a signature. Used by our own self-test so the maths is proven."""
    key_length = (n.bit_length() + 7) // 8
    if len(signature) != key_length:
        return False
    recovered = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_length, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    return recovered == _pkcs1_v15_pad(digest_info, key_length)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_signed_jwt(
    *,
    client_email: str,
    private_key_pem: str,
    private_key_id: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
    scope: str = "https://www.googleapis.com/auth/datastore",
    lifetime: int = 3600,
) -> str:
    """Build a Google service-account JWT assertion."""
    n, d = parse_pkcs8_rsa_private_key(private_key_pem)

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": private_key_id}
    claims = {
        "iss": client_email,
        "scope": scope,
        "aud": token_uri,
        "iat": now,
        "exp": now + lifetime,
    }

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")

    signature = rsa_sha256_sign(signing_input, n, d)
    return signing_input.decode("ascii") + "." + _b64url(signature)


def exchange_jwt_for_token(
    jwt: str, token_uri: str = "https://oauth2.googleapis.com/token", timeout: int = 30
) -> dict[str, Any]:
    """Trade the JWT assertion for an OAuth 2.0 access token."""
    payload = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        token_uri,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.read().decode("utf-8", errors="replace")
        raise JWTError(f"Google token endpoint rejected the assertion: {detail}") from exc
    except Exception as exc:  # pragma: no cover - network dependent
        raise JWTError(f"Could not reach the Google token endpoint: {exc}") from exc


def service_account_access_token(credentials: dict[str, Any]) -> tuple[str, float]:
    """Return ``(access_token, expiry_epoch)`` for a service-account dict."""
    jwt = build_signed_jwt(
        client_email=credentials["client_email"],
        private_key_pem=credentials["private_key"],
        private_key_id=credentials.get("private_key_id", ""),
        token_uri=credentials.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    response = exchange_jwt_for_token(
        jwt, credentials.get("token_uri", "https://oauth2.googleapis.com/token")
    )
    if "access_token" not in response:
        raise JWTError(f"No access_token in the token response: {response}")
    expires_in = float(response.get("expires_in", 3600))
    return response["access_token"], time.time() + expires_in


def load_service_account() -> dict[str, Any] | None:
    """Locate service-account credentials.

    Order of precedence:
      1. ``FIREBASE_SERVICE_ACCOUNT``  -- the JSON itself (good for hosting env vars)
      2. ``FIREBASE_SERVICE_ACCOUNT_PATH`` -- path to the downloaded JSON file
      3. ``./firebase-service-account.json`` or ``./secrets/firebase-service-account.json``
      4. ``FIREBASE_CREDENTIALS`` (path; the variable firebase-admin itself reads)
    """
    inline = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise JWTError("FIREBASE_SERVICE_ACCOUNT is not valid JSON") from exc

    candidates: list[str] = []
    for variable in ("FIREBASE_SERVICE_ACCOUNT_PATH", "FIREBASE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)
    candidates += [
        "firebase-service-account.json",
        "secrets/firebase-service-account.json",
        "data/firebase-service-account.json",
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as handle:
                return json.load(handle)
    return None


# ============================================================================
# Backend selection
# ============================================================================
#
# Storage backends and the factory that selects one.
#
# ``build_store()`` implements a deliberate *graceful degradation* policy:
#
# * ``backend = "firestore"`` -> Firestore. If credentials are missing or the
#   network is down, we do **not** crash: we log the reason and fall back to the
#   local JSON file so the app keeps working (a real requirement for a college
#   lab with patchy Wi-Fi).
# * ``backend = "local"``     -> JSON file only.
# * ``backend = "auto"``      -> Firestore when fully configured, else local.
#
# The chosen backend is reported at ``/api/system/status`` and shown as a badge in
# the UI, so there is never any doubt about where the data is going.

log = logging.getLogger("bcoe.storage")

_store: Store | None = None
_store_report: dict[str, Any] = {}


def build_store(config) -> tuple[Store, dict[str, Any]]:
    """Create the store described by ``config``, with fallback.

    Returns the store and a report describing what happened, so the UI can show
    *why* it ended up on the local backend.
    """
    storage = config.storage
    requested = (storage.backend or "auto").lower()
    report: dict[str, Any] = {"requested": requested, "fallback": False, "notes": []}

    wants_firestore = requested in {"auto", "firestore", "firebase"}
    if wants_firestore:
        project_id = storage.firebase_project_id
        if not project_id:
            report["notes"].append(
                "FIREBASE_PROJECT_ID is not set, so Firestore cannot be used."
            )
        else:
            try:
                credentials = None
                if storage.firebase_service_account:
                    import json

                    with open(storage.firebase_service_account, "r", encoding="utf-8") as fh:
                        credentials = json.load(fh)
                store = FirestoreStore(
                    project_id,
                    credentials=credentials,
                    database=storage.firebase_database,
                    prefix=storage.firebase_prefix,
                )
                store.init()
                report.update(
                    {
                        "active": store.name,
                        "project_id": project_id,
                        "database": storage.firebase_database,
                        "notes": report["notes"] + ["Connected to Google Cloud Firestore."],
                    }
                )
                return store, report
            except Exception as exc:
                # Deliberately broad. Choosing a storage backend must never be
                # able to stop the application from starting, and the failure
                # modes here are wide: a malformed service-account variable
                # (JWTError), an expired key, DNS failure, a disabled Firestore
                # API, a 403 from a wrong project. Naming the types meant one of
                # them -- JWTError, raised when the credentials simply are not
                # valid JSON -- crashed the app on boot instead of degrading,
                # which is precisely what this fallback exists to prevent.
                report["notes"].append(f"Firestore unavailable: {exc}")
                if requested == "firestore":
                    report["notes"].append(
                        "Falling back to the local JSON store so the application "
                        "still runs. Fix the credentials and restart to use Firestore."
                    )

    store = LocalStore(storage.local_path)
    store.init()
    report.update(
        {
            "active": store.name,
            "path": str(store.path),
            "fallback": wants_firestore and requested != "local" and bool(report["notes"]),
            "notes": report["notes"]
            + ["Using the local JSON store: no internet or credentials required."],
        }
    )
    return store, report


def get_store(config=None) -> Store:
    """Return the process-wide store, creating it on first use."""
    global _store, _store_report
    if _store is None:
        from .config import settings

        _store, _store_report = build_store(config or settings)
        log.info("Storage backend: %s (%s)", _store.name, _store_report.get("notes"))
    return _store


def store_report() -> dict[str, Any]:
    return dict(_store_report)


def reset_store() -> None:
    """Drop the cached store -- used by tests and the settings screen."""
    global _store, _store_report
    if _store is not None:
        try:
            _store.close()
        except Exception:  # pragma: no cover - best effort
            pass
    _store = None
    _store_report = {}
