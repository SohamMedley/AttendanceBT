"""
The live layer: sessions, attendance marking, sealing and anchoring.

``Ledger`` is the entry point for everything that changes state -- opening a
roll call, accepting a scan, sealing a block, running a verification. The
anchor service publishes Merkle roots to an external ledger, and ``Services``
wires the whole application together with the storage backend and key store.
"""

from __future__ import annotations

import logging
import secrets
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .anchoring import build_provider
from .blockchain.chain import Blockchain, estimate_attempts
from .blockchain.crypto import merkle_root as compute_merkle_root
from .blockchain.chain import STATUS_ABSENT, STATUS_LATE, STATUS_MANUAL, STATUS_PRESENT, TX_ATTENDANCE, build_attendance_transaction
from .blockchain.chain import TX_ATTENDANCE, build_anchor_transaction
from .config import AppConfig, settings
from . import services
from . import services as qr
from .services import INSTITUTION_OWNER_ID, Identity, KeyStore, ensure_institution_identity
from .services import MU_MIN_ATTENDANCE, dashboard_stats, defaulter_list, export_rows, student_report, student_subject_summary, subject_summary
from .services import QRToken, QRTokenError, issue_token, verify_token
from .services import Repository
from .services import scan as scan_anomalies
from .storage import SESSIONS, Store
from .storage import get_store


# ============================================================================
# The ledger (sessions, marking, sealing)
# ============================================================================
#
# The attendance ledger: the single place where all the pieces meet.
#
# Flow of a single roll-call::
#
#     faculty opens session
#         -> server generates a random session secret + rotating QR
#     student scans QR
#         -> POST /api/attendance/mark  (token + roll number)
#     ledger.mark_attendance()
#         1. session must be OPEN
#         2. QR token must be signed, fresh and for THIS session
#         3. student must exist, be active, and belong to this division
#         4. anti-abuse: rate limits, duplicate check, shared-device check
#         5. status -> PRESENT (within grace) or LATE
#         6. build a transaction and sign it with the student's key
#         7. append to the mempool; seal a block when the batch fills
#         8. write the query index and return a receipt with the Merkle proof
#     faculty closes session
#         -> pending transactions are sealed into one block, the block hash is
#            returned, and the block becomes an anchoring candidate
#
# Every rejection returns a *stable error code* so the UI can show a precise
# message instead of a generic failure.

log = logging.getLogger("bcoe.ledger")


class LedgerError(Exception):
    """A business-rule rejection, carrying a stable machine-readable code."""

    def __init__(self, message: str, code: str = "REJECTED", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message}


@dataclass
class MarkResult:
    """What the student sees after scanning."""

    ok: bool
    tx_id: str
    status: str
    student_roll: str
    student_name: str
    session_id: str
    subject_code: str
    marked_at: float
    block_index: int | None
    block_hash: str | None
    merkle_root: str | None
    merkle_proof_verified: bool | None
    signature: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tx_id": self.tx_id,
            "status": self.status,
            "student_roll": self.student_roll,
            "student_name": self.student_name,
            "session_id": self.session_id,
            "subject_code": self.subject_code,
            "marked_at": self.marked_at,
            "on_chain": self.block_index is not None,
            "block_index": self.block_index,
            "block_hash": self.block_hash,
            "merkle_root": self.merkle_root,
            "merkle_proof_verified": self.merkle_proof_verified,
            "signature": self.signature,
            "message": self.message,
        }


class Ledger:
    """Owns the blockchain instance and mediates every attendance write."""

    def __init__(
        self,
        config,
        store: Store,
        repository: Repository,
        keystore: KeyStore,
    ) -> None:
        self.config = config
        self.store = store
        self.repo = repository
        self.keystore = keystore
        self.chain = Blockchain(
            difficulty=config.chain.difficulty,
            node_name=config.chain.node_name,
            seal_threshold=config.chain.seal_threshold,
        )
        self._rate_buckets: dict[str, list[float]] = {}
        self._device_sessions: dict[tuple[str, str], str] = {}  # (session, device) -> roll
        self.load_from_store()

    # ==================================================================
    # Persistence
    # ==================================================================
    def load_from_store(self) -> None:
        """Rebuild the in-memory chain from the active backend at startup."""
        blocks = self.repo.list_blocks()
        if not blocks:
            # First run: make sure the genesis block is written, otherwise the
            # root of trust would be lost on the next restart.
            self.persist_block(self.chain.chain[0])
            log.info("Empty ledger -- persisted the genesis block")
            return

        stored_indexes = {int(block.get("index", -1)) for block in blocks}
        self.chain.load_blocks(blocks)
        # Warm the chain's own signature cache so validation -- including the
        # tamper demo -- does not re-verify thousands of signatures every time.
        self.chain.signature_cache = self._load_signature_cache()

        # If an older ledger was written before the genesis block persisted
        # itself, repair it now. Genesis is deterministic, so rebuilding it
        # restores the exact hash that block 1 already links back to.
        if 0 not in stored_indexes:
            log.warning("Genesis block was missing from storage; restored it")
        self.persist_block(self.chain.chain[0])
        log.info("Loaded %d blocks from storage", len(self.chain.chain))

    def persist_block(self, block) -> None:
        """Write a sealed block, and mark its records as confirmed."""
        self.repo.save_block(block.to_dict())
        self.repo.set_meta("chain_head", {"index": block.index, "hash": block.hash})
        self._index_sealed_block(block)

    def _index_sealed_block(self, block) -> int:
        """Stamp every attendance record in a freshly sealed block.

        Records are written the moment a student scans, with ``on_chain: False``,
        because the block does not exist yet. Without this step the stored copy
        would keep saying "not on the chain" forever -- the receipt would show
        confirmed while the student's own page said pending.
        """
        updated: list[dict[str, Any]] = []
        for tx in block.transactions:
            if tx.tx_type != TX_ATTENDANCE:
                continue
            record = self.repo.get_attendance(tx.tx_id)
            if record is None:
                # The block is authoritative; rebuild the index entry from it.
                updated.append(self._index_record(tx, block))
            else:
                updated.append(
                    {
                        **record,
                        "block_index": block.index,
                        "block_hash": block.hash,
                        "merkle_root": block.merkle_root,
                        "on_chain": True,
                    }
                )
        if updated:
            self.repo.save_attendance_many(updated)
        return len(updated)

    def rebuild_attendance_index(self) -> int:
        """Recreate the query index from the authoritative chain."""
        records: list[dict[str, Any]] = []
        for block in self.chain.chain:
            for tx in block.transactions:
                if tx.tx_type != "ATTENDANCE":
                    continue
                records.append(self._index_record(tx, block))
        if records:
            self.repo.save_attendance_many(records)
        return len(records)

    def _index_record(self, tx, block) -> dict[str, Any]:
        payload = tx.payload
        return {
            "tx_id": tx.tx_id,
            "session_id": payload.get("session_id"),
            "student_roll": payload.get("student_roll"),
            "student_name": payload.get("student_name"),
            "subject_code": payload.get("subject_code"),
            "subject_name": payload.get("subject_name"),
            "faculty_id": payload.get("faculty_id"),
            "room": payload.get("room"),
            "status": payload.get("status"),
            "marked_at": payload.get("marked_at"),
            "session_started_at": payload.get("session_started_at"),
            "device_fingerprint": payload.get("device_fingerprint"),
            "signature": tx.signature,
            "public_key": tx.sender_pubkey,
            "block_index": block.index,
            "block_hash": block.hash,
            "merkle_root": block.merkle_root,
            "on_chain": True,
        }

    # ==================================================================
    # Sessions
    # ==================================================================
    def open_session(
        self,
        *,
        subject_code: str,
        faculty_id: str,
        room: str = "",
        duration_minutes: int | None = None,
        division: str = "A",
        year: str = "TE",
        note: str = "",
    ) -> dict[str, Any]:
        """Start a roll-call window and mint its rotating-QR secret."""
        subject = self.repo.get_subject(subject_code)
        if subject is None:
            raise LedgerError(f"Unknown subject code {subject_code}", "UNKNOWN_SUBJECT", 404)

        faculty = self.repo.get_faculty(faculty_id)
        if faculty is None:
            raise LedgerError(f"Unknown faculty id {faculty_id}", "UNKNOWN_FACULTY", 404)

        existing = self.repo.open_session_for_faculty(faculty_id)
        if existing:
            raise LedgerError(
                f"Session {existing['session_id']} is already open. Close it first.",
                "SESSION_ALREADY_OPEN",
                409,
            )

        now = time.time()
        duration = duration_minutes or self.config.session.session_default_minutes
        session_id = self._session_id(subject_code, division, now)

        session = {
            "session_id": session_id,
            "subject_code": subject_code,
            "subject_name": subject.get("name", subject_code),
            "faculty_id": faculty_id,
            "faculty_name": faculty.get("name", faculty_id),
            "department": subject.get("department", self.config.college.department),
            "semester": subject.get("semester"),
            "division": division,
            "year": year,
            "room": room or subject.get("default_room", "Classroom"),
            "started_at": now,
            "expires_at": now + duration * 60,
            "qr_ttl": self.config.session.qr_token_ttl,
            "grace_seconds": self.config.session.grace_seconds,
            "secret": qr.new_session_secret(),
            "status": "OPEN",
            "note": note,
            "closed_at": None,
            "block_index": None,
            "block_hash": None,
            "merkle_root": None,
            "present_count": 0,
            "late_count": 0,
            "marked_count": 0,
            "expected_count": len(
                self.repo.list_students(
                    year=year, division=division, active_only=True
                )
            ),
            "created_at": now,
        }
        self.repo.save_session(session)
        self.repo.log_audit(
            "SESSION_OPEN",
            actor=faculty_id,
            target=session_id,
            detail={"subject": subject_code, "room": session["room"], "division": division},
        )
        return session

    def _session_id(self, subject_code: str, division: str, now: float) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        return f"LEC-{stamp}-{subject_code}-{division}-{secrets.token_hex(2)}"

    def close_session(self, session_id: str, *, actor: str = "faculty") -> dict[str, Any]:
        """End the roll-call and seal every pending transaction into one block."""
        session = self._require_session(session_id)

        block = None
        if self.chain.mempool:
            block = self.chain.mine_pending(note=f"Roll call: {session['subject_code']}")
            if block is not None:
                self.persist_block(block)

        records = self.repo.attendance_for_session(session_id)
        updates: dict[str, Any] = {
            "status": "CLOSED",
            "closed_at": time.time(),
            "present_count": sum(1 for r in records if r["status"] == STATUS_PRESENT),
            "late_count": sum(1 for r in records if r["status"] == STATUS_LATE),
            "marked_count": len(records),
        }
        if block is not None:
            updates.update(
                {
                    "block_index": block.index,
                    "block_hash": block.hash,
                    "merkle_root": block.merkle_root,
                    "mining_stats": block.mining_stats,
                }
            )
        self.repo.update_session(session_id, updates)
        self.repo.log_audit(
            "SESSION_CLOSE",
            actor=actor,
            target=session_id,
            detail={
                "marked": updates["marked_count"],
                "block_index": updates.get("block_index"),
                "block_hash": updates.get("block_hash"),
            },
        )
        result = self.repo.get_session(session_id) or session
        result["sealed_block"] = block.to_dict(include_transactions=False) if block else None
        return result

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = self.repo.get_session(session_id)
        if session is None:
            raise LedgerError(f"Unknown session {session_id}", "UNKNOWN_SESSION", 404)
        return session

    def current_qr(self, session_id: str) -> dict[str, Any]:
        """The QR payload for right now -- the display polls this to refresh."""
        session = self._require_session(session_id)
        if session["status"] != "OPEN":
            raise LedgerError("This session is closed", "SESSION_CLOSED", 409)
        if time.time() > float(session.get("expires_at", 0)):
            self.repo.update_session(session_id, {"status": "EXPIRED"})
            raise LedgerError("This session has timed out", "SESSION_EXPIRED", 409)

        token = qr.issue_token(
            session_id, session["secret"], int(session.get("qr_ttl", 30))
        )
        share_url = f"/scan?token={token.token}"
        return {
            "session_id": session_id,
            "subject_code": session["subject_code"],
            "subject_name": session["subject_name"],
            "faculty_name": session.get("faculty_name"),
            "room": session.get("room"),
            "token": token.token,
            "share_url": share_url,
            "slot": token.slot,
            "issued_at": token.issued_at,
            "expires_in": token.expires_in,
            "ttl": token.ttl,
            "server_time": time.time(),
            "session_started_at": session.get("started_at"),
            "grace_seconds": session.get("grace_seconds"),
            "marked_count": session.get("marked_count", 0),
            "expected_count": session.get("expected_count", 0),
        }

    # ==================================================================
    # Marking attendance
    # ==================================================================
    def mark_attendance(
        self,
        *,
        token: str,
        session_id: str,
        roll_no: str,
        device_fingerprint: str = "",
        client_ip: str = "",
        client_latency_ms: int | None = None,
        non_custodial_signature: str | None = None,
        non_custodial_public_key: str | None = None,
    ) -> MarkResult:
        """The main entry point. Raises :class:`LedgerError` on any rejection."""
        session = self._require_session(session_id)
        subject_code = session["subject_code"]

        # -- 1. session must be open and not expired --------------------
        if session["status"] != "OPEN":
            raise LedgerError(
                f"Attendance for {subject_code} is closed", "SESSION_CLOSED", 409
            )
        now = time.time()
        if now > float(session.get("expires_at", 0)):
            self.repo.update_session(session_id, {"status": "EXPIRED"})
            raise LedgerError("This session has timed out", "SESSION_EXPIRED", 409)

        # -- 2. the QR token must be genuine and fresh -----------------
        try:
            token_info = qr.verify_token(
                token,
                secret=session["secret"],
                session_id=session_id,
                ttl=int(session.get("qr_ttl", 30)),
                at=now,
            )
        except qr.QRTokenError as exc:
            self.repo.log_audit(
                "QR_REJECTED",
                actor=roll_no,
                target=session_id,
                detail={"code": exc.code, "reason": str(exc)},
            )
            raise LedgerError(str(exc), exc.code, 400) from exc

        # -- 3. the student must exist and belong to this division -----
        student_roll = str(roll_no).strip().upper()
        student = self.repo.get_student(student_roll)
        if student is None:
            raise LedgerError(
                f"Roll number {student_roll} is not registered", "UNKNOWN_STUDENT", 404
            )
        if not student.get("active", True):
            raise LedgerError(
                "This student record is inactive", "STUDENT_INACTIVE", 403
            )
        if student.get("year") != session.get("year") or student.get("division") != session.get(
            "division"
        ):
            raise LedgerError(
                f"{student_roll} is not enrolled in {session.get('year')}-"
                f"{session.get('division')} for {subject_code}",
                "NOT_ENROLLED",
                403,
            )

        # -- 4. anti-abuse ---------------------------------------------
        self._rate_limit(f"ip:{client_ip}", self.config.security.max_marks_per_minute_per_ip)
        self._rate_limit(
            f"student:{student_roll}",
            self.config.security.max_marks_per_minute_per_student,
        )
        self._check_shared_device(session_id, device_fingerprint, student_roll)

        # -- 5. no duplicate marks -------------------------------------
        if self.chain.is_duplicate_attendance(student_roll, session_id):
            raise LedgerError(
                "Your attendance is already recorded for this lecture",
                "ALREADY_MARKED",
                409,
            )

        # -- 6. decide PRESENT vs LATE ---------------------------------
        elapsed = now - float(session.get("started_at", now))
        grace = int(session.get("grace_seconds", self.config.session.grace_seconds))
        if elapsed <= grace:
            status = STATUS_PRESENT
        elif self.config.session.late_allowed:
            status = STATUS_LATE
        else:
            raise LedgerError(
                "The grace period for this lecture has passed", "TOO_LATE", 403
            )

        # -- 7. build, sign and submit the transaction -----------------
        identity = self.keystore.get(student_roll)
        if non_custodial_signature:
            transaction = self._build_and_attach_signature(
                session=session,
                student=student,
                status=status,
                now=now,
                device_fingerprint=device_fingerprint,
                client_latency_ms=client_latency_ms,
                public_key=non_custodial_public_key or student.get("public_key", ""),
                signature=non_custodial_signature,
            )
        else:
            if identity is None:
                raise LedgerError(
                    "No signing key registered for this roll number",
                    "NO_IDENTITY",
                    403,
                )
            transaction = build_attendance_transaction(
                session_id=session_id,
                student_roll=student_roll,
                student_name=student.get("name", student_roll),
                subject_code=subject_code,
                subject_name=session.get("subject_name", subject_code),
                faculty_id=session["faculty_id"],
                room=session.get("room", ""),
                status=status,
                marks_at=now,
                session_started_at=float(session.get("started_at", now)),
                grace_seconds=grace,
                device_fingerprint=device_fingerprint,
                client_latency_ms=client_latency_ms,
                nonce=qr.token_fingerprint(token),
            )
            transaction.sender_pubkey = identity.public_key
            transaction.sign(identity.private_key)
            transaction.tx_id = transaction.compute_id()

        accepted, reason = self.chain.add_transaction(transaction)
        if not accepted:
            raise LedgerError(f"Transaction rejected: {reason}", "TX_REJECTED", 409)

        block = None
        if self.chain.auto_seal and len(self.chain.mempool) >= self.chain.seal_threshold:
            block = self.chain.mine_pending(note=f"Roll call: {subject_code} (batch)")
            if block is not None:
                self.persist_block(block)

        index_record = self._index_record(transaction, block) if block else {
            "tx_id": transaction.tx_id,
            "session_id": session_id,
            "student_roll": student_roll,
            "student_name": student.get("name", student_roll),
            "subject_code": subject_code,
            "subject_name": session.get("subject_name", subject_code),
            "faculty_id": session["faculty_id"],
            "room": session.get("room", ""),
            "status": status,
            "marked_at": now,
            "session_started_at": session.get("started_at"),
            "device_fingerprint": device_fingerprint,
            "signature": transaction.signature,
            "public_key": transaction.sender_pubkey,
            "block_index": None,
            "block_hash": None,
            "merkle_root": None,
            "on_chain": False,
        }
        self.repo.save_attendance(index_record)

        # -- 8. keep session counters current --------------------------
        marked = self.repo.attendance_for_session(session_id)
        self.repo.update_session(
            session_id,
            {
                "marked_count": len(marked),
                "present_count": sum(1 for r in marked if r["status"] == STATUS_PRESENT),
                "late_count": sum(1 for r in marked if r["status"] == STATUS_LATE),
            },
        )

        self.repo.log_audit(
            "ATTENDANCE_MARKED",
            actor=student_roll,
            target=session_id,
            detail={
                "tx_id": transaction.tx_id,
                "status": status,
                "age_seconds": token_info["age_seconds"],
                "block_index": block.index if block else None,
            },
        )

        return MarkResult(
            ok=True,
            tx_id=transaction.tx_id,
            status=status,
            student_roll=student_roll,
            student_name=student.get("name", student_roll),
            session_id=session_id,
            subject_code=subject_code,
            marked_at=now,
            block_index=block.index if block else None,
            block_hash=block.hash if block else None,
            merkle_root=block.merkle_root if block else None,
            merkle_proof_verified=None,
            signature=transaction.signature,
            message=(
                "Attendance recorded and sealed in block "
                f"{block.index} of the attendance blockchain."
                if block
                else "Attendance recorded. It will be sealed into the next block "
                "when the lecture ends."
            ),
        )

    def _build_and_attach_signature(
        self,
        *,
        session: dict[str, Any],
        student: dict[str, Any],
        status: str,
        now: float,
        device_fingerprint: str,
        client_latency_ms: int | None,
        public_key: str,
        signature: str,
    ):
        """Non-custodial mode: the student's device already signed the payload."""
        from ..blockchain import ecdsa

        transaction = build_attendance_transaction(
            session_id=session["session_id"],
            student_roll=student["roll_no"],
            student_name=student.get("name", student["roll_no"]),
            subject_code=session["subject_code"],
            subject_name=session.get("subject_name", session["subject_code"]),
            faculty_id=session["faculty_id"],
            room=session.get("room", ""),
            status=status,
            marks_at=now,
            session_started_at=float(session.get("started_at", now)),
            grace_seconds=int(session.get("grace_seconds", 300)),
            device_fingerprint=device_fingerprint,
            client_latency_ms=client_latency_ms,
            nonce="",
        )
        transaction.sender_pubkey = public_key
        transaction.signature = signature
        transaction.tx_id = transaction.compute_id()
        if not ecdsa.verify_signature(public_key, transaction.signing_bytes(), signature):
            raise LedgerError(
                "The signature on this transaction does not verify",
                "BAD_SIGNATURE",
                400,
            )
        return transaction

    # ------------------------------------------------------------------
    # Anti-abuse helpers
    # ------------------------------------------------------------------
    def _rate_limit(self, bucket: str, limit: int, window: float = 60.0) -> None:
        """Sliding-window limiter. Cheap, and enough to stop scripted abuse."""
        now = time.time()
        history = [t for t in self._rate_buckets.get(bucket, []) if now - t < window]
        if len(history) >= limit:
            raise LedgerError(
                "Too many attendance attempts. Please wait a moment.",
                "RATE_LIMITED",
                429,
            )
        history.append(now)
        self._rate_buckets[bucket] = history

    def _check_shared_device(
        self, session_id: str, device_fingerprint: str, student_roll: str
    ) -> None:
        """One device cannot mark several students in the same lecture.

        Proxy marking is the number-one real-world attendance fraud. A laptop or
        phone that submits roll numbers for two different students inside one
        session is almost certainly doing exactly that.
        """
        if not device_fingerprint or not self.config.security.bind_device_on_first_use:
            return
        key = (session_id, device_fingerprint)
        previous = self._device_sessions.get(key)
        if previous and previous != student_roll:
            self.repo.log_audit(
                "SHARED_DEVICE_BLOCKED",
                actor=student_roll,
                target=session_id,
                detail={"device": device_fingerprint, "also_used_by": previous},
            )
            raise LedgerError(
                "This device has already marked attendance for another student "
                f"({previous}) in this lecture. Proxy marking is not allowed.",
                "SHARED_DEVICE",
                403,
            )
        self._device_sessions[key] = student_roll

    # ==================================================================
    # Manual override (faculty-initiated corrections)
    # ==================================================================
    def manual_mark(
        self,
        *,
        session_id: str,
        roll_no: str,
        status: str,
        faculty_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Faculty marks a student by hand -- always recorded with a reason.

        Manual corrections are legitimate (a student's phone died, the QR reader
        failed) but they are the obvious corruption vector. So they are signed by
        the *faculty* key, tagged ``MANUAL``, carry a mandatory reason, and go on
        the same immutable chain.
        """
        if not self.config.security.allow_manual_override:
            raise LedgerError("Manual override is disabled", "OVERRIDE_DISABLED", 403)
        if self.config.security.manual_override_requires_reason and not reason.strip():
            raise LedgerError(
                "A reason is required for a manual override",
                "REASON_REQUIRED",
                400,
            )
        if status not in {STATUS_PRESENT, STATUS_LATE, STATUS_ABSENT}:
            raise LedgerError(f"Invalid status {status}", "BAD_STATUS", 400)

        session = self._require_session(session_id)
        student = self.repo.get_student(str(roll_no).upper())
        if student is None:
            raise LedgerError(f"Unknown roll number {roll_no}", "UNKNOWN_STUDENT", 404)

        identity = self.keystore.get(faculty_id)
        if identity is None:
            raise LedgerError("Unknown faculty identity", "NO_IDENTITY", 403)

        now = time.time()
        transaction = build_attendance_transaction(
            session_id=session_id,
            student_roll=student["roll_no"],
            student_name=student.get("name", student["roll_no"]),
            subject_code=session["subject_code"],
            subject_name=session.get("subject_name", session["subject_code"]),
            faculty_id=faculty_id,
            room=session.get("room", ""),
            status=status,
            marks_at=now,
            session_started_at=float(session.get("started_at", now)),
            grace_seconds=0,
            device_fingerprint="",
            nonce=f"manual-{secrets.token_hex(4)}",
        )
        transaction.payload["status"] = STATUS_MANUAL
        transaction.payload["manual"] = True
        transaction.payload["manual_status"] = status
        transaction.payload["manual_reason"] = reason.strip()
        transaction.payload["override_by"] = faculty_id
        transaction.sender_pubkey = identity.public_key
        transaction.sign(identity.private_key)
        transaction.tx_id = transaction.compute_id()

        accepted, why = self.chain.add_transaction(transaction, verify=True)
        if not accepted:
            raise LedgerError(f"Rejected: {why}", "TX_REJECTED", 409)

        record = {
            "tx_id": transaction.tx_id,
            "session_id": session_id,
            "student_roll": student["roll_no"],
            "student_name": student.get("name"),
            "subject_code": session["subject_code"],
            "status": STATUS_MANUAL,
            "manual_status": status,
            "manual_reason": reason,
            "override_by": faculty_id,
            "marked_at": now,
            "signature": transaction.signature,
            "public_key": transaction.sender_pubkey,
            "on_chain": False,
            "block_index": None,
            "block_hash": None,
            "merkle_root": None,
        }
        self.repo.save_attendance(record)
        self.repo.log_audit(
            "MANUAL_OVERRIDE",
            actor=faculty_id,
            target=session_id,
            detail={"roll_no": student["roll_no"], "status": status, "reason": reason},
        )
        return {"ok": True, "tx_id": transaction.tx_id, "record": record}

    # ==================================================================
    # Verification & audit
    # ==================================================================
    # ------------------------------------------------------------------
    # Signature audit cache
    # ------------------------------------------------------------------
    #: Blocks whose ECDSA signatures have already been verified. Keyed by block
    #: hash, so a modified block can never hit a stale cache entry.
    SIGNATURE_CACHE_META = "signature_verification_cache"

    def _load_signature_cache(self) -> set[str]:
        cached = self.repo.get_meta(self.SIGNATURE_CACHE_META, []) or []
        return set(cached)

    def _save_signature_cache(self, cache: set[str]) -> None:
        # Keep it bounded; the oldest entries are the least useful.
        trimmed = list(cache)[-20000:]
        self.repo.set_meta(self.SIGNATURE_CACHE_META, trimmed)

    def signature_audit_status(self) -> dict[str, Any]:
        cache = self._load_signature_cache()
        total = sum(1 for block in self.chain.chain if block.transactions)
        verified = sum(
            1 for block in self.chain.chain if block.transactions and block.hash in cache
        )
        return {
            "blocks_with_transactions": total,
            "blocks_signature_verified": verified,
            "blocks_pending": total - verified,
            "complete": verified >= total,
            "signatures_on_chain": self.chain.sealed_attendance_count()
            + sum(
                1
                for block in self.chain.chain
                for tx in block.transactions
                if tx.tx_type == "ANCHOR"
            ),
        }

    def verify_chain(self, *, deep: bool = False, parallel: bool = True) -> dict[str, Any]:
        """Validate the chain.

        ``deep=False`` (default) runs every structural check instantly.
        ``deep=True`` additionally re-verifies ECDSA signatures, skipping blocks
        already present in the verification cache -- which is what makes a full
        cryptographic audit affordable on a college laptop.
        """
        cache = self._load_signature_cache() if deep else set()
        report = self.chain.validate(
            deep=deep, signature_cache=cache, parallel=parallel and deep
        )

        if deep and report.signature_blocks_verified:
            cache.update(report.signature_blocks_verified)
            self._save_signature_cache(cache)
            self.chain.signature_cache = cache

        payload = report.to_dict()
        payload["signature_audit"] = self.signature_audit_status()
        self.repo.log_audit(
            "CHAIN_VERIFIED",
            actor="system",
            target="chain",
            detail={
                "valid": report.valid,
                "blocks": report.checked_blocks,
                "issues": report.critical_count,
                "deep": deep,
                "signatures_checked": report.signatures_checked,
            },
        )
        return payload

    def receipt(self, tx_id: str) -> dict[str, Any]:
        """Full audit receipt for one attendance transaction."""
        found = self.chain.find_transaction(tx_id)
        if found is None:
            record = self.repo.get_attendance(tx_id)
            if record is None:
                raise LedgerError(f"Unknown transaction {tx_id}", "UNKNOWN_TX", 404)
            return {
                "tx_id": tx_id,
                "on_chain": False,
                "record": record,
                "message": "Recorded but not yet sealed into a block.",
            }

        block, tx = found
        proof = self.chain.proof_of_inclusion(tx_id)
        return {
            "tx_id": tx_id,
            "on_chain": True,
            "record": self._index_record(tx, block),
            "block": block.to_dict(include_transactions=False),
            "transaction": tx.to_dict(),
            "signature_valid": tx.verify_signature(),
            "tx_id_valid": tx.verify_id(),
            "merkle_proof": proof,
            "merkle_proof_verified": bool(proof and proof["verified"]),
            "block_pow_valid": block.pow_is_valid(),
        }

    def seal_now(self, note: str = "Manual seal", *, actor: str = "faculty") -> dict[str, Any]:
        """Force-seal the mempool (used by the live demo)."""
        if not self.chain.mempool:
            return {"ok": False, "message": "Nothing to seal -- the mempool is empty"}
        block = self.chain.mine_pending(note=note)
        if block is None:
            return {"ok": False, "message": "Nothing to seal"}
        self.persist_block(block)
        self.repo.log_audit(
            "BLOCK_SEALED",
            actor=actor,
            target=str(block.index),
            detail={"hash": block.hash, "transactions": len(block.transactions)},
        )
        return {"ok": True, "block": block.to_dict(include_transactions=True)}

    def pending_root(self) -> str:
        """Merkle root of everything currently in the mempool."""
        return compute_merkle_root([tx.tx_id for tx in self.chain.mempool])

    #: Highest difficulty allowed from the web UI. Expected mining work multiplies
    #: by 16 per step, so difficulty 6 would freeze a lecturer's browser for
    #: minutes the next time a block sealed. The CLI and config file are not
    #: limited, because `manage.py bench` is where you show the scaling.
    MAX_LIVE_DIFFICULTY = 5

    def set_difficulty(self, difficulty: int, *, actor: str = "admin") -> dict[str, Any]:
        requested = int(difficulty)
        difficulty = max(1, min(self.MAX_LIVE_DIFFICULTY, requested))
        self.chain.force_mining_difficulty(difficulty)
        self.config.chain.difficulty = difficulty
        self.repo.log_audit(
            "DIFFICULTY_CHANGED", actor=actor, target="chain", detail={"difficulty": difficulty}
        )
        return {
            "ok": True,
            "difficulty": difficulty,
            "requested_difficulty": requested,
            # Never silently accept a different value than the one asked for.
            "clamped": difficulty != requested,
            "note": (
                f"Live difficulty is capped at {self.MAX_LIVE_DIFFICULTY}: each step "
                "multiplies the expected mining work by 16. Use "
                "`python manage.py bench` to demonstrate larger targets."
                if difficulty != requested
                else None
            ),
            "auto_seal": self.chain.auto_seal,
            "expected_attempts": estimate_attempts(difficulty),
        }


# ============================================================================
# Anchor service (publishing Merkle roots)
# ============================================================================
#
# Anchor service -- decides *what* to anchor and records the proof.
#
# Two roots are involved, and conflating them is a common mistake:
#
# ``records_root``
#     The Merkle root of every attendance transaction sealed since the previous
#     anchor. This is the value that actually goes to the public ledger, and it is
#     what makes "these exact records existed at this moment" provable.
#
# ``anchor_tx_id``
#     The hash of the anchor transaction that we write into *our own* chain. That
#     transaction's payload contains ``records_root`` plus the block range and a
#     link to the previous anchor root, so our chain carries an auditable record
#     of every public commitment.
#
# The anchor chain (``previous_anchor_root``) is what prevents an attacker from
# quietly *deleting* an inconvenient anchor: anchors form a hash-linked chain of
# their own, and each one is committed to a block of the main chain.

  # noqa: F401  (re-exported types)



class AnchorService:
    """Creates anchor transactions and submits roots to the configured provider."""

    def __init__(self, config, repository: Repository, ledger: Ledger, keystore: KeyStore) -> None:
        self.config = config
        self.repo = repository
        self.ledger = ledger
        self.keystore = keystore
        self.provider = build_provider(config)

    # ------------------------------------------------------------------
    def _attendance_tx_ids(self, from_block: int, to_block: int) -> list[str]:
        """Attendance transaction ids inside a block range, in chain order.

        Order matters: the Merkle root is only reproducible if both the anchoring
        and the verifying side walk the chain the same way.
        """
        tx_ids: list[str] = []
        for block in self.ledger.chain.chain:
            if from_block <= block.index <= to_block:
                tx_ids.extend(
                    tx.tx_id for tx in block.transactions if tx.tx_type == TX_ATTENDANCE
                )
        return tx_ids

    def _pending_range(self) -> tuple[int, int, list[str]]:
        """Block range and transaction ids sealed since the last anchor."""
        latest = self.repo.latest_anchor()
        from_block = int(latest.get("to_block", 0)) + 1 if latest else 1
        to_block = self.ledger.chain.head.index
        return from_block, to_block, self._attendance_tx_ids(from_block, to_block)

    # ------------------------------------------------------------------
    def anchor_now(self, *, actor: str = "faculty", note: str = "") -> dict[str, Any]:
        """Seal pending records, publish the root, and store the proof."""
        institution = ensure_institution_identity(self.keystore)

        from_block, to_block, tx_ids = self._pending_range()
        latest = self.repo.latest_anchor()
        previous_root = latest.get("merkle_root") if latest else None

        # Two modes, both fully reproducible by `verify_anchor`:
        #
        # INCREMENTAL  a range with new records -> root over exactly those records
        # CUMULATIVE   nothing new since the last anchor -> re-commit to *every*
        #              attendance record on the chain. Still a meaningful public
        #              commitment, and still independently verifiable, which a
        #              "root of whatever the head block happens to be" would not be.
        if tx_ids:
            mode = "INCREMENTAL"
            records_root = compute_merkle_root(tx_ids)
        else:
            mode = "CUMULATIVE"
            from_block = 1
            to_block = self.ledger.chain.head.index
            tx_ids = self._attendance_tx_ids(1, to_block)
            records_root = compute_merkle_root(tx_ids)
            note = note or (
                "No new attendance records since the last anchor; re-committing to "
                "every record on the chain"
            )
        anchor_id = (
            f"ANC-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        )
        created_at = time.time()

        # -- 1. write the anchor transaction into our own chain ---------
        transaction = build_anchor_transaction(
            anchor_id=anchor_id,
            merkle_root=records_root,
            previous_anchor_root=previous_root,
            from_block=from_block,
            to_block=max(to_block, from_block - 1),
            tx_count=len(tx_ids),
            created_at=created_at,
            chain_name="BCOE-ATTENDANCE-CHAIN",
        )
        transaction.payload["note"] = note
        transaction.payload["provider"] = self.provider.name
        transaction.sender_pubkey = institution.public_key
        transaction.sign(institution.private_key)
        transaction.tx_id = transaction.compute_id()

        accepted, reason = self.ledger.chain.add_transaction(transaction)
        if not accepted:
            raise LedgerError(f"Anchor transaction rejected: {reason}", "ANCHOR_REJECTED", 409)

        # Sealed immediately, regardless of difficulty, so the anchor is on-chain
        # before we hand the root to an external provider.
        seal_block = self.ledger.chain.mine_pending(
            note=f"Anchor checkpoint {anchor_id}", miner=institution.address
        )
        if seal_block is not None:
            self.ledger.persist_block(seal_block)

        # -- 2. publish the root to the chosen provider ----------------
        receipt = self.provider.submit(
            records_root,
            {
                "anchor_id": anchor_id,
                "previous_anchor_root": previous_root,
                "block_index": seal_block.index if seal_block else None,
                "created_at": created_at,
                "tx_count": len(tx_ids),
            },
        )

        record = {
            **receipt.to_dict(),
            "previous_anchor_root": previous_root,
            "mode": mode,
            "from_block": from_block,
            "to_block": max(to_block, from_block - 1),
            "records_anchored": len(tx_ids),
            "anchor_tx_id": transaction.tx_id,
            "anchor_block_index": seal_block.index if seal_block else None,
            "anchor_block_hash": seal_block.hash if seal_block else None,
            "institution_address": institution.address,
            "note": note,
        }
        self.repo.save_anchor(record)
        self.repo.log_audit(
            "ANCHOR_CREATED",
            actor=actor,
            target=anchor_id,
            detail={
                "provider": receipt.provider,
                "status": receipt.status,
                "root": records_root,
                "records": len(tx_ids),
                "independent": receipt.independent,
            },
        )
        return record

    # ------------------------------------------------------------------
    def verify_anchor(self, anchor_id: str) -> dict[str, Any]:
        """Re-check an anchor against the chain, without trusting the stored record."""
        anchor = self.repo.get_anchor(anchor_id)
        if anchor is None:
            raise LedgerError(f"Unknown anchor {anchor_id}", "UNKNOWN_ANCHOR", 404)

        from_block = int(anchor.get("from_block", 1))
        to_block = int(anchor.get("to_block", 0))
        stored_root = anchor.get("merkle_root")

        # The committed set is exactly the attendance records in the stored block
        # range -- re-derive them from the chain in the same order, so the root is
        # genuinely recomputed rather than read back from the anchor record.
        tx_ids = self._attendance_tx_ids(from_block, to_block)
        recomputed = compute_merkle_root(tx_ids) if tx_ids else None

        found = self.ledger.chain.find_transaction(anchor.get("anchor_tx_id", ""))
        signature_ok = bool(found and found[1].verify_signature())

        checks = [
            {
                "check": "Merkle root recomputed from the chain",
                "expected": stored_root,
                "actual": recomputed,
                "passed": (recomputed == stored_root) if recomputed else None,
                "detail": (
                    f"{len(tx_ids)} attendance records in blocks "
                    f"{from_block}–{to_block} ({anchor.get('mode', 'INCREMENTAL')} anchor)"
                ),
            },
            {
                "check": "Anchor transaction found on the chain",
                "expected": anchor.get("anchor_tx_id"),
                "actual": found[1].tx_id if found else None,
                "passed": bool(found),
            },
            {
                "check": "Anchor signed by the institutional key",
                "expected": anchor.get("institution_address"),
                "actual": anchor.get("institution_address"),
                "passed": signature_ok,
            },
        ]
        return {
            "anchor_id": anchor_id,
            "provider": anchor.get("provider"),
            "status": anchor.get("status"),
            "independent": anchor.get("independent"),
            "records_anchored": anchor.get("records_anchored"),
            "merkle_root": anchor.get("merkle_root"),
            "checks": checks,
            "verified": all(check["passed"] for check in checks if check["passed"] is not None),
            "caveat": (
                "Offline verification proves the root matches our chain. Proving it "
                "was published externally requires the provider's own receipt "
                "(see the stored proof or the explorer link)."
            ),
        }

    # ------------------------------------------------------------------
    def pending_summary(self) -> dict[str, Any]:
        """How much is waiting to be anchored -- shown on the Anchoring page."""
        from_block, to_block, tx_ids = self._pending_range()
        latest = self.repo.latest_anchor()
        return {
            "provider": self.provider.name,
            "provider_info": self.provider.describe(),
            "pending_records": len(tx_ids),
            "from_block": from_block,
            "to_block": to_block,
            "pending_root": compute_merkle_root(tx_ids) if tx_ids else None,
            "last_anchor": latest,
            "anchor_count": len(self.repo.list_anchors()),
            "auto_anchor_enabled": False,
            "head_hash": self.ledger.chain.head.hash,
        }


# ============================================================================
# The service facade
# ============================================================================
#
# Service container -- one place that wires the application together.
#
# Layer map (bottom to top)::
#
#     storage          -> documents (local JSON | Firestore)
#     repository       -> domain queries over those documents
#     blockchain       -> blocks, transactions, Merkle trees, Proof-of-Work
#     identity         -> secp256k1 key pairs (custodial | non-custodial)
#     services         -> ledger, analytics, anomaly detection, anchoring
#     api              -> HTTP endpoints
#     templates/static -> the UI
#
# Constructing :class:`Services` once and sharing it keeps the Flask app thin and
# makes the whole system testable without a browser.


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
        # The store doubles as the keystore's cloud mirror: on a host with an
        # ephemeral filesystem, keys recover from Firestore just like the chain.
        self.keystore = KeyStore(keystore_file, self.store)
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
        from .storage import store_report

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
