"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app                        # noqa: E402
from app.config import Settings                   # noqa: E402
from app.data import Database                     # noqa: E402
from app.store import LocalStore                  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        secret_key="test-secret",
        difficulty=2,                             # keep mining fast in tests
        qr_ttl=30,
        data_file=str(tmp_path / "ledger.json"),
    )


@pytest.fixture
def store(settings) -> LocalStore:
    return LocalStore(settings.storage_path)


@pytest.fixture
def db(settings, store) -> Database:
    return Database(settings, store)


@pytest.fixture
def app(settings, store, db):
    """The app, wired to the same Database the test is holding.

    create_app() builds its own Database from the store, which would leave the
    test looking at an empty register while the app looked at another one.
    """
    application = create_app(settings, store)
    from app import routes

    routes.init(db, settings)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()
