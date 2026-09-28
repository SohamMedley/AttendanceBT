"""
Service container -- one place that wires the application together.

Layer map (bottom to top)::

    storage          -> documents (local JSON | Firestore)
    repository       -> domain queries over those documents
    blockchain       -> blocks, transactions, Merkle trees, Proof-of-Work
    identity         -> secp256k1 key pairs (custodial | non-custodial)
    services         -> ledger, analytics, anomaly detection, anchoring
    api              -> HTTP endpoints
    templates/static -> the UI

Constructing :class:`Services` once and sharing it keeps the Flask app thin and
makes the whole system testable without a browser.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import AppConfig, settings
from ..storage import get_store
from .analytics import (
    MU_MIN_ATTENDANCE,
    dashboard_stats,
    defaulter_list,
    export_rows,
    student_report,
    student_subject_summary,
    subject_summary,
)
from .anomaly import scan as scan_anomalies
from .anchoring import AnchorService
from .identity import INSTITUTION_OWNER_ID, Identity, KeyStore, ensure_institution_identity
from .ledger import Ledger, LedgerError, MarkResult
from .qr import QRToken, QRTokenError, issue_token, verify_token
from .repository import Repository

log = logging.getLogger("bcoe.services")

__all__ = [
    "Services",
    "get_services",
    "reset_services",
    "Repository",
    "Ledger",
    "LedgerError",
    "MarkResult",
    "AnchorService",
    "KeyStore",
    "Identity",
    "QRToken",
    "QRTokenError",
    "issue_token",
    "verify_token",
    "dashboard_stats",
    "student_report",
    "student_subject_summary",
    "defaulter_list",
    "subject_summary",
    "export_rows",
    "scan_anomalies",
    "MU_MIN_ATTENDANCE",
    "INSTITUTION_OWNER_ID",
]


class Services:
    """Owns every long-lived object the application needs."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        keystore_path: str | None = None,
        store=None,
    ) -> None:
        self.config = config or settings
        # Tests (and any embedding application) can inject their own store;
        # otherwise fall back to the process-wide one.
        self.store = store if store is not None else get_store(self.config)
        self.repo = Repository(self.store)

        keystore_file = Path(
            keystore_path
            or (Path("data") / "keystore" / "keys.json")
        )
        self.keystore = KeyStore(keystore_file)
        self.keystore.load()

        self.ledger = Ledger(self.config, self.store, self.repo, self.keystore)
        self.anchoring = AnchorService(self.config, self.repo, self.ledger, self.keystore)

        # The college's own signing identity must always exist.
        self.institution = ensure_institution_identity(self.keystore)

    # ------------------------------------------------------------------
    # Register helpers
    # ------------------------------------------------------------------
    def register_keys_for_students(self, students: list[dict[str, Any]]) -> int:
        """Make sure every student in the register has a signing key.

        In custodial mode the key is generated here; if a student already has a
        public key recorded (non-custodial), we leave it alone.
        """
        created = 0
        for student in students:
            roll = student["roll_no"]
            if self.keystore.get(roll) is None:
                self.keystore.create(roll, role="student")
                created += 1
        if created:
            self.keystore.save()
        return created

    def register_keys_for_faculty(self, faculty: list[dict[str, Any]]) -> int:
        created = 0
        for record in faculty:
            faculty_id = record["faculty_id"]
            if self.keystore.get(faculty_id) is None:
                self.keystore.create(faculty_id, role="faculty")
                created += 1
        if created:
            self.keystore.save()
        return created

    def is_seeded(self) -> bool:
        return self.repo.student_count() > 0

    # ------------------------------------------------------------------
    # Convenience queries used by several blueprints
    # ------------------------------------------------------------------
    def students(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.repo.list_students(**kwargs)

    def subjects_for_year(self, year: str) -> list[dict[str, Any]]:
        return [s for s in self.repo.list_subjects() if s.get("year") == year]

    def sessions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.repo.list_sessions(**kwargs)

    def attendance(self) -> list[dict[str, Any]]:
        return self.repo.attendance_all()

    def chain_stats(self) -> dict[str, Any]:
        stats = self.ledger.chain.stats()
        report = self.ledger.chain.last_validation
        stats["last_report_valid"] = report.valid if report else True
        stats["storage_backend"] = self.store.name
        stats["difficulty"] = self.ledger.chain.difficulty
        return stats

    def dashboard(self) -> dict[str, Any]:
        return dashboard_stats(
            students=self.repo.list_students(),
            subjects=self.repo.list_subjects(),
            sessions=self.repo.list_sessions(),
            attendance=self.repo.attendance_all(),
            chain_stats=self.chain_stats(),
            anchors=self.repo.list_anchors(limit=10),
        )

    def status(self) -> dict[str, Any]:
        """System status for the settings screen and the UI badge."""
        from ..storage import store_report

        return {
            "storage": {
                "backend": self.store.name,
                "report": store_report(),
                "health": self._safe_health(),
            },
            "chain": self.chain_stats(),
            "ledger": {
                "keystore_size": len(self.keystore),
                "institution_address": self.institution.address,
                "institution_public_key": self.institution.public_key,
            },
            "anchor": self.anchoring.pending_summary(),
            "register": {
                "students": self.repo.student_count(),
                "faculty": len(self.repo.list_faculty()),
                "subjects": len(self.repo.list_subjects()),
                "sessions": len(self.repo.list_sessions()),
                "attendance_records": len(self.repo.attendance_all()),
                "blocks": self.repo.block_count(),
                "anchors": len(self.repo.list_anchors()),
            },
            "config": self.config.as_dict(),
        }

    def _safe_health(self) -> dict[str, Any]:
        try:
            return self.store.health()
        except Exception as exc:  # pragma: no cover - health must never raise
            return {"ok": False, "error": str(exc), "backend": self.store.name}


_services: Services | None = None


def get_services(config: AppConfig | None = None) -> Services:
    """Process-wide :class:`Services` instance."""
    global _services
    if _services is None:
        _services = Services(config)
    return _services


def reset_services() -> None:
    global _services
    _services = None
