"""
Demo data generator: six weeks of believable attendance history.

Why this exists
---------------
A blockchain with one block in it demonstrates nothing. Reviewers respond to a
populated system: a real trend line, a few students below the 75% bar, an
occasional late arrival, blocks chained back through time.

This module builds that history **through the real code path** -- it constructs
genuine transactions, signs them with each student's key, and mines a real block
per lecture. The only thing faked is the clock: timestamps are backdated to the
lecture's date, which is exactly what a seeded migration would do.

It also plants three detectable oddities on purpose, so the anomaly screen has
something true to report:

* one student marked from four different devices (device sharing);
* one burst of six students inside 10 seconds (shared QR screenshot);
* one pair of sessions where the same student was marked seconds apart.

Those are real patterns in the data, found by the real detectors -- not
hard-coded alerts.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from .blockchain.transaction import (
    STATUS_LATE,
    STATUS_PRESENT,
    build_attendance_transaction,
)
from .services.ledger import Ledger, LedgerError
from .services.repository import Repository

log = logging.getLogger("bcoe.demo")

LECTURE_HOURS = (9, 10, 11, 12, 14, 15)
WEEKDAYS = (0, 1, 2, 3, 4)  # Monday to Friday


def _student_diligence(roll_no: str, rng: random.Random) -> float:
    """Stable per-student attendance tendency, with a few outliers.

    Deterministic per roll number so repeated demo runs look the same, and skewed
    so roughly 10-15% of the class ends up below the university's 75% bar --
    which is realistic and gives the defaulter report something to show.
    """
    seed = sum(ord(character) for character in roll_no)
    rng = random.Random(seed)
    roll = rng.random()
    if roll < 0.12:
        return rng.uniform(0.45, 0.68)   # consistent defaulters
    if roll < 0.25:
        return rng.uniform(0.68, 0.80)   # borderline
    if roll < 0.75:
        return rng.uniform(0.80, 0.94)   # normal
    return rng.uniform(0.94, 0.995)      # very regular


def simulate_history(
    *,
    config,
    repository: Repository,
    ledger: Ledger,
    subjects: list[str] | None = None,
    year: str = "BE",
    division: str = "A",
    weeks: int = 6,
    seed: int = 20260929,
    clean: bool = True,
) -> dict[str, Any]:
    """Generate backdated lecture sessions and attendance records."""
    rng = random.Random(seed)
    students = repository.list_students(year=year, division=division, active_only=True)
    if not students:
        raise LedgerError("No students found -- run `python manage.py seed` first", "NOT_SEEDED")

    all_subjects = [s for s in repository.list_subjects() if s.get("year") == year]
    if subjects:
        all_subjects = [s for s in all_subjects if s["code"] in subjects]
    if not all_subjects:
        raise LedgerError(f"No subjects found for year {year}", "NO_SUBJECTS")

    diligence = {student["roll_no"]: _student_diligence(student["roll_no"], rng) for student in students}
    devices = {student["roll_no"]: f"BCOE-DEV-{student['roll_no'][-4:]}" for student in students}

    # Deliberate device-sharing ring and a shared-screenshot burst.
    device_sharers = [student["roll_no"] for student in students[:4]]
    burst_group = [student["roll_no"] for student in students[10:16]]
    overlap_pair = (students[6]["roll_no"], students[7]["roll_no"])

    now = time.time()
    day_seconds = 86400
    sessions_created = 0
    records_created = 0
    blocks_created = 0
    start_block = ledger.chain.head.index

    # -- build the whole timetable first, then sort it -------------------
    # Blocks are appended in creation order, so they must be created in
    # chronological order or the chain's timestamps would run backwards.
    local = time.localtime(now)
    midnight_today = now - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
    monday_this_week = midnight_today - local.tm_wday * day_seconds

    schedule: list[tuple[float, dict[str, Any]]] = []
    for week_offset in range(weeks, 0, -1):
        monday = monday_this_week - week_offset * 7 * day_seconds
        for weekday in WEEKDAYS:
            for subject in all_subjects:
                # Not every subject meets every day.
                if rng.random() > 0.45:
                    continue
                hour = rng.choice(LECTURE_HOURS)
                started = monday + weekday * day_seconds + hour * 3600 + rng.randint(0, 240)
                if started > now - 900:
                    continue
                schedule.append((started, subject))

    schedule.sort(key=lambda item: item[0])

    for started, subject in schedule:
        session = _create_backdated_session(
            repository=repository,
            ledger=ledger,
            subject=subject,
            year=year,
            division=division,
            started=started,
            students=students,
            rng=rng,
        )
        sessions_created += 1
        _, marked, sealed, _ = _mark_students(
            repository=repository,
            ledger=ledger,
            session=session,
            students=students,
            diligence=diligence,
            devices=devices,
            device_sharers=device_sharers,
            burst_group=burst_group,
            overlap_pair=overlap_pair,
            rng=rng,
        )
        records_created += marked
        blocks_created += sealed

    return {
        "sessions_created": sessions_created,
        "records_created": records_created,
        "blocks_created": ledger.chain.head.index - start_block,
        "students": len(students),
        "subjects": len(all_subjects),
        "weeks": weeks,
        "chain_height": ledger.chain.head.index,
        "difficulty": ledger.chain.difficulty,
    }


def _create_backdated_session(
    *,
    repository: Repository,
    ledger: Ledger,
    subject: dict[str, Any],
    year: str,
    division: str,
    started: float,
    students: list[dict[str, Any]],
    rng: random.Random,
) -> dict[str, Any]:
    """Create a historical session record that already looks closed."""
    import secrets

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
    session_id = f"LEC-{stamp}-{subject['code']}-{division}-{secrets.token_hex(2)}"
    duration_minutes = 60
    faculty_id = (subject.get("faculty_ids") or ["BCOE-FAC-001"])[0]

    session = {
        "session_id": session_id,
        "subject_code": subject["code"],
        "subject_name": subject.get("name", subject["code"]),
        "faculty_id": faculty_id,
        "faculty_name": repository.get_faculty(faculty_id).get("name", faculty_id)
        if repository.get_faculty(faculty_id)
        else faculty_id,
        "department": subject.get("department"),
        "semester": subject.get("semester"),
        "division": division,
        "year": year,
        "room": rng.choice(["Classroom 204", "Lab 301", "Lab 305", "Seminar Hall"]),
        "started_at": started,
        "expires_at": started + duration_minutes * 60,
        "closed_at": started + duration_minutes * 60,
        "qr_ttl": 30,
        "grace_seconds": 300,
        "secret": "simulated-session-secret-not-usable",
        "status": "CLOSED",
        "note": "Seeded history (simulated)",
        "simulated": True,
        "block_index": None,
        "block_hash": None,
        "merkle_root": None,
        "present_count": 0,
        "late_count": 0,
        "marked_count": 0,
        "expected_count": len(students),
        "created_at": started,
    }
    repository.save_session(session)
    return session


def _mark_students(
    *,
    repository: Repository,
    ledger: Ledger,
    session: dict[str, Any],
    students: list[dict[str, Any]],
    diligence: dict[str, float],
    devices: dict[str, str],
    device_sharers: list[str],
    burst_group: list[str],
    overlap_pair: tuple[str, str],
    rng: random.Random,
) -> tuple[int, int, int, dict[str, int]]:
    """Attach one signed transaction per attending student, then seal one block."""
    subject_code = session["subject_code"]
    session_id = session["session_id"]
    started = float(session["started_at"])
    present: list[dict[str, Any]] = []

    attendance_roll = rng.random()
    if attendance_roll < 0.08:
        multiplier = 0.35          # a lecture almost nobody attended
    elif attendance_roll < 0.2:
        multiplier = 0.62          # a poorly attended lecture
    else:
        multiplier = 1.0

    for student in students:
        roll = student["roll_no"]
        probability = diligence[roll] * multiplier
        if rng.random() > probability:
            continue
        present.append(student)

    # Force the burst group in, so the detector has a genuine pattern to find.
    for roll in burst_group:
        if not any(s["roll_no"] == roll for s in present):
            student = next(s for s in students if s["roll_no"] == roll)
            present.append(student)

    created = 0
    for index, student in enumerate(present):
        roll = student["roll_no"]

        # -- when did they mark? --------------------------------------
        if roll in burst_group:
            # all six inside a 10-second window, a few minutes after the hour
            burst_offset = 180 + burst_group.index(roll) * 1.4
            marked_at = started + burst_offset
        elif roll in overlap_pair:
            # two students marking in different lectures seconds apart is handled
            # by the overlap detector; here they arrive suspiciously early
            marked_at = started + 2 + overlap_pair.index(roll)
        else:
            # most arrive within the grace period, some straggle in late
            if rng.random() < 0.88:
                marked_at = started + rng.uniform(5, 280)
                status = STATUS_PRESENT
            else:
                marked_at = started + rng.uniform(320, 900)
                status = STATUS_LATE

        if roll in burst_group or roll in overlap_pair:
            status = STATUS_PRESENT

        duration = 60
        if marked_at > float(session["expires_at"]):
            marked_at = float(session["expires_at"]) - rng.uniform(10, duration)

        elapsed = marked_at - started
        if elapsed <= int(session["grace_seconds"]):
            status = STATUS_PRESENT
        else:
            status = STATUS_LATE

        device = devices[roll]
        if roll in device_sharers and index == 0:
            # The first sharer's device is 'borrowed'; the rest use it too.
            device = "BCOE-DEV-SHARED-0001"
        elif roll in device_sharers[1:]:
            device = rng.choice(
                ["BCOE-DEV-SHARED-0001", devices[roll], "BCOE-DEV-BORROWED-77"]
            )

        identity = ledger.keystore.get(roll)
        if identity is None:
            continue

        transaction = build_attendance_transaction(
            session_id=session_id,
            student_roll=roll,
            student_name=student.get("name", roll),
            subject_code=subject_code,
            subject_name=session.get("subject_name", subject_code),
            faculty_id=session["faculty_id"],
            room=session.get("room", ""),
            status=status,
            marks_at=marked_at,
            session_started_at=started,
            grace_seconds=int(session["grace_seconds"]),
            device_fingerprint=device,
            client_latency_ms=rng.randint(38, 260),
            nonce=f"sim-{roll[-4:]}-{int(marked_at)}",
        )
        transaction.sender_pubkey = identity.public_key
        transaction.sign(identity.private_key)
        transaction.tx_id = transaction.compute_id()
        transaction.received_at = marked_at

        accepted, _reason = ledger.chain.add_transaction(transaction)
        if not accepted:
            continue
        created += 1

    blocks = 0
    block = ledger.chain.mine_pending(note=f"Roll call: {subject_code}")
    if block is not None:
        # Backdate the block to the lecture so the chain reads chronologically.
        block.timestamp = started + 900
        block.seal()
        ledger.persist_block(block)
        blocks = 1

    records = ledger.chain.transactions_for_session(session_id)
    index_records = []
    for entry in records:
        payload = entry["payload"]
        index_records.append(
            {
                "tx_id": entry["tx_id"],
                "session_id": session_id,
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
                "client_latency_ms": payload.get("client_latency_ms"),
                "signature": entry.get("signature", ""),
                "block_index": entry["block_index"],
                "block_hash": entry["block_hash"],
                "merkle_root": None,
                "on_chain": True,
            }
        )
    if index_records:
        repository.save_attendance_many(index_records)

    repository.update_session(
        session_id,
        {
            "marked_count": len(index_records),
            "present_count": sum(1 for r in index_records if r["status"] == STATUS_PRESENT),
            "late_count": sum(1 for r in index_records if r["status"] == STATUS_LATE),
            "block_index": block.index if block else None,
            "block_hash": block.hash if block else None,
            "merkle_root": block.merkle_root if block else None,
        },
    )

    return len(present), len(index_records), blocks, {
        "records": len(index_records),
        "blocks": blocks,
    }


__all__ = ["simulate_history", "LECTURE_HOURS"]
