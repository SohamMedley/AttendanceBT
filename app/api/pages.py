"""
Page routes.

Thin: each view gathers what its template needs and renders. Anything interactive
is handled by the JSON API from JavaScript, so the pages stay readable.
"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from ..services import Services

bp = Blueprint("pages", __name__)
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise RuntimeError("Service layer is not initialised")
    return _services


def _base_context(title: str, active: str, **extra) -> dict:
    services = _svc()
    return {
        "title": title,
        "active": active,
        "chain_stats": services.chain_stats(),
        "signature_audit": services.ledger.signature_audit_status(),
        **extra,
    }


# ---------------------------------------------------------------------------
@bp.get("/")
def home():
    services = _svc()
    if not services.is_seeded():
        return redirect(url_for("pages.setup"))

    return render_template(
        "home.html",
        **_base_context(
            "Overview",
            "home",
            dashboard=services.dashboard(),
            anchors=services.repo.list_anchors(limit=3),
        ),
    )


@bp.get("/setup")
def setup():
    services = _svc()
    return render_template(
        "setup.html",
        **_base_context("Setup", "setup", register=services.status()["register"]),
    )


@bp.get("/dashboard")
def dashboard():
    services = _svc()
    sessions = services.repo.list_sessions(limit=25)
    open_session = next((s for s in sessions if s.get("status") == "OPEN"), None)
    return render_template(
        "dashboard.html",
        **_base_context(
            "Faculty Dashboard",
            "dashboard",
            dashboard=services.dashboard(),
            sessions=sessions,
            open_session=open_session,
            subjects=services.repo.list_subjects(),
            faculty=services.repo.list_faculty(),
            students=services.repo.list_students(year="BE"),
        ),
    )


@bp.get("/session/<session_id>")
def session_view(session_id: str):
    services = _svc()
    session = services.repo.get_session(session_id)
    if session is None:
        abort(404)
    records = services.repo.attendance_for_session(session_id)
    return render_template(
        "session.html",
        **_base_context(
            f"Live Session - {session.get('subject_code')}",
            "dashboard",
            session={k: v for k, v in session.items() if k != "secret"},
            records=records,
        ),
    )


@bp.get("/scan")
def scan():
    """The page a student's phone opens after scanning the QR code."""
    return render_template(
        "scan.html",
        **_base_context(
            "Mark Attendance",
            "scan",
            token=request.args.get("token", ""),
            session_id=request.args.get("session", ""),
        ),
    )


@bp.get("/student/<roll_no>")
def student_view(roll_no: str):
    services = _svc()
    student = services.repo.get_student(roll_no.upper())
    if student is None:
        abort(404)

    from ..services.analytics import student_report

    report = student_report(
        student=student,
        subjects=[
            s for s in services.repo.list_subjects() if s.get("year") == student.get("year")
        ],
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
    )
    return render_template(
        "student.html",
        **_base_context(
            f"{student.get('name')} - Attendance",
            "students",
            student=student,
            report=report,
            recent=services.repo.attendance_for_student(student["roll_no"])[:12],
        ),
    )


@bp.get("/explorer")
def explorer():
    services = _svc()
    blocks = services.ledger.chain.chain[::-1][:30]
    return render_template(
        "explorer.html",
        **_base_context(
            "Blockchain Explorer",
            "explorer",
            blocks=[b.to_dict(include_transactions=False) for b in blocks],
            genesis=services.ledger.chain.chain[0].to_dict(include_transactions=False),
        ),
    )


@bp.get("/block/<int:index>")
def block_view(index: int):
    services = _svc()
    block = services.ledger.chain.block_by_index(index)
    if block is None:
        abort(404)
    data = block.to_dict(include_transactions=True)
    # Recomputed from scratch, not read from storage -- this is the check the
    # page displays, so it must not simply echo what was saved.
    data["integrity"] = {
        "hash_valid": block.hash_is_valid(),
        "merkle_valid": block.merkle_is_valid(),
        "proof_of_work_valid": block.pow_is_valid(),
        "recomputed_hash": block.recompute_hash(),
    }
    return render_template(
        "block.html",
        **_base_context(
            f"Block #{index}",
            "explorer",
            block=data,
            merkle=services.ledger.chain.merkle_tree(index),
        ),
    )


@bp.get("/verify")
def verify_page():
    services = _svc()
    tx_id = request.args.get("tx_id", "")
    receipt = None
    if tx_id:
        try:
            receipt = services.ledger.receipt(tx_id)
        except Exception:
            receipt = None
    return render_template(
        "verify.html",
        **_base_context("Verify a Record", "verify", tx_id=tx_id, receipt=receipt),
    )


@bp.get("/anchors")
def anchors_page():
    services = _svc()
    from ..anchoring import provider_catalogue

    return render_template(
        "anchors.html",
        **_base_context(
            "Public-chain Anchoring",
            "anchors",
            anchors=services.repo.list_anchors(limit=40),
            pending=services.anchoring.pending_summary(),
            providers=provider_catalogue(services.config),
        ),
    )


@bp.get("/audit")
def audit_page():
    services = _svc()
    from ..services.anomaly import scan as scan_anomalies

    return render_template(
        "audit.html",
        **_base_context(
            "Audit & Integrity",
            "audit",
            audit=services.repo.list_audit(limit=80),
            anomalies=scan_anomalies(
                attendance=services.repo.attendance_all(),
                sessions=services.repo.list_sessions(),
            ),
            audit_status=services.ledger.signature_audit_status(),
        ),
    )


@bp.get("/settings")
def settings_page():
    services = _svc()
    return render_template(
        "settings.html",
        **_base_context("System Settings", "settings", status=services.status()),
    )


__all__ = ["bp", "init"]
