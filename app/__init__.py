"""
Flask application factory.

Kept deliberately thin: every route delegates to the service layer, so the same
business logic is reachable from the web UI, the CLI, and the test suite.

Run with ``python manage.py serve`` (development) or
``gunicorn "app:create_app()"`` (anything resembling production).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .config import AppConfig, load_config
from .ledger import Services, get_services

__version__ = "1.0.0"

log = logging.getLogger("bcoe")


def create_app(config: AppConfig | None = None, services: Services | None = None) -> Flask:
    """Build and configure the Flask application."""
    config = config or load_config()
    services = services or get_services(config)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=config.secret_key,
        JSON_SORT_KEYS=False,
        # Always reload templates on change: editing a page should not require
        # restarting the server, which matters when a student is presenting.
        TEMPLATES_AUTO_RELOAD=True,
        # Read by manage.py serve / any hosting wrapper.
        STORAGE_BACKEND_NAME=services.store.name,
        CHAIN_DIFFICULTY=config.chain.difficulty,
    )
    app.config["APP_CONFIG"] = config
    app.config["SERVICES"] = services
    app.config["COLLEGE"] = config.college
    app.config["VERSION"] = __version__

    if not app.debug:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        )

    _register_blueprints(app, services)
    _register_template_helpers(app, services)
    _register_error_handlers(app)

    log.info(
        "BCOE attendance chain ready | storage=%s | blocks=%d | difficulty=%d",
        services.store.name,
        len(services.ledger.chain.chain),
        services.ledger.chain.difficulty,
    )
    return app


# ---------------------------------------------------------------------------
def _register_blueprints(app: Flask, services: Services) -> None:
    """Wire the service layer into the blueprints, then mount them."""
    from .api import attendance, chain, pages

    # Each blueprint module keeps a module-level reference to the service layer
    # rather than importing it at module scope, so the app can be created with
    # any configuration (including a test one) without import-time state.
    for module in (pages, attendance, chain):
        module.init(services)

    app.register_blueprint(pages.bp)
    app.register_blueprint(attendance.bp)
    app.register_blueprint(chain.bp)


def _build_commit() -> str:
    """Short id of the code that is actually running.

    Hosts hand us the commit (``RENDER_GIT_COMMIT`` and friends); locally we ask
    git. The value is shown in the footer so "is my fix deployed yet?" is a
    glance instead of a guess.
    """
    for variable in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION", "HEROKU_SLUG_COMMIT"):
        value = os.environ.get(variable)
        if value:
            return value[:7]
    try:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip() or "local"
    except Exception:  # pragma: no cover - no git, no problem
        return "local"


def _register_template_helpers(app: Flask, services: Services) -> None:
    """Values every template needs (college identity, backend badge)."""

    def _safe_chain_stats() -> dict:
        """Chain statistics that can never themselves raise.

        Error handlers render the same layout as every other page, so if this
        blew up while handling an error, the user would see a blank 500 instead
        of the message explaining what went wrong.
        """
        try:
            return services.chain_stats()
        except Exception:  # pragma: no cover - defensive
            log.warning("Could not compute chain stats for template context")
            return {
                "blocks": 0,
                "height": 0,
                "sealed_attendance_transactions": 0,
                "mempool_size": 0,
                "difficulty": 0,
                "last_report_valid": True,
            }

    @app.context_processor
    def inject_globals():
        from .storage import store_report

        # `active`, `title` and `chain_stats` are supplied here so that *every*
        # template -- including the error pages -- can extend base.html safely.
        # Anything passed to render_template() still takes precedence.
        return {
            "college": app.config["COLLEGE"],
            "app_version": app.config["VERSION"],
            "build_commit": _build_commit(),
            "storage_backend": services.store.name,
            "storage_report": store_report(),
            "chain_difficulty": services.ledger.chain.difficulty,
            "academic_year": app.config["APP_CONFIG"].academic_year,
            "is_seeded": services.is_seeded(),
            "chain_stats": _safe_chain_stats(),
            "active": "",
            "title": "Attendance Chain",
        }

    @app.template_filter("datetime")
    def format_datetime(value):
        import time

        if value in (None, ""):
            return "--"
        try:
            return time.strftime("%d %b %Y, %H:%M", time.localtime(float(value)))
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("date")
    def format_date(value):
        import time

        if value in (None, ""):
            return "--"
        try:
            return time.strftime("%d %b %Y", time.localtime(float(value)))
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("time_only")
    def format_time(value):
        import time

        if value in (None, ""):
            return "--"
        try:
            return time.strftime("%H:%M:%S", time.localtime(float(value)))
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("short_hash")
    def short_hash(value, head: int = 10, tail: int = 6):
        """Abbreviate a hash for display: ``short_hash(8, 6)`` -> abcd1234...ef5678."""
        if not value:
            return "--"
        text = str(value)
        if len(text) <= head + tail + 3:
            return text
        return f"{text[:head]}...{text[-tail:]}"

    @app.template_filter("thousands")
    def thousands(value):
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value)


def _register_error_handlers(app: Flask) -> None:
    from .ledger import LedgerError

    @app.errorhandler(LedgerError)
    def handle_ledger_error(error: LedgerError):
        payload = error.to_dict()
        if request.path.startswith("/api/"):
            return jsonify(payload), error.status
        return (
            render_template(
                "error.html",
                title="Request rejected",
                active="",
                error={"code": payload.get("code"), "message": payload.get("error")},
            ),
            error.status,
        )

    @app.errorhandler(404)
    def handle_not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "NOT_FOUND", "error": "Unknown endpoint"}), 404
        return (
            render_template(
                "error.html",
                title="Page not found",
                active="",
                error={
                    "code": "NOT_FOUND",
                    "message": "That page does not exist. Check the address, or use "
                    "the navigation to get back.",
                },
            ),
            404,
        )

    @app.errorhandler(500)
    def handle_server_error(error):
        log.exception("Unhandled error on %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify(
                {"ok": False, "code": "SERVER_ERROR", "error": "Internal server error"}
            ), 500

        # Say why. An opaque "something went wrong" turns a five-second
        # diagnosis into a log hunt. Only the exception's own message is
        # shown -- never a traceback, and never a configuration value.
        original = getattr(error, "original_exception", None) or error
        detail = str(original) or original.__class__.__name__
        return (
            render_template(
                "error.html",
                title="Server error",
                active="",
                error={
                    "code": original.__class__.__name__,
                    "message": detail[:400],
                    "hint": "The ledger itself is unaffected - no data was lost.",
                },
            ),
            500,
        )


__all__ = ["create_app", "__version__"]
