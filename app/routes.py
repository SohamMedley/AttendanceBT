"""All the HTTP routes, in one file.

Nine pages and a handful of JSON endpoints:

    /                     what the project is, and the state of the chain
    /console              start a lecture roll call
    /session/<id>         the QR code to project, and who has marked so far
    /scan?t=...           the student's phone page
    /students             the class register
    /chain                every block
    /verify               check the chain, or prove one student's record
    /setup                build the demo register on a fresh deployment

Almost every page is server-rendered HTML. The only JavaScript is the camera on
/scan, the live list on /session/<id>, and copy-to-clipboard for hashes.
"""

from __future__ import annotations

import time
from typing import Any

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .data import check_token, current_token

bp = Blueprint("web", __name__)

#: Filled in by ``create_app`` so the routes can reach the database.
db = None
settings = None


def init(database, app_settings) -> None:
    global db, settings
    db = database
    settings = app_settings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def qr_svg(text: str, box_size: int = 8, border: int = 2) -> str:
    """Render a QR code as inline SVG (no image files, no JavaScript)."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:                                # pragma: no cover
        return ""
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=box_size, border=border)
    return image.to_string().decode("utf-8")


def scan_url(session_id: str, window: int, signature: str) -> str:
    """The link the QR code carries (absolute, so a phone camera can open it)."""
    return url_for(
        "web.scan",
        s=session_id,
        w=window,
        sig=signature,
        _external=True,
    )


def context(**extra: Any) -> dict[str, Any]:
    """Values every page needs."""
    base = {
        "settings": settings,
        "stats": db.stats(),
        "store_error": getattr(db, "store_error", ""),
        "now": time.time(),
    }
    base.update(extra)
    return base


def fail(message: str, status: int = 400):
    """Return an error: JSON for the API, a message page for a browser."""
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": message}), status
    return render_template("message.html", **context(title="Not allowed", message=message)), status


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@bp.get("/")
def home():
    if db.is_empty():
        return redirect(url_for("web.setup_page"))
    open_sessions = db.open_sessions()
    recent = db.sessions(limit=5)
    marked = {s["id"]: len(db.records_for(s["id"])) for s in open_sessions + recent}
    return render_template(
        "index.html",
        **context(
            title="Overview",
            open_sessions=open_sessions,
            recent=recent,
            marked=marked,
        ),
    )


@bp.get("/setup")
def setup_page():
    return render_template("setup.html", **context(title="Set up"))


@bp.post("/setup")
def setup_run():
    from .seed import seed_demo

    if not db.is_empty():
        return redirect(url_for("web.home"))
    seed_demo(db, settings)
    return redirect(url_for("web.console"))


@bp.get("/console")
def console():
    if db.is_empty():
        return redirect(url_for("web.setup_page"))
    sessions = db.sessions(limit=12)
    return render_template(
        "console.html",
        **context(
            title="Teacher console",
            subjects=settings.subjects,
            sessions=sessions,
            marked={s["id"]: len(db.records_for(s["id"])) for s in sessions},
            open_ids={s["id"] for s in db.open_sessions()},
        ),
    )


@bp.post("/console/session")
def open_session():
    try:
        session = db.open_session(
            subject_code=request.form.get("subject_code", ""),
            room=request.form.get("room", ""),
            minutes=request.form.get("minutes", 30),
        )
    except ValueError as exc:
        return fail(str(exc))
    return redirect(url_for("web.session_page", session_id=session["id"]))


@bp.get("/session/<session_id>")
def session_page(session_id: str):
    session = db.session(session_id)
    if session is None:
        return fail("That lecture session does not exist.", 404)
    token = current_token(session_id, settings.secret_key, settings.qr_ttl)
    return render_template(
        "session.html",
        **context(
            title=session["subject_name"],
            session=session,
            token=token,
            qr=qr_svg(scan_url(session_id, token["window"], token["signature"])),
            records=db.records_for(session_id),
            is_open=db.is_open(session),
        ),
    )


@bp.post("/session/<session_id>/close")
def close_session(session_id: str):
    try:
        result = db.close_session(session_id)
    except ValueError as exc:
        return fail(str(exc))
    block = result["block"]
    return redirect(url_for("web.chain_page", block=block.index))


@bp.get("/scan")
def scan():
    """The page a student opens by pointing their camera at the projector."""
    token = {
        "session_id": request.args.get("s", ""),
        "window": request.args.get("w", ""),
        "signature": request.args.get("sig", ""),
    }
    session = db.session(token["session_id"])
    problem = ""
    try:
        check_token(
            token["session_id"],
            token["window"],
            token["signature"],
            settings.secret_key,
            settings.qr_ttl,
        )
        if session is None:
            problem = "That lecture session does not exist."
        elif not db.is_open(session):
            problem = "This roll call has closed."
    except ValueError as exc:
        problem = str(exc)

    return render_template(
        "scan.html",
        **context(title="Mark attendance", token=token, session=session, problem=problem),
    )


@bp.get("/students")
def students_page():
    students = db.students()
    return render_template(
        "students.html",
        **context(
            title="Class register",
            students=students,
            percentages={s["roll_no"]: db.attendance_percentage(s["roll_no"]) for s in students},
        ),
    )


@bp.post("/students")
def add_student():
    try:
        db.add_student(
            roll_no=request.form.get("roll_no", ""),
            name=request.form.get("name", ""),
            division=request.form.get("division", "A"),
            year=request.form.get("year", "BE"),
            email=request.form.get("email", ""),
        )
    except ValueError as exc:
        return fail(str(exc))
    return redirect(url_for("web.students_page"))


@bp.get("/chain")
def chain_page():
    """Every block, newest first. `?block=N` also prints block N's records."""
    blocks = list(reversed(db.chain.to_list()))
    try:
        expanded = int(request.args.get("block", ""))
    except ValueError:
        expanded = None
    return render_template(
        "chain.html",
        **context(title="Blockchain", blocks=blocks, expanded=expanded),
    )


@bp.get("/verify")
def verify_page():
    return render_template("verify.html", **context(title="Verify"))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@bp.get("/api/health")
def api_health():
    return jsonify({"ok": True, "storage": db.store.name, "stats": db.stats()})


@bp.get("/api/session/<session_id>/token")
def api_token(session_id: str):
    """The next QR token, so the projector page can rotate it without reloading."""
    session = db.session(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "Unknown session"}), 404
    token = current_token(session_id, settings.secret_key, settings.qr_ttl)
    token["qr"] = qr_svg(scan_url(session_id, token["window"], token["signature"]))
    token["ok"] = True
    return jsonify(token)


@bp.get("/api/session/<session_id>/records")
def api_records(session_id: str):
    if db.session(session_id) is None:
        return jsonify({"ok": False, "error": "Unknown session"}), 404
    records = db.records_for(session_id)
    return jsonify({"ok": True, "count": len(records), "records": records})


@bp.post("/api/mark")
def api_mark():
    payload = request.get_json(silent=True) or request.form
    try:
        check_token(
            payload.get("session_id", ""),
            payload.get("window", ""),
            payload.get("signature", ""),
            settings.secret_key,
            settings.qr_ttl,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        record = db.mark(payload.get("session_id", ""), payload.get("roll_no", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        {
            "ok": True,
            "message": f"{record['name']} marked present.",
            "record": record,
            "sealed": False,
        }
    )


@bp.post("/api/verify")
def api_verify():
    ok, message = db.chain.is_valid()
    return jsonify({"ok": ok, "message": message, "blocks": len(db.chain.blocks)})


@bp.get("/api/prove")
def api_prove():
    """Prove that one student's attendance is inside the chain."""
    roll_no = request.args.get("roll_no", "").strip()
    session_id = request.args.get("session_id") or None
    if not roll_no:
        return jsonify({"ok": False, "error": "Enter a roll number."}), 400

    record = db.prove(roll_no, session_id)
    if record is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"No sealed attendance found for {roll_no.upper()}. "
                    "Marks only enter the chain when the teacher closes the roll call."
                ),
            }
        ), 404

    return jsonify({"ok": True, "proof": record, "chain_valid": db.chain.is_valid()[0]})


@bp.get("/api/student/<roll_no>")
def api_student(roll_no: str):
    student = db.student(roll_no)
    if student is None:
        return jsonify({"ok": False, "error": "Not in the register."}), 404
    records = db.records_for_student(roll_no)
    return jsonify(
        {
            "ok": True,
            "student": student,
            "percentage": db.attendance_percentage(roll_no),
            "records": records,
        }
    )
