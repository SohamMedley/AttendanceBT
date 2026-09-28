"""
Storage abstraction.

Both backends (local JSON file and Google Firestore) expose the *same* small
document API: ``put`` / ``get`` / ``list`` / ``update`` / ``delete`` / ``count``
over named collections. Domain-specific queries live in
``app/services/repository.py`` so they are written once and work identically on
either backend.

Collections used by the project
------------------------------
``students``     roll number -> profile, public key, wallet address
``faculty``      employee id -> profile, public key, subjects taught
``subjects``     subject code -> name, department, semester, credits
``sessions``     session id  -> one lecture's roll-call window + QR secret
``attendance``   tx id       -> one student's record for one session (index)
``blocks``       block index -> the authoritative block data
``anchors``      anchor id   -> public-chain anchoring proof
``audit``        audit id    -> append-only trail of sensitive actions
``meta``         key         -> schema version, chain settings, counters

The ``blocks`` collection is the source of truth for attendance; ``attendance``
is a query index that can always be rebuilt from the chain.
"""

from __future__ import annotations

import abc
from typing import Any, Iterable, Mapping, Sequence

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


__all__ = [
    "Store",
    "StoreError",
    "STUDENTS",
    "FACULTY",
    "SUBJECTS",
    "SESSIONS",
    "ATTENDANCE",
    "BLOCKS",
    "ANCHORS",
    "AUDIT",
    "META",
    "ALL_COLLECTIONS",
    "SCHEMA_VERSION",
    "matches_filters",
    "sort_documents",
]
