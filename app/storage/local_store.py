"""
Local JSON-file store -- the zero-setup fallback backend.

Data lives in a single human-readable JSON file (default
``data/attendance_ledger.json``) so a student can open it in a text editor,
break a byte, and watch the chain validator catch it. That is a *feature* for
this project, not a limitation: it makes tamper-evidence visible.

Writes are atomic (temp file + ``os.replace``) and guarded by a re-entrant lock,
so two browser tabs cannot interleave and corrupt the file. An in-memory cache
keeps reads fast and mirrors exactly what is on disk.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .base import (
    ALL_COLLECTIONS,
    SCHEMA_VERSION,
    Store,
    StoreError,
    matches_filters,
    sort_documents,
)


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


__all__ = ["LocalStore"]
