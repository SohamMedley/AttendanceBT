"""Analytics API: dashboard, student reports, defaulters, anomaly alerts, CSV."""

from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, jsonify, request

from ..services import Services
from ..services.analytics import defaulter_list, export_rows, student_report
from ..services.anomaly import scan as scan_anomalies
from ..services.ledger import LedgerError

bp = Blueprint("analytics_api", __name__, url_prefix="/api/analytics")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


@bp.get("/dashboard")
def dashboard():
    return jsonify({"ok": True, "dashboard": _svc().dashboard()})


@bp.get("/student/<roll_no>")
def student(roll_no: str):
    services = _svc()
    record = services.repo.get_student(roll_no.upper())
    if record is None:
        raise LedgerError(f"No student with roll number {roll_no}", "UNKNOWN_STUDENT", 404)

    report = student_report(
        student=record,
        subjects=[
            s for s in services.repo.list_subjects() if s.get("year") == record.get("year")
        ],
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
    )
    report["recent"] = services.repo.attendance_for_student(record["roll_no"])[:15]
    report["identity"] = {
        "public_key": record.get("public_key"),
        "address": record.get("address"),
    }
    return jsonify({"ok": True, "report": report})


@bp.get("/defaulters")
def defaulters():
    services = _svc()
    threshold = float(request.args.get("threshold", 75))
    rows = defaulter_list(
        students=services.repo.list_students(),
        subjects=services.repo.list_subjects(),
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
        threshold=threshold,
    )
    return jsonify({"ok": True, "threshold": threshold, "count": len(rows), "defaulters": rows})


@bp.get("/anomalies")
def anomalies():
    services = _svc()
    result = scan_anomalies(
        attendance=services.repo.attendance_all(),
        sessions=services.repo.list_sessions(),
    )
    return jsonify({"ok": True, "scan": result})


@bp.get("/export.csv")
def export_csv():
    services = _svc()
    rows = export_rows(
        students=services.repo.list_students(),
        subjects=services.repo.list_subjects(),
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
    )
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bcoe_attendance_report.csv"},
    )


__all__ = ["bp", "init"]
