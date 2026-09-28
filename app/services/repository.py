"""
Domain repository: everything the application knows about the college register.

Sits on top of the generic :class:`~app.storage.base.Store` document API so the
same code runs against the local JSON backend and Firestore without changes.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Iterable

from ..storage import (
    ANCHORS,
    ATTENDANCE,
    AUDIT,
    BLOCKS,
    FACULTY,
    META,
    SESSIONS,
    STUDENTS,
    SUBJECTS,
    Store,
)


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


__all__ = ["Repository"]
