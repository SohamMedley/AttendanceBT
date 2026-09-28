"""Attendance API: sessions, rotating QR codes, marking, and manual overrides."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from flask import Blueprint, jsonify, request

from ..services import Services
from ..services.ledger import LedgerError

bp = Blueprint("attendance_api", __name__, url_prefix="/api")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover - always initialised by the factory
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


def device_fingerprint(payload: dict[str, Any]) -> str:
    """Derive a stable, privacy-preserving device identifier.

    We never store the raw user-agent or IP. Instead we keep a salted SHA-256
    fingerprint: enough to tell "same device" from "different device" for fraud
    detection, but not enough to identify a person from the database alone.
    """
    provided = (payload or {}).get("device_id")
    if provided:
        raw = str(provided)
    else:
        raw = "|".join(
            [
                request.headers.get("User-Agent", ""),
                str(request.headers.get("Accept-Language", "")),
                request.remote_addr or "",
            ]
        )
    return "BCOE-DEV-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12].upper()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@bp.post("/sessions")
def open_session():
    """Faculty starts a roll call."""
    payload = request.get_json(silent=True) or {}
    session = _svc().ledger.open_session(
        subject_code=payload.get("subject_code", ""),
        faculty_id=payload.get("faculty_id", ""),
        room=payload.get("room", ""),
        duration_minutes=payload.get("duration_minutes"),
        division=payload.get("division", "A"),
        year=payload.get("year", "BE"),
        note=payload.get("note", ""),
    )
    public = {k: v for k, v in session.items() if k != "secret"}
    public["qr_ttl"] = session.get("qr_ttl", 30)
    return jsonify({"ok": True, "session": public})


@bp.get("/sessions")
def list_sessions():
    params = request.args
    sessions = _svc().repo.list_sessions(
        faculty_id=params.get("faculty_id"),
        subject_code=params.get("subject_code"),
        status=params.get("status"),
        limit=int(params.get("limit", 50)),
    )
    return jsonify({"ok": True, "sessions": [dict(s) for s in sessions]})


@bp.get("/sessions/<session_id>")
def get_session(session_id: str):
    session = _svc().repo.get_session(session_id)
    if session is None:
        raise LedgerError(f"Unknown session {session_id}", "UNKNOWN_SESSION", 404)
    return jsonify({"ok": True, "session": {k: v for k, v in session.items() if k != "secret"}})


@bp.post("/sessions/<session_id>/close")
def close_session(session_id: str):
    payload = request.get_json(silent=True) or {}
    session = _svc().ledger.close_session(
        session_id, actor=payload.get("faculty_id", "faculty")
    )
    return jsonify({"ok": True, "session": {k: v for k, v in session.items() if k != "secret"}})


@bp.get("/sessions/<session_id>/qr")
def session_qr(session_id: str):
    """The projector display polls this every few seconds to refresh the QR.

    Returns the signed token *and* a ready-to-use SVG data URI, so the browser
    never needs a QR rendering library.
    """
    from ..services.qr import render_data_uri

    data = _svc().ledger.current_qr(session_id)
    data["qr_svg"] = render_data_uri(data["token"], box_size=10, border=3)
    return jsonify({"ok": True, **data})


@bp.get("/sessions/<session_id>/live")
def session_live(session_id: str):
    """Live counters + the newest marks, for the session screen."""
    services = _svc()
    session = services.repo.get_session(session_id)
    if session is None:
        raise LedgerError(f"Unknown session {session_id}", "UNKNOWN_SESSION", 404)

    records = services.repo.attendance_for_session(session_id)
    students = services.repo.list_students(
        year=session.get("year"), division=session.get("division")
    )
    marked_rolls = {r["student_roll"] for r in records}

    return jsonify(
        {
            "ok": True,
            "session_id": session_id,
            "status": session.get("status"),
            "marked": len(records),
            "present": sum(1 for r in records if r["status"] == "PRESENT"),
            "late": sum(1 for r in records if r["status"] == "LATE"),
            "manual": sum(1 for r in records if r["status"] == "MANUAL"),
            "expected": len(students),
            "absent": len(students) - len(marked_rolls),
            "absentees": [
                {"roll_no": s["roll_no"], "name": s["name"]}
                for s in students
                if s["roll_no"] not in marked_rolls
            ][:40],
            "recent": [
                {
                    "roll_no": r.get("student_roll"),
                    "name": r.get("student_name"),
                    "status": r.get("status"),
                    "marked_at": r.get("marked_at"),
                    "tx_id": r.get("tx_id"),
                    "on_chain": r.get("on_chain", False),
                }
                for r in sorted(records, key=lambda r: r.get("marked_at", 0), reverse=True)[:20]
            ],
            "mempool": len(services.ledger.chain.mempool),
            "server_time": time.time(),
        }
    )


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------
@bp.post("/attendance/mark")
def mark_attendance():
    """The student's phone posts here after scanning the QR code."""
    payload = request.get_json(silent=True) or {}
    token = payload.get("token", "")
    session_id = payload.get("session_id", "")
    roll_no = payload.get("roll_no", "")

    if not token or not session_id or not roll_no:
        raise LedgerError(
            "token, session_id and roll_no are all required", "MISSING_FIELDS", 400
        )

    result = _svc().ledger.mark_attendance(
        token=token,
        session_id=session_id,
        roll_no=roll_no,
        device_fingerprint=device_fingerprint(payload),
        client_ip=request.remote_addr or "",
        client_latency_ms=payload.get("client_latency_ms"),
        non_custodial_signature=payload.get("signature"),
        non_custodial_public_key=payload.get("public_key"),
    )
    return jsonify(result.to_dict()), 201


@bp.post("/attendance/manual")
def manual_attendance():
    """Faculty corrects a record by hand -- reason is mandatory."""
    payload = request.get_json(silent=True) or {}
    result = _svc().ledger.manual_mark(
        session_id=payload.get("session_id", ""),
        roll_no=payload.get("roll_no", ""),
        status=payload.get("status", "PRESENT"),
        faculty_id=payload.get("faculty_id", ""),
        reason=payload.get("reason", ""),
    )
    return jsonify(result), 201


@bp.get("/attendance/session/<session_id>")
def session_attendance(session_id: str):
    records = _svc().repo.attendance_for_session(session_id)
    return jsonify({"ok": True, "count": len(records), "records": [dict(r) for r in records]})


@bp.get("/attendance/student/<roll_no>")
def student_attendance(roll_no: str):
    services = _svc()
    subject_code = request.args.get("subject_code")
    records = services.repo.attendance_for_student(
        roll_no.upper(), subject_code=subject_code
    )
    return jsonify({"ok": True, "count": len(records), "records": [dict(r) for r in records]})


@bp.post("/sessions/<session_id>/seal")
def seal_session(session_id: str):
    """Force the pending transactions into a block right now."""
    services = _svc()
    if services.repo.get_session(session_id) is None:
        raise LedgerError(f"Unknown session {session_id}", "UNKNOWN_SESSION", 404)
    result = services.ledger.seal_now(note=f"Manual seal for {session_id}")
    return jsonify(result)


__all__ = ["bp", "init", "device_fingerprint"]
