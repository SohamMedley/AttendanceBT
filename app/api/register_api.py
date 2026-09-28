"""Register API: students, faculty, subjects."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services import Services
from ..services.ledger import LedgerError

bp = Blueprint("register_api", __name__, url_prefix="/api")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


def _public_student(student: dict) -> dict:
    """Strip anything that should not be public."""
    hidden = {"guardian_contact", "phone_masked", "email"}
    return {k: v for k, v in student.items() if k not in hidden and not k.startswith("_")}


@bp.get("/students")
def list_students():
    params = request.args
    students = _svc().repo.list_students(
        department=params.get("department"),
        year=params.get("year"),
        division=params.get("division"),
        active_only=params.get("active", "1") == "1",
    )
    return jsonify(
        {"ok": True, "count": len(students), "students": [_public_student(s) for s in students]}
    )


@bp.get("/students/<roll_no>")
def get_student(roll_no: str):
    student = _svc().repo.get_student(roll_no.upper())
    if student is None:
        raise LedgerError(f"No student with roll number {roll_no}", "UNKNOWN_STUDENT", 404)
    return jsonify({"ok": True, "student": _public_student(student)})


@bp.get("/subjects")
def list_subjects():
    params = request.args
    semester = params.get("semester")
    subjects = _svc().repo.list_subjects(
        semester=int(semester) if semester else None,
        department=params.get("department"),
    )
    return jsonify({"ok": True, "count": len(subjects), "subjects": [dict(s) for s in subjects]})


@bp.get("/faculty")
def list_faculty():
    faculty = _svc().repo.list_faculty(department=request.args.get("department"))
    return jsonify({"ok": True, "count": len(faculty), "faculty": [dict(f) for f in faculty]})


@bp.get("/register/summary")
def summary():
    services = _svc()
    students = services.repo.list_students()
    return jsonify(
        {
            "ok": True,
            "students": len(students),
            "faculty": len(services.repo.list_faculty()),
            "subjects": len(services.repo.list_subjects()),
            "by_year": {
                year: sum(1 for s in students if s.get("year") == year)
                for year in sorted({s.get("year") for s in students})
            },
        }
    )


__all__ = ["bp", "init"]
