"""
Shared pytest fixtures.

Every test gets a **completely isolated** environment: its own temporary JSON
ledger, its own keystore, and a low mining difficulty so the suite stays fast.
Nothing touches the real `data/` directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402
from app.services import Services  # noqa: E402
from app.storage import LocalStore  # noqa: E402

#: Difficulty 1 keeps Proof-of-Work effectively instant in tests. The consensus
#: logic exercised is identical -- only the target differs.
TEST_DIFFICULTY = 1


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.chain.difficulty = TEST_DIFFICULTY
    cfg.chain.seal_threshold = 1000        # we seal explicitly in most tests
    cfg.chain.node_name = "TEST-NODE"
    cfg.storage.backend = "local"
    cfg.storage.local_path = str(tmp_path / "ledger.json")
    cfg.session.grace_seconds = 300
    cfg.session.qr_token_ttl = 30
    cfg.debug = False
    return cfg


@pytest.fixture()
def store(config: AppConfig, tmp_path: Path) -> LocalStore:
    local = LocalStore(tmp_path / "ledger.json")
    local.init()
    return local


@pytest.fixture()
def services(config: AppConfig, store: LocalStore, tmp_path: Path) -> Services:
    return Services(config, keystore_path=str(tmp_path / "keys.json"), store=store)


@pytest.fixture()
def seeded(services: Services) -> Services:
    """A Services instance with a small register and subjects, no history."""
    from app.seed import build_seed_payload

    payload = build_seed_payload(students_per_year=6)
    services.repo.upsert_students(payload["students"])
    services.repo.upsert_many_faculty(payload["faculty"])
    services.repo.upsert_many_subjects(payload["subjects"])
    services.register_keys_for_students(payload["students"])
    services.register_keys_for_faculty(payload["faculty"])
    return services


@pytest.fixture()
def client(seeded: Services):
    """Flask test client wired to the isolated service layer."""
    from app import create_app

    application = create_app(seeded.config, services=seeded)
    application.testing = True
    with application.test_client() as test_client:
        yield test_client


def mark_attendance_for_session(client, session_id: str, rolls: list[str]) -> list[dict]:
    """Helper: fetch a fresh token and mark each roll number from its own device."""
    token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
    results = []
    for index, roll in enumerate(rolls):
        response = client.post(
            "/api/attendance/mark",
            json={
                "token": token,
                "session_id": session_id,
                "roll_no": roll,
                "device_id": f"TEST-DEVICE-{index}",
                "client_latency_ms": 100 + index,
            },
        )
        results.append(response.get_json())
    return results
