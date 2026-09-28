"""The application's rules, and the only place that touches the store.

Everything the system can do is here:

    students     add / list / look up
    sessions     open a lecture roll call, close it (which seals a block)
    records      mark a student present, list what a session or student has
    chain        load on start-up, save whenever a block is mined
    tokens       sign and check the rotating QR code

Keeping it in one file means the whole data model can be read in one sitting.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from .blockchain import Blockchain, verify_merkle_proof

# ---------------------------------------------------------------------------
# The rotating QR token
# ---------------------------------------------------------------------------
def _signature(session_id: str, window: int, secret: str) -> str:
    """A short signature over (session, time window)."""
    message = f"{session_id}|{window}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:16]


def current_token(session_id: str, secret: str, ttl: int, *, at: float | None = None) -> dict[str, Any]:
    """The QR token for right now.

    The token is not stored anywhere. It is a time window plus a signature, so
    the server can check it later without remembering anything, and an old token
    stops working by itself when its window passes.
    """
    ttl = max(5, int(ttl))
    now = time.time() if at is None else at
    window = int(now // ttl)
    return {
        "session_id": session_id,
        "window": window,
        "signature": _signature(session_id, window, secret),
        "ttl": ttl,
        "expires_in": int(ttl - (now % ttl)),
    }


def check_token(
    session_id: str,
    window: Any,
    signature: str,
    secret: str,
    ttl: int,
    *,
    at: float | None = None,
) -> str:
    """Raise ValueError if a scanned token is not usable right now."""
    try:
        window = int(window)
    except (TypeError, ValueError):
        raise ValueError("This code is not valid.")

    ttl = max(5, int(ttl))
    now = time.time() if at is None else at
    current = int(now // ttl)

    if window not in (current, current - 1):
        raise ValueError("This code has expired. Scan the new one on the projector.")

    expected = _signature(session_id, window, secret)
    if not hmac.compare_digest(expected, str(signature)):
        raise ValueError("This code was not issued by the server.")

    return session_id


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------
class Database:
    """All reads and writes, in front of the store."""

    def __init__(self, settings, store) -> None:
        self.settings = settings
        self.store = store
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.chain = Blockchain(difficulty=settings.difficulty)
        self.load()

    # -- loading and saving --------------------------------------------
    def load(self) -> None:
        try:
            loaded = self.store.read_all()
        except Exception as exc:                      # noqa: BLE001 - keep serving
            self.store_error = str(exc)
            loaded = {}
        else:
            self.store_error = ""
        self.data = {name: dict(loaded.get(name, {})) for name in ("students", "sessions", "records", "blocks")}

        stored_blocks = sorted(self.data["blocks"].values(), key=lambda b: b.get("index", 0))
        self.chain.load(stored_blocks)
        if not stored_blocks:
            # The genesis block is what the first real block points at, so it has
            # to be saved too -- otherwise a restart leaves the chain starting at
            # block 1 with nothing to link backwards to.
            self._put("blocks", "0", self.chain.blocks[0].to_dict())

    def _put(self, collection: str, doc_id: str, document: dict[str, Any]) -> dict[str, Any]:
        self.data.setdefault(collection, {})[doc_id] = document
        try:
            self.store.write(collection, doc_id, document)
        except Exception as exc:                      # noqa: BLE001
            self.store_error = str(exc)
        return document

    def _delete(self, collection: str, doc_id: str) -> None:
        self.data.get(collection, {}).pop(doc_id, None)
        try:
            self.store.delete(collection, doc_id)
        except Exception as exc:                      # noqa: BLE001
            self.store_error = str(exc)

    def all(self, collection: str) -> list[dict[str, Any]]:
        return list(self.data.get(collection, {}).values())

    # -- students -------------------------------------------------------
    def add_student(
        self,
        roll_no: str,
        name: str,
        division: str = "A",
        year: str = "BE",
        email: str = "",
    ) -> dict[str, Any]:
        roll_no = roll_no.strip().upper()
        if not roll_no:
            raise ValueError("A roll number is required.")
        student = {
            "roll_no": roll_no,
            "name": name.strip() or roll_no,
            "division": division.strip() or "A",
            "year": year.strip() or "BE",
            "email": email.strip(),
            "created_at": time.time(),
        }
        return self._put("students", roll_no, student)

    def student(self, roll_no: str) -> dict[str, Any] | None:
        return self.data["students"].get(str(roll_no).strip().upper())

    def students(self) -> list[dict[str, Any]]:
        return sorted(self.data["students"].values(), key=lambda s: s.get("roll_no", ""))

    # -- sessions -------------------------------------------------------
    def open_session(
        self,
        subject_code: str,
        room: str = "",
        minutes: int = 30,
        *,
        at: float | None = None,
    ) -> dict[str, Any]:
        subject = self.settings.subject(subject_code)
        if subject is None:
            raise ValueError(f"Unknown subject code: {subject_code}")

        now = time.time() if at is None else at
        minutes = max(1, min(int(minutes or 30), 180))
        session_id = f"{subject['code']}-{int(now)}"

        session = {
            "id": session_id,
            "subject_code": subject["code"],
            "subject_name": subject["name"],
            "faculty": subject["faculty"],
            "division": subject["division"],
            "room": room.strip() or "Lab 204",
            "opened_at": now,
            "expires_at": now + minutes * 60,
            "minutes": minutes,
            "closed_at": None,
            "block_index": None,
        }
        return self._put("sessions", session_id, session)

    def session(self, session_id: str) -> dict[str, Any] | None:
        return self.data["sessions"].get(session_id)

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = sorted(self.data["sessions"].values(), key=lambda s: s.get("opened_at", 0), reverse=True)
        return rows[:limit]

    def open_sessions(self, *, at: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if at is None else at
        return [s for s in self.sessions(100) if self.is_open(s, at=now)]

    @staticmethod
    def is_open(session: dict[str, Any], *, at: float | None = None) -> bool:
        if session.get("closed_at"):
            return False
        now = time.time() if at is None else at
        return now < float(session.get("expires_at", 0))

    # -- records --------------------------------------------------------
    def mark(
        self,
        session_id: str,
        roll_no: str,
        *,
        at: float | None = None,
    ) -> dict[str, Any]:
        """Record one student as present in one session."""
        session = self.session(session_id)
        if session is None:
            raise ValueError("That lecture session does not exist.")
        if not self.is_open(session, at=at):
            raise ValueError("This session is closed, so it can no longer be marked.")

        student = self.student(roll_no)
        if student is None:
            raise ValueError(f"{roll_no} is not in the class register.")

        doc_id = f"{session_id}:{student['roll_no']}"
        if doc_id in self.data["records"]:
            raise ValueError(f"{student['name']} is already marked present.")

        record = {
            "id": doc_id,
            "session_id": session_id,
            "subject_code": session["subject_code"],
            "subject_name": session["subject_name"],
            "roll_no": student["roll_no"],
            "name": student["name"],
            "status": "PRESENT",
            "marked_at": time.time() if at is None else at,
            "block_index": None,
        }
        return self._put("records", doc_id, record)

    def records_for(self, session_id: str) -> list[dict[str, Any]]:
        rows = [r for r in self.data["records"].values() if r.get("session_id") == session_id]
        return sorted(rows, key=lambda r: r.get("marked_at", 0), reverse=True)

    def records_for_student(self, roll_no: str) -> list[dict[str, Any]]:
        roll_no = str(roll_no).strip().upper()
        rows = [r for r in self.data["records"].values() if r.get("roll_no") == roll_no]
        return sorted(rows, key=lambda r: r.get("marked_at", 0), reverse=True)

    def attendance_percentage(self, roll_no: str) -> float:
        sessions = [s for s in self.data["sessions"].values() if s.get("closed_at")]
        if not sessions:
            return 0.0
        marked = len({r["session_id"] for r in self.records_for_student(roll_no)})
        return round(marked * 100.0 / len(sessions), 1)

    # -- sealing --------------------------------------------------------
    def close_session(self, session_id: str, *, at: float | None = None) -> dict[str, Any]:
        """Close a roll call and seal its records into a new block."""
        session = self.session(session_id)
        if session is None:
            raise ValueError("That lecture session does not exist.")
        if session.get("closed_at"):
            raise ValueError("This session is already sealed.")

        records = self.records_for(session_id)
        block = self.chain.add_session_block(session_id, records, when=at)

        for record in records:
            record["block_index"] = block.index
            self._put("records", record["id"], record)

        session = dict(session)
        session["closed_at"] = time.time() if at is None else at
        session["block_index"] = block.index
        self._put("sessions", session_id, session)
        self._put("blocks", str(block.index), block.to_dict())

        return {"block": block, "record_count": len(records)}

    # -- proofs and statistics -----------------------------------------
    def prove(self, roll_no: str, session_id: str | None = None) -> dict[str, Any] | None:
        """A Merkle proof for one sealed record, checked before it is returned."""
        found = self.chain.prove_record(roll_no, session_id)
        if found is None:
            return None
        valid = verify_merkle_proof(found["record_hash"], found["proof"], found["root"])
        found["proof_valid"] = valid
        return found

    def stats(self) -> dict[str, Any]:
        chain = self.chain.stats()
        return {
            **chain,
            "students": len(self.data["students"]),
            "sessions": len(self.data["sessions"]),
            "open_sessions": len(self.open_sessions()),
            "attendance_records": len(self.data["records"]),
            "marked_now": sum(
                len(self.records_for(s["id"])) for s in self.open_sessions()
            ),
        }

    # -- demo data ------------------------------------------------------
    def is_empty(self) -> bool:
        return not self.data["students"] and not self.data["sessions"]

    def reset(self) -> None:
        """Start again from an empty register and a fresh genesis block."""
        for collection in ("students", "sessions", "records", "blocks"):
            for doc_id in list(self.data.get(collection, {})):
                self._delete(collection, doc_id)
        self.chain.create_genesis()
        self._put("blocks", "0", self.chain.blocks[0].to_dict())
