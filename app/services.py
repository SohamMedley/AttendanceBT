"""
Service layer utilities: queries, keys, rotating QR, analytics and fraud.

Nothing here knows how HTTP or the web UI work. These modules sit on top of
the blockchain and storage layers and answer questions about the register --
who is enrolled, whether a QR token is genuine, which students are below the
75% bar, and which records look like proxy marking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import stat
import statistics
import time

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Any, Iterable

from .blockchain import ecdsa
from .storage import ANCHORS, ATTENDANCE, AUDIT, BLOCKS, FACULTY, META, SESSIONS, STUDENTS, SUBJECTS, Store


# ============================================================================
# The repository (queries over the document store)
# ============================================================================
#
# Domain repository: everything the application knows about the college register.
#
# Sits on top of the generic :class:`~app.storage.base.Store` document API so the
# same code runs against the local JSON backend and Firestore without changes.

class Repository:
    """Typed accessors for students, faculty, subjects, sessions and ledger data."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ==================================================================
    # Students
    # ==================================================================
    def upsert_student(self, student: dict[str, Any]) -> None:
        self.store.put(STUDENTS, student["roll_no"], student)

    def upsert_students(self, students: Iterable[dict[str, Any]]) -> int:
        return self.store.put_many(
            STUDENTS, {student["roll_no"]: student for student in students}
        )

    def get_student(self, roll_no: str) -> dict[str, Any] | None:
        return self.store.get(STUDENTS, str(roll_no).upper())

    def list_students(
        self,
        *,
        department: str | None = None,
        year: str | None = None,
        division: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if department:
            filters["department"] = department
        if year:
            filters["year"] = year
        if division:
            filters["division"] = division
        if active_only:
            filters["active"] = True
        students = self.store.list(STUDENTS, filters or None, order_by="roll_no")
        return students

    def student_count(self) -> int:
        return self.store.count(STUDENTS)

    # ==================================================================
    # Faculty
    # ==================================================================
    def upsert_faculty(self, faculty: dict[str, Any]) -> None:
        self.store.put(FACULTY, faculty["faculty_id"], faculty)

    def upsert_many_faculty(self, records: Iterable[dict[str, Any]]) -> int:
        return self.store.put_many(
            FACULTY, {record["faculty_id"]: record for record in records}
        )

    def get_faculty(self, faculty_id: str) -> dict[str, Any] | None:
        return self.store.get(FACULTY, faculty_id)

    def list_faculty(self, department: str | None = None) -> list[dict[str, Any]]:
        filters = {"department": department} if department else None
        return self.store.list(FACULTY, filters, order_by="faculty_id")

    # ==================================================================
    # Subjects
    # ==================================================================
    def upsert_subject(self, subject: dict[str, Any]) -> None:
        self.store.put(SUBJECTS, subject["code"], subject)

    def upsert_many_subjects(self, subjects: Iterable[dict[str, Any]]) -> int:
        return self.store.put_many(
            SUBJECTS, {subject["code"]: subject for subject in subjects}
        )

    def get_subject(self, code: str) -> dict[str, Any] | None:
        return self.store.get(SUBJECTS, code)

    def list_subjects(
        self, *, semester: int | None = None, department: str | None = None
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if semester is not None:
            filters["semester"] = semester
        if department:
            filters["department"] = department
        return self.store.list(SUBJECTS, filters or None, order_by="code")

    def subjects_for_faculty(self, faculty_id: str) -> list[dict[str, Any]]:
        return self.store.list(
            SUBJECTS, {"faculty_ids": ("contains", faculty_id)}, order_by="code"
        )

    # ==================================================================
    # Sessions
    # ==================================================================
    def save_session(self, session: dict[str, Any]) -> None:
        self.store.put(SESSIONS, session["session_id"], session)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.store.get(SESSIONS, session_id)

    def update_session(self, session_id: str, fields: dict[str, Any]) -> None:
        self.store.update(SESSIONS, session_id, fields)

    def list_sessions(
        self,
        *,
        faculty_id: str | None = None,
        subject_code: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if faculty_id:
            filters["faculty_id"] = faculty_id
        if subject_code:
            filters["subject_code"] = subject_code
        if status:
            filters["status"] = status
        return self.store.list(
            SESSIONS, filters or None, limit=limit, order_by="started_at", descending=True
        )

    def open_session_for_faculty(self, faculty_id: str) -> dict[str, Any] | None:
        sessions = self.store.list(
            SESSIONS,
            {"faculty_id": faculty_id, "status": "OPEN"},
            order_by="started_at",
            descending=True,
        )
        return sessions[0] if sessions else None

    # ==================================================================
    # Attendance index (derived from the chain)
    # ==================================================================
    def save_attendance(self, record: dict[str, Any]) -> None:
        self.store.put(ATTENDANCE, record["tx_id"], record)

    def save_attendance_many(self, records: Iterable[dict[str, Any]]) -> int:
        return self.store.put_many(
            ATTENDANCE, {record["tx_id"]: record for record in records}
        )

    def get_attendance(self, tx_id: str) -> dict[str, Any] | None:
        return self.store.get(ATTENDANCE, tx_id)

    def attendance_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list(
            ATTENDANCE, {"session_id": session_id}, order_by="marked_at"
        )

    def attendance_for_student(
        self, roll_no: str, *, subject_code: str | None = None
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"student_roll": roll_no}
        if subject_code:
            filters["subject_code"] = subject_code
        return self.store.list(ATTENDANCE, filters, order_by="marked_at", descending=True)

    def attendance_all(self) -> list[dict[str, Any]]:
        return self.store.list(ATTENDANCE, order_by="marked_at", descending=True)

    def already_marked(self, session_id: str, roll_no: str) -> dict[str, Any] | None:
        rows = self.store.list(
            ATTENDANCE, {"session_id": session_id, "student_roll": roll_no}, limit=1
        )
        return rows[0] if rows else None

    # ==================================================================
    # Blocks / chain persistence
    # ==================================================================
    def save_block(self, block: dict[str, Any]) -> None:
        self.store.put(BLOCKS, str(block["index"]), block)

    def save_blocks(self, blocks: Iterable[dict[str, Any]]) -> int:
        return self.store.put_many(BLOCKS, {str(b["index"]): b for b in blocks})

    def get_block(self, index: int) -> dict[str, Any] | None:
        return self.store.get(BLOCKS, str(index))

    def list_blocks(self) -> list[dict[str, Any]]:
        blocks = self.store.list(BLOCKS)
        return sorted(blocks, key=lambda block: int(block.get("index", 0)))

    def block_count(self) -> int:
        return self.store.count(BLOCKS)

    # ==================================================================
    # Anchors
    # ==================================================================
    def save_anchor(self, anchor: dict[str, Any]) -> None:
        self.store.put(ANCHORS, anchor["anchor_id"], anchor)

    def get_anchor(self, anchor_id: str) -> dict[str, Any] | None:
        return self.store.get(ANCHORS, anchor_id)

    def list_anchors(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.store.list(
            ANCHORS, limit=limit, order_by="created_at", descending=True
        )

    def latest_anchor(self) -> dict[str, Any] | None:
        anchors = self.list_anchors(limit=1)
        return anchors[0] if anchors else None

    # ==================================================================
    # Audit trail
    # ==================================================================
    def log_audit(
        self,
        action: str,
        *,
        actor: str = "system",
        target: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an immutable-ish audit entry (tamper-evident once anchored)."""
        # The id must be unique, not merely "distinctive". An earlier version
        # derived the suffix from hash((action, actor, target)), so two entries
        # with the same action in the same millisecond produced the SAME id and
        # the second silently overwrote the first -- losing audit history, which
        # is the one thing an audit log must never do.
        entry = {
            "audit_id": f"AUD-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
            "action": action,
            "actor": actor,
            "target": target,
            "detail": detail or {},
            "at": time.time(),
        }
        self.store.put(AUDIT, entry["audit_id"], entry)
        return entry

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list(AUDIT, limit=limit, order_by="at", descending=True)

    # ==================================================================
    # Meta / counters
    # ==================================================================
    def set_meta(self, key: str, value: Any) -> None:
        self.store.put(META, key, {"key": key, "value": value, "updated_at": time.time()})

    def get_meta(self, key: str, default: Any = None) -> Any:
        record = self.store.get(META, key)
        return record.get("value", default) if record else default


# ============================================================================
# Identity and key custody
# ============================================================================
#
# Key management: who owns which secp256k1 key pair.
#
# Threat model (state this plainly in the report and the viva)
# -----------------------------------------------------------
# Every attendance transaction is signed with a **secp256k1 key pair**. The
# project ships in "custodial" mode: the server generates and holds a key pair per
# student in a local keystore so the system works on a college laptop with no
# wallet app installed.
#
# That is a deliberate, documented trade-off:
#
# * **What it does prove** -- after the fact, *nobody* (not the student, not the
#   faculty member, not the developer editing the database) can silently change a
#   record, because the record's signature and Merkle root would stop matching.
#   Tamper-*evidence* is fully preserved.
# * **What it does not prove** -- that the *student personally* pressed the button,
#   because the server holds their key. Non-repudiation against the student
#   themselves therefore requires the non-custodial mode.
#
# ``MODE_NON_CUSTODIAL`` is implemented for the flow that matters: the student's
# browser generates the key pair locally with the Web Crypto API (or our JS
# secp256k1), keeps the private key in ``localStorage``, and sends only a signed
# transaction. The server then verifies the signature and never sees the key. In
# that mode the system gives true non-repudiation.


MODE_CUSTODIAL = "custodial"
MODE_NON_CUSTODIAL = "non_custodial"

DEFAULT_KEYSTORE = "data/keystore/keys.json"


@dataclass
class Identity:
    """A key pair plus its derived public address."""

    owner_id: str
    role: str                    # "student" | "faculty" | "institution"
    private_key: str             # 64 hex chars
    public_key: str              # 66 hex chars (compressed SEC1)
    address: str                 # Base58Check address

    def to_public_dict(self) -> dict[str, Any]:
        """Never leak the private key to the API."""
        return {
            "owner_id": self.owner_id,
            "role": self.role,
            "public_key": self.public_key,
            "address": self.address,
        }


class KeyStore:
    """A JSON-file keystore for server-held (custodial) identities."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_KEYSTORE) -> None:
        self.path = Path(path)
        self._identities: dict[str, Identity] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        if self.path.is_file():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                for owner_id, record in raw.get("identities", {}).items():
                    self._identities[owner_id] = Identity(
                        owner_id=owner_id,
                        role=record.get("role", "student"),
                        private_key=record["private_key"],
                        public_key=record["public_key"],
                        address=record.get("address", ""),
                    )
            except (json.JSONDecodeError, KeyError) as exc:
                log.error("Keystore %s is unreadable (%s); starting empty", self.path, exc)
        self._loaded = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "warning": (
                "PRIVATE KEYS - development keystore. Never commit this file."
            ),
            "mode": MODE_CUSTODIAL,
            "identities": {
                owner_id: {
                    "role": identity.role,
                    "private_key": identity.private_key,
                    "public_key": identity.public_key,
                    "address": identity.address,
                }
                for owner_id, identity in self._identities.items()
            },
        }
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        os.replace(temporary, self.path)
        # Owner read/write only -- private keys must not be world-readable.
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - platform dependent (Windows)
            pass

    # ------------------------------------------------------------------
    def create(self, owner_id: str, role: str = "student") -> Identity:
        """Generate (or return an existing) identity for ``owner_id``."""
        self.load()
        existing = self._identities.get(owner_id)
        if existing is not None:
            return existing
        keypair = ecdsa.generate_keypair()
        identity = Identity(
            owner_id=owner_id,
            role=role,
            private_key=keypair.private_hex,
            public_key=keypair.public_hex,
            address=keypair.address,
        )
        self._identities[owner_id] = identity
        return identity

    def create_many(self, owner_ids: list[str], role: str = "student") -> list[Identity]:
        created = [self.create(owner_id, role) for owner_id in owner_ids]
        self.save()
        return created

    def get(self, owner_id: str) -> Identity | None:
        self.load()
        return self._identities.get(owner_id)

    def require(self, owner_id: str) -> Identity:
        identity = self.get(owner_id)
        if identity is None:
            raise KeyError(f"No key pair registered for {owner_id!r}")
        return identity

    def __len__(self) -> int:
        self.load()
        return len(self._identities)

    def all(self) -> list[Identity]:
        self.load()
        return list(self._identities.values())

    # ------------------------------------------------------------------
    def verify_ownership(self, owner_id: str, public_key: str, message: bytes, signature: str) -> bool:
        """Verify that ``signature`` over ``message`` belongs to this owner's key.

        Used in non-custodial mode: the browser signs, we check it against the
        public key the identity is registered with.
        """
        identity = self.get(owner_id)
        if identity is None or identity.public_key != public_key:
            return False
        return ecdsa.verify_signature(public_key, message, signature)

    def rotate(self, owner_id: str) -> Identity:
        """Replace an identity's key pair (e.g. after a device change)."""
        self.load()
        keypair = ecdsa.generate_keypair()
        identity = Identity(
            owner_id=owner_id,
            role=self._identities.get(owner_id, Identity(owner_id, "student", "", "", "")).role,
            private_key=keypair.private_hex,
            public_key=keypair.public_hex,
            address=keypair.address,
        )
        self._identities[owner_id] = identity
        self.save()
        return identity


# --------------------------------------------------------------------------
# Institutional signing identity -- signs anchor transactions
# --------------------------------------------------------------------------
INSTITUTION_OWNER_ID = "BCOE-INSTITUTION"


def get_key_store(path: str | os.PathLike[str] = DEFAULT_KEYSTORE) -> KeyStore:
    return KeyStore(path)


def ensure_institution_identity(store: KeyStore) -> Identity:
    """The college's own signing identity, used for anchor transactions."""
    identity = store.get(INSTITUTION_OWNER_ID)
    if identity is None:
        identity = store.create(INSTITUTION_OWNER_ID, role="institution")
        store.save()
    return identity


def generate_nonce(length: int = 16) -> str:
    return secrets.token_hex(length // 2)


# ============================================================================
# Rotating QR tokens
# ============================================================================
#
# Rotating QR attendance tokens.
#
# The problem
# -----------
# A static QR code stuck on the classroom wall is worthless: photograph it once and
# you can mark yourself present from home for the rest of the semester.
#
# The fix -- the same idea behind Google Authenticator
# ----------------------------------------------------
# The session holds a random 32-byte **secret** that never leaves the server. The
# displayed QR code is regenerated every ``ttl`` seconds (default 30) from
#
#     HMAC-SHA256(session_secret, session_id | time_slot | version)
#
# Because the payload is *time-bound* and *signed*, an attacker cannot forge one
# without the secret, and a screenshot stops working once its slot expires. This is
# a TOTP (RFC 6238) construction in spirit: a shared secret plus a time counter
# produces a value that is valid only for a short window.
#
# Two-layer design
# ----------------
# 1. **Slot signature** -- unforgeable, expires automatically.
# 2. **Server-side session state** -- the session can be closed at any moment, and
#    the signing secret is unique per session, so token reuse across lectures is
#    impossible.
#
# Accepted clock skew is plus/minus one slot, so a student who scans at the exact
# moment of rotation is never wrongly rejected. That yields an effective validity
# window of ``ttl``..``3*ttl`` seconds rather than exactly ``ttl``.

TOKEN_VERSION = 1
SKEW_SLOTS = 1


class QRTokenError(ValueError):
    """Raised when a scanned token is invalid, expired, or forged."""

    def __init__(self, message: str, code: str = "INVALID_TOKEN") -> None:
        super().__init__(message)
        self.code = code


def new_session_secret() -> str:
    """32 random bytes, hex encoded. Unique per lecture session."""
    return secrets.token_hex(32)


def current_slot(ttl: int, at: float | None = None) -> int:
    """The current time-slot index. Changes every ``ttl`` seconds."""
    return int((at if at is not None else time.time()) // max(1, ttl))


def _signing_message(session_id: str, slot: int, version: int = TOKEN_VERSION) -> bytes:
    return f"BCOE-ATT|v{version}|{session_id}|{slot}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(secret: str, session_id: str, slot: int, version: int = TOKEN_VERSION) -> str:
    digest = hmac.new(
        bytes.fromhex(secret), _signing_message(session_id, slot, version), hashlib.sha256
    ).digest()
    return _b64url(digest[:16])  # 128-bit MAC is plenty for a 30-second window


@dataclass
class QRToken:
    """A short-lived, signed QR payload."""

    token: str
    session_id: str
    slot: int
    issued_at: float
    expires_in: float
    ttl: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "session_id": self.session_id,
            "slot": self.slot,
            "issued_at": self.issued_at,
            "expires_in": round(self.expires_in, 1),
            "ttl": self.ttl,
        }


def issue_token(session_id: str, secret: str, ttl: int, *, at: float | None = None) -> QRToken:
    """Build the QR payload for the current time slot."""
    now = at if at is not None else time.time()
    slot = current_slot(ttl, now)
    payload = {
        "v": TOKEN_VERSION,
        "s": session_id,
        "t": slot,
        "k": _signature(secret, session_id, slot),
    }
    token = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    slot_end = (slot + 1) * ttl
    return QRToken(
        token=token,
        session_id=session_id,
        slot=slot,
        issued_at=now,
        expires_in=max(0.0, slot_end - now),
        ttl=ttl,
    )


def parse_token(token: str) -> dict[str, Any]:
    """Decode a scanned token without trusting anything in it."""
    try:
        payload = json.loads(_b64url_decode(token.strip()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise QRTokenError("This QR code is not a valid attendance token", "MALFORMED") from exc
    if not isinstance(payload, dict) or "s" not in payload or "k" not in payload:
        raise QRTokenError("Malformed attendance token", "MALFORMED")
    return payload


def verify_token(
    token: str,
    *,
    secret: str,
    session_id: str,
    ttl: int,
    at: float | None = None,
    skew_slots: int = SKEW_SLOTS,
) -> dict[str, Any]:
    """Validate a scanned token. Raises :class:`QRTokenError` on any problem.

    Checks, in order:

    1. version supported
    2. token belongs to the session the student claims (prevents cross-session replay)
    3. HMAC signature matches -- proves the college server issued it
    4. the time slot is within the accepted window
    """
    now = at if at is not None else time.time()
    payload = parse_token(token)

    if int(payload.get("v", 0)) != TOKEN_VERSION:
        raise QRTokenError("Unsupported token version", "VERSION")

    if str(payload.get("s")) != session_id:
        raise QRTokenError(
            "This QR code belongs to a different lecture session", "WRONG_SESSION"
        )

    slot = int(payload.get("t", -1))
    expected = _signature(secret, session_id, slot)
    if not hmac.compare_digest(str(payload.get("k", "")), expected):
        raise QRTokenError("This QR code is not genuine (bad signature)", "BAD_SIGNATURE")

    live_slot = current_slot(ttl, now)
    age_slots = live_slot - slot
    if age_slots > skew_slots:
        raise QRTokenError(
            "This QR code has expired. Ask your faculty member to show the current code.",
            "EXPIRED",
        )
    if age_slots < -skew_slots:
        raise QRTokenError("This QR code is not valid yet", "NOT_YET_VALID")

    return {
        "session_id": session_id,
        "slot": slot,
        "live_slot": live_slot,
        "age_seconds": (live_slot - slot) * ttl,
        "age_slots": age_slots,
        "fresh": age_slots == 0,
    }


def token_fingerprint(token: str) -> str:
    """Short hash of a token, safe to store in logs and audit trails."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def render_svg(token: str, *, box_size: int = 10, border: int = 3) -> str:
    """Render a token as a QR code in SVG form.

    SVG (rather than a bitmap) keeps the code crisp on any projector or phone
    screen without needing Pillow installed. ``qrcode`` is pure Python for the
    SVG backend, so this works with no image library at all.
    """
    import io

    import qrcode
    import qrcode.image.svg

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    code.add_data(token)
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def render_data_uri(token: str, **kwargs) -> str:
    """The same QR code as a ``data:`` URI, ready to drop into an <img> tag."""
    import base64

    svg = render_svg(token, **kwargs)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ============================================================================
# Analytics and the University 75% rule
# ============================================================================
#
# Attendance analytics.
#
# University of Mumbai regulations require **75% attendance** for a student to be
# allowed to appear for the end-semester examination. Everything here is built
# around answering that question precisely:
#
# * how many lectures of this subject were actually *held* for my division?
# * how many did I attend?
# * am I above or below the 75% bar, and by how much?
# * what happens to my percentage if I miss the next N lectures?
#
# Attendance percentage is computed as::
#
#     attended / held * 100
#
# where ``attended`` counts PRESENT, LATE and faculty-approved MANUAL-present
# records, and ``held`` counts the sessions conducted for the student's division.
# Counting the divisor from *sessions held* (not from the number of records) is
# what makes the number meaningful -- a student who never scanned in still has
# a 0/12 record, not an undefined one.

MU_MIN_ATTENDANCE = 75.0
CURRENT_YEAR = time.strftime("%Y")


def _counts_as_attended(record: dict[str, Any]) -> bool:
    status = record.get("status")
    if status in {"PRESENT", "LATE"}:
        return True
    if status == "MANUAL":
        return record.get("manual_status") in {"PRESENT", "LATE"}
    return False


def _sessions_for(
    sessions: Iterable[dict[str, Any]], *, subject_code: str, year: str, division: str
) -> list[dict[str, Any]]:
    return [
        session
        for session in sessions
        if session.get("subject_code") == subject_code
        and session.get("year") == year
        and session.get("division") == division
    ]


def student_subject_summary(
    *,
    student: dict[str, Any],
    subject: dict[str, Any],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attendance for one student in one subject."""
    held = _sessions_for(
        sessions,
        subject_code=subject["code"],
        year=student.get("year", ""),
        division=student.get("division", ""),
    )
    session_ids = {session["session_id"] for session in held}
    mine = [
        record
        for record in attendance
        if record.get("student_roll") == student["roll_no"]
        and record.get("session_id") in session_ids
    ]
    attended = [r for r in mine if _counts_as_attended(r)]
    held_count = len(held) or len(mine)
    percentage = (len(attended) / held_count * 100) if held_count else 0.0

    return {
        "subject_code": subject["code"],
        "subject_name": subject.get("name", subject["code"]),
        "credits": subject.get("credits"),
        "held": held_count,
        "attended": len(attended),
        "absent": max(0, held_count - len(attended)),
        "present": sum(1 for r in mine if r.get("status") == "PRESENT"),
        "late": sum(1 for r in mine if r.get("status") == "LATE"),
        "manual": sum(1 for r in mine if r.get("status") == "MANUAL"),
        "percentage": round(percentage, 2),
        "eligible": percentage >= MU_MIN_ATTENDANCE,
        "minimum_required": MU_MIN_ATTENDANCE,
        "lectures_to_recover": _lectures_to_recover(len(attended), held_count),
    }


def _lectures_to_recover(attended: int, held: int) -> int | None:
    """How many consecutive lectures must be attended to reach 75%.

    Solves ``(attended + x) / (held + x) >= 0.75`` for the smallest integer x.
    Returns ``None`` when the target is already met.
    """
    if held == 0:
        return 0
    if attended / held >= MU_MIN_ATTENDANCE / 100:
        return None
    x = 1
    while x < 10_000:
        if (attended + x) / (held + x) >= MU_MIN_ATTENDANCE / 100:
            return x
        x += 1
    return None


def student_report(
    *,
    student: dict[str, Any],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-subject breakdown plus an overall figure for one student."""
    rows = [
        student_subject_summary(
            student=student, subject=subject, sessions=sessions, attendance=attendance
        )
        for subject in subjects
    ]
    total_held = sum(row["held"] for row in rows)
    total_attended = sum(row["attended"] for row in rows)
    overall = (total_attended / total_held * 100) if total_held else 0.0
    defaulters = [row["subject_code"] for row in rows if not row["eligible"]]

    return {
        "student": {
            "roll_no": student.get("roll_no"),
            "name": student.get("name"),
            "year": student.get("year"),
            "division": student.get("division"),
            "department": student.get("department"),
            "address": student.get("address"),
        },
        "subjects": rows,
        "overall": {
            "held": total_held,
            "attended": total_attended,
            "percentage": round(overall, 2),
            "eligible": overall >= MU_MIN_ATTENDANCE and not defaulters,
            "minimum_required": MU_MIN_ATTENDANCE,
            "defaulting_subjects": defaulters,
        },
        "generated_at": time.time(),
    }


def defaulter_list(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    threshold: float = MU_MIN_ATTENDANCE,
) -> list[dict[str, Any]]:
    """Students below the 75% bar, worst first -- the report the HoD asks for."""
    defaulters: list[dict[str, Any]] = []
    for student in students:
        rows = [
            student_subject_summary(
                student=student, subject=subject, sessions=sessions, attendance=attendance
            )
            for subject in subjects
        ]
        total_held = sum(row["held"] for row in rows)
        total_attended = sum(row["attended"] for row in rows)
        if not total_held:
            continue
        percentage = total_attended / total_held * 100
        if percentage < threshold:
            defaulters.append(
                {
                    "roll_no": student.get("roll_no"),
                    "name": student.get("name"),
                    "year": student.get("year"),
                    "division": student.get("division"),
                    "held": total_held,
                    "attended": total_attended,
                    "percentage": round(percentage, 2),
                    "shortfall": round(threshold - percentage, 2),
                    "weak_subjects": [
                        row["subject_code"]
                        for row in rows
                        if row["held"] and not row["eligible"]
                    ],
                }
            )
    return sorted(defaulters, key=lambda row: row["percentage"])


def subject_summary(
    *,
    subject: dict[str, Any],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    student_count: int,
) -> dict[str, Any]:
    held = [s for s in sessions if s.get("subject_code") == subject["code"]]
    relevant = [r for r in attendance if r.get("subject_code") == subject["code"]]
    attended = sum(1 for r in relevant if _counts_as_attended(r))
    capacity = len(held) * max(1, student_count)

    return {
        "subject_code": subject["code"],
        "subject_name": subject.get("name", subject["code"]),
        "semester": subject.get("semester"),
        "credits": subject.get("credits"),
        "sessions_held": len(held),
        "marks_recorded": len(relevant),
        "average_percentage": round(attended / capacity * 100, 2) if capacity else 0.0,
        "present": sum(1 for r in relevant if r.get("status") == "PRESENT"),
        "late": sum(1 for r in relevant if r.get("status") == "LATE"),
        "manual": sum(1 for r in relevant if r.get("status") == "MANUAL"),
        "last_session_at": max((s.get("started_at", 0) for s in held), default=None),
    }


def daily_trend(attendance: list[dict[str, Any]], days: int = 14) -> list[dict[str, Any]]:
    """Attendance volume per day -- feeds the dashboard bar chart."""
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"present": 0, "late": 0, "manual": 0})
    for record in attendance:
        stamp = record.get("marked_at")
        if not stamp:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(float(stamp)))
        status = record.get("status")
        if status == "PRESENT":
            buckets[day]["present"] += 1
        elif status == "LATE":
            buckets[day]["late"] += 1
        elif status == "MANUAL":
            buckets[day]["manual"] += 1

    today = time.time()
    series = []
    for offset in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(today - offset * 86400))
        row = buckets.get(day, {"present": 0, "late": 0, "manual": 0})
        series.append(
            {
                "date": day,
                "label": time.strftime("%d %b", time.localtime(today - offset * 86400)),
                "present": row["present"],
                "late": row["late"],
                "manual": row["manual"],
                "total": row["present"] + row["late"] + row["manual"],
            }
        )
    return series


def hourly_heatmap(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Marks grouped by hour of day -- useful for spotting odd scanning times."""
    buckets: dict[int, int] = defaultdict(int)
    for record in attendance:
        stamp = record.get("marked_at")
        if stamp:
            buckets[time.localtime(float(stamp)).tm_hour] += 1
    return [{"hour": hour, "count": buckets.get(hour, 0)} for hour in range(24)]


def _expected_marks(
    students: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> int:
    """How many attendance marks a fully-attended term would contain.

    One mark per enrolled student per lecture held. Sessions carry the year and
    division they were held for, so students are matched the same way the
    marking path matches them.
    """
    enrolled_by_group: dict[tuple[str, str], int] = {}
    for student in students:
        group = (str(student.get("year", "")), str(student.get("division", "")))
        enrolled_by_group[group] = enrolled_by_group.get(group, 0) + 1

    expected = 0
    for session in sessions:
        group = (str(session.get("year", "")), str(session.get("division", "")))
        expected += enrolled_by_group.get(group, 0)
    return expected


def dashboard_stats(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    chain_stats: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the faculty dashboard needs, in one payload."""
    attended = [r for r in attendance if _counts_as_attended(r)]
    today = time.strftime("%Y-%m-%d")
    today_marks = [
        r
        for r in attendance
        if time.strftime("%Y-%m-%d", time.localtime(float(r.get("marked_at", 0)))) == today
    ]
    per_subject = [
        subject_summary(
            subject=subject,
            sessions=sessions,
            attendance=attendance,
            student_count=len(students),
        )
        for subject in subjects
    ]

    return {
        "totals": {
            "students": len(students),
            "subjects": len(subjects),
            "sessions": len(sessions),
            "open_sessions": sum(1 for s in sessions if s.get("status") == "OPEN"),
            "attendance_records": len(attendance),
            "attended_records": len(attended),
            "marks_today": len(today_marks),
            "blocks": chain_stats.get("blocks", 0),
            "chain_height": chain_stats.get("height", 0),
            "anchors": len(anchors),
            "integrity": "VERIFIED" if chain_stats.get("last_report_valid", True) else "FAILED",
        },
        # Turnout is attended marks over the marks that COULD have been made.
        #
        # Dividing by len(attendance) -- as an earlier version did -- always
        # returns 100%, because a record only exists when a student actually
        # scans. An absent student leaves no row. The denominator therefore has
        # to be (lectures held x students enrolled), which is what the
        # University's 75% rule is measured against.
        "average_attendance_percentage": round(
            len(attended) / expected_marks * 100, 2
        )
        if (expected_marks := _expected_marks(students, sessions))
        else 0.0,
        "expected_marks": expected_marks,
        "chain": chain_stats,
        "subjects": per_subject,
        "trend": daily_trend(attendance),
        "hourly": hourly_heatmap(attendance),
        "recent_attendance": sorted(
            attendance, key=lambda r: r.get("marked_at", 0), reverse=True
        )[:25],
        "recent_anchors": anchors[:5],
        "generated_at": time.time(),
    }


def export_rows(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten everything into CSV-friendly rows (one per student per subject)."""
    rows: list[dict[str, Any]] = []
    for student in students:
        for subject in subjects:
            summary = student_subject_summary(
                student=student, subject=subject, sessions=sessions, attendance=attendance
            )
            rows.append(
                {
                    "roll_no": student.get("roll_no"),
                    "name": student.get("name"),
                    "year": student.get("year"),
                    "division": student.get("division"),
                    "subject_code": subject["code"],
                    "subject_name": subject.get("name"),
                    "lectures_held": summary["held"],
                    "attended": summary["attended"],
                    "absent": summary["absent"],
                    "late": summary["late"],
                    "percentage": summary["percentage"],
                    "eligible_75": "YES" if summary["eligible"] else "NO",
                }
            )
    return rows


# ============================================================================
# Fraud detectors
# ============================================================================
#
# Rule-based anomaly detection for attendance fraud.
#
# The classic attacks on a QR attendance system, and the signature each one leaves:
#
# ======================  =====================================================
# Attack                  Evidence in the data
# ======================  =====================================================
# Proxy marking           the same device or IP marks several different students
#                         inside one session
# Screenshot sharing      marks cluster in the first seconds of a time slot, all
#                         from one IP, well after the lecture started
# Scripted submissions    suspiciously uniform client latency, machine-like
#                         inter-arrival times
# Away-from-class         a student marks attendance in two places at the same
#                         moment, or marks while the session is elsewhere
# Sudden outsiders        a student who never attends suddenly marks in a session
#                         with an unusually low overall turnout
# ======================  =====================================================
#
# This module deliberately uses **transparent rules**, not a black box. Every alert
# states the rule, the observed value, the threshold, and the records that
# triggered it, so a faculty member can act on it and a student can contest it.
#
# That transparency is also the honest engineering answer: with a few thousand
# records there is not enough labelled data to train a reliable supervised model,
# and an unexplainable flag that can fail a student is worse than no flag. The
# ``AnomalyScorer`` interface is the seam where a future ML model (isolation
# forest, autoencoder) would plug in without touching the rest of the system.

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"


class AnomalyScorer:
    """Extension point for a future ML-based detector."""

    name = "rule-based"

    def score(self, records: list[dict[str, Any]]) -> float:  # pragma: no cover - interface
        raise NotImplementedError


def _group(records: Iterable[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    buckets: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[record.get(key)].append(record)
    return buckets


def detect_shared_devices(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same device fingerprint used by different students in one session."""
    alerts: list[dict[str, Any]] = []
    by_session = _group(attendance, "session_id")
    for session_id, records in by_session.items():
        by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            fingerprint = record.get("device_fingerprint") or ""
            if fingerprint:
                by_device[fingerprint].append(record)
        for fingerprint, group in by_device.items():
            rolls = sorted({r.get("student_roll") for r in group})
            if len(rolls) > 1:
                alerts.append(
                    {
                        "rule": "SHARED_DEVICE",
                        "severity": SEVERITY_HIGH,
                        "session_id": session_id,
                        "subject_code": group[0].get("subject_code"),
                        "observed": len(rolls),
                        "threshold": 1,
                        "message": (
                            f"One device submitted attendance for {len(rolls)} students "
                            f"in the same lecture: {', '.join(rolls)}"
                        ),
                        "evidence": {
                            "device_fingerprint": fingerprint,
                            "students": rolls,
                            "tx_ids": [r.get("tx_id") for r in group],
                        },
                    }
                )
    return alerts


def detect_burst(attendance: list[dict[str, Any]], *, window_seconds: float = 20.0) -> list[dict[str, Any]]:
    """A suspicious burst of marks in a very short window (screenshot sharing)."""
    alerts: list[dict[str, Any]] = []
    for session_id, records in _group(attendance, "session_id").items():
        stamps = sorted(float(r.get("marked_at", 0)) for r in records)
        for index in range(len(stamps) - 5):
            span = stamps[index + 5] - stamps[index]
            if 0 < span <= window_seconds:
                alerts.append(
                    {
                        "rule": "BURST",
                        "severity": SEVERITY_MEDIUM,
                        "session_id": session_id,
                        "subject_code": records[0].get("subject_code"),
                        "observed": round(span, 2),
                        "threshold": window_seconds,
                        "message": (
                            f"6 students marked within {span:.1f}s of each other -- "
                            "possible shared QR screenshot."
                        ),
                        "evidence": {
                            "window_start": stamps[index],
                            "window_end": stamps[index + 5],
                        },
                    }
                )
                break
    return alerts


def detect_scripted_clients(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identical client latencies suggest an automated script rather than a phone."""
    alerts: list[dict[str, Any]] = []
    latencies = [
        int(r["client_latency_ms"])
        for r in attendance
        if r.get("client_latency_ms") is not None
    ]
    if len(latencies) < 8:
        return alerts
    try:
        deviation = statistics.pstdev(latencies)
    except statistics.StatisticsError:  # pragma: no cover
        return alerts
    if deviation == 0:
        alerts.append(
            {
                "rule": "SCRIPTED_CLIENT",
                "severity": SEVERITY_MEDIUM,
                "session_id": None,
                "observed": 0.0,
                "threshold": 1.0,
                "message": (
                    f"All {len(latencies)} submissions report an identical client "
                    "latency -- consistent with a scripted client."
                ),
                "evidence": {"samples": len(latencies), "latency_ms": latencies[0]},
            }
        )
    return alerts


def detect_duplicate_device_students(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One student rolling across many devices -- a device-sharing ring."""
    alerts: list[dict[str, Any]] = []
    thresholds = 4
    for roll, records in _group(attendance, "student_roll").items():
        devices = {r.get("device_fingerprint") for r in records if r.get("device_fingerprint")}
        if len(devices) > thresholds:
            alerts.append(
                {
                    "rule": "MANY_DEVICES",
                    "severity": SEVERITY_LOW,
                    "session_id": None,
                    "student_roll": roll,
                    "observed": len(devices),
                    "threshold": thresholds,
                    "message": (
                        f"{roll} has marked attendance from {len(devices)} different "
                        "devices. Verify the student is not sharing credentials."
                    ),
                    "evidence": {"devices": sorted(devices)},
                }
            )
    return alerts


def detect_impossible_overlap(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same student marked in two different sessions at the same instant."""
    alerts: list[dict[str, Any]] = []
    for roll, records in _group(attendance, "student_roll").items():
        ordered = sorted(records, key=lambda r: float(r.get("marked_at", 0)))
        for previous, current in zip(ordered, ordered[1:]):
            gap = float(current.get("marked_at", 0)) - float(previous.get("marked_at", 0))
            if (
                0 <= gap < 60
                and previous.get("session_id") != current.get("session_id")
            ):
                alerts.append(
                    {
                        "rule": "IMPOSSIBLE_OVERLAP",
                        "severity": SEVERITY_HIGH,
                        "student_roll": roll,
                        "observed": round(gap, 2),
                        "threshold": 60,
                        "message": (
                            f"{roll} was marked present in two different lectures "
                            f"{gap:.0f}s apart."
                        ),
                        "evidence": {
                            "first": previous.get("tx_id"),
                            "second": current.get("tx_id"),
                            "first_session": previous.get("session_id"),
                            "second_session": current.get("session_id"),
                        },
                    }
                )
    return alerts


def detect_low_turnout_outliers(
    attendance: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Sessions where one student attended but almost nobody else did."""
    alerts: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    for record in attendance:
        counts[record.get("session_id")] += 1

    for session in sessions:
        session_id = session.get("session_id")
        marked = counts.get(session_id, 0)
        expected = int(session.get("expected_count") or 0)
        if expected >= 10 and marked:
            ratio = marked / expected
            if ratio < 0.15:
                alerts.append(
                    {
                        "rule": "LOW_TURNOUT",
                        "severity": SEVERITY_LOW,
                        "session_id": session_id,
                        "subject_code": session.get("subject_code"),
                        "observed": round(ratio, 3),
                        "threshold": 0.15,
                        "message": (
                            f"Only {marked} of {expected} students marked attendance "
                            f"for {session.get('subject_code')} -- verify the session "
                            "ran as scheduled."
                        ),
                        "evidence": {"marked": marked, "expected": expected},
                    }
                )
    return alerts


def detect_late_start_scans(
    attendance: list[dict[str, Any]], *, seconds: float = 5.0
) -> list[dict[str, Any]]:
    """Marks landing in the first seconds of a lecture (before anyone could sit)."""
    alerts: list[dict[str, Any]] = []
    per_session = _group(attendance, "session_id")
    for session_id, records in per_session.items():
        starts = [float(r.get("session_started_at", 0)) for r in records]
        if not starts:
            continue
        started = statistics.median(starts)
        immediate = [
            r for r in records if float(r.get("marked_at", 0)) - started < seconds
        ]
        if len(immediate) >= 3:
            alerts.append(
                {
                    "rule": "INSTANT_SCANS",
                    "severity": SEVERITY_LOW,
                    "session_id": session_id,
                    "observed": len(immediate),
                    "threshold": 3,
                    "message": (
                        f"{len(immediate)} students marked within {seconds:.0f}s of the "
                        "session opening -- unusual if the lecture had not started."
                    ),
                    "evidence": {"tx_ids": [r.get("tx_id") for r in immediate]},
                }
            )
    return alerts


#: All detectors, run together by :func:`scan`.
DETECTORS = (
    detect_shared_devices,
    detect_impossible_overlap,
    detect_burst,
    detect_scripted_clients,
    detect_duplicate_device_students,
)


def scan(
    *,
    attendance: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
    include_low_turnout: bool = True,
) -> dict[str, Any]:
    """Run every detector and summarise the results."""
    alerts: list[dict[str, Any]] = []
    for detector in DETECTORS:
        try:
            alerts.extend(detector(attendance))
        except Exception as exc:  # pragma: no cover - a bad detector must not break the page
            alerts.append(
                {
                    "rule": "DETECTOR_ERROR",
                    "severity": SEVERITY_LOW,
                    "message": f"{detector.__name__} failed: {exc}",
                    "observed": None,
                    "threshold": None,
                    "evidence": {},
                }
            )

    if include_low_turnout and sessions:
        alerts.extend(detect_low_turnout_outliers(attendance, sessions))
        alerts.extend(detect_late_start_scans(attendance))

    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    alerts.sort(key=lambda alert: order.get(alert.get("severity"), 3))

    return {
        "scanned_records": len(attendance),
        "alert_count": len(alerts),
        "by_severity": {
            severity: sum(1 for a in alerts if a.get("severity") == severity)
            for severity in (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)
        },
        "alerts": alerts,
        "detectors": [detector.__name__ for detector in DETECTORS],
        "generated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Module aliases
# ---------------------------------------------------------------------------
# Some call sites read `analytics.student_report(...)`, which is clearer than a
# bare function name. The analytics section above is this module, so alias it.
analytics = sys.modules[__name__]
qr = sys.modules[__name__]
identity = sys.modules[__name__]
