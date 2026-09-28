"""BCOE Blockchain Attendance System.

A mini project: attendance is recorded in a hash-chained ledger, so a mark
cannot be quietly changed afterwards.

    from app import create_app
    application = create_app()
"""

from __future__ import annotations

import logging

from flask import Flask, render_template

from .config import Settings, load_settings
from .data import Database
from .store import build_store

__version__ = "1.0"
log = logging.getLogger("bcoe")


def create_app(settings: Settings | None = None, store=None) -> Flask:
    """Build the Flask application."""
    settings = settings or load_settings()
    if store is None:
        store, report = build_store(settings)
    else:
        report = {"notes": ["Store supplied by the caller."], "fallback": False}

    database = Database(settings, store)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=settings.secret_key,
        TEMPLATES_AUTO_RELOAD=True,
        DATABASE=database,
        SETTINGS=settings,
        STORE_REPORT=report,
    )

    if not app.debug:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
        )

    from . import routes

    routes.init(database, settings)
    app.register_blueprint(routes.bp)

    @app.context_processor
    def inject_globals():
        return {
            "college": settings.college_name,
            "college_short": settings.college_short,
            "department": settings.college_department,
            "affiliation": settings.college_affiliation,
            "academic_year": settings.academic_year,
            "version": __version__,
            "storage_backend": store.name,
            "storage_notes": report.get("notes", []),
        }

    @app.template_filter("datetime")
    def format_datetime(value):
        import time

        if not value:
            return "--"
        return time.strftime("%d %b, %H:%M", time.localtime(float(value)))

    @app.template_filter("time_only")
    def format_time(value):
        import time

        if not value:
            return "--"
        return time.strftime("%H:%M:%S", time.localtime(float(value)))

    @app.template_filter("short_hash")
    def short_hash(value, head: int = 10, tail: int = 6):
        text = str(value or "")
        if len(text) <= head + tail + 3:
            return text
        return f"{text[:head]}...{text[-tail:]}"

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("message.html", title="Not found", message="There is no page at that address."), 404

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover - defensive
        log.exception("Unhandled error")
        return (
            render_template(
                "message.html",
                title="Something went wrong",
                message=f"{type(error).__name__}: {error}",
            ),
            500,
        )

    log.info(
        "%s | storage=%s | %d blocks, %d students",
        "Ready",
        store.name,
        len(database.chain.blocks),
        len(database.data["students"]),
    )
    return app
