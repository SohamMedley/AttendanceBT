"""
Storage backends and the factory that selects one.

``build_store()`` implements a deliberate *graceful degradation* policy:

* ``backend = "firestore"`` -> Firestore. If credentials are missing or the
  network is down, we do **not** crash: we log the reason and fall back to the
  local JSON file so the app keeps working (a real requirement for a college
  lab with patchy Wi-Fi).
* ``backend = "local"``     -> JSON file only.
* ``backend = "auto"``      -> Firestore when fully configured, else local.

The chosen backend is reported at ``/api/system/status`` and shown as a badge in
the UI, so there is never any doubt about where the data is going.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    ALL_COLLECTIONS,
    ANCHORS,
    ATTENDANCE,
    AUDIT,
    BLOCKS,
    FACULTY,
    META,
    SCHEMA_VERSION,
    SESSIONS,
    STUDENTS,
    SUBJECTS,
    Store,
    StoreError,
)
from .firestore_store import FirestoreStore
from .local_store import LocalStore

log = logging.getLogger("bcoe.storage")

__all__ = [
    "Store",
    "StoreError",
    "LocalStore",
    "FirestoreStore",
    "build_store",
    "get_store",
    "reset_store",
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
]

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
            except (StoreError, OSError, ValueError, ImportError) as exc:
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
        from ..config import settings

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
