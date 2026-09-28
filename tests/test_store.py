"""The storage layer: the JSON file, and falling back to it when Firebase fails."""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.store import (
    LocalStore,
    StoreError,
    build_store,
    from_firestore_document,
    load_service_account,
    to_firestore_document,
)


def test_documents_survive_a_write_and_a_read(tmp_path):
    store = LocalStore(tmp_path / "ledger.json")
    store.write("students", "BCOE23AI001", {"roll_no": "BCOE23AI001", "name": "Ansh Vaze"})
    store.write("students", "BCOE23AI002", {"roll_no": "BCOE23AI002", "name": "Aarya Halde"})

    data = store.read_all()
    assert sorted(data["students"]) == ["BCOE23AI001", "BCOE23AI002"]
    assert data["students"]["BCOE23AI001"]["name"] == "Ansh Vaze"


def test_a_document_can_be_replaced_and_deleted(tmp_path):
    store = LocalStore(tmp_path / "ledger.json")
    store.write("students", "A", {"name": "first"})
    store.write("students", "A", {"name": "second"})
    assert store.read_all()["students"]["A"]["name"] == "second"

    store.delete("students", "A")
    assert store.read_all()["students"] == {}


def test_deleting_something_that_is_not_there_is_harmless(tmp_path):
    store = LocalStore(tmp_path / "ledger.json")
    store.delete("students", "nobody")
    assert store.read_all() == {}


def test_the_file_is_human_readable(tmp_path):
    """Half the point of the local backend: you can open it and edit it."""
    path = tmp_path / "ledger.json"
    LocalStore(path).write("students", "A", {"name": "Ansh"})

    text = path.read_text(encoding="utf-8")
    assert '"name": "Ansh"' in text
    assert json.loads(text)["students"]["A"]["name"] == "Ansh"


def test_a_corrupt_file_is_reported_not_ignored(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(StoreError):
        LocalStore(path).read_all()


def test_a_missing_file_is_simply_empty(tmp_path):
    assert LocalStore(tmp_path / "nothing-here.json").read_all() == {}


def test_health_counts_documents_per_collection(tmp_path):
    store = LocalStore(tmp_path / "ledger.json")
    store.write("students", "A", {"name": "x"})
    store.write("blocks", "0", {"index": 0})

    health = store.health()
    assert health["ok"] is True
    assert health["counts"]["students"] == 1
    assert health["counts"]["blocks"] == 1


# --------------------------------------------------------------------------
# Firebase
# --------------------------------------------------------------------------
def test_a_firestore_document_round_trips():
    document = {
        "roll_no": "BCOE23AI001",
        "count": 3,
        "percentage": 81.5,
        "present": True,
        "note": None,
        "tags": ["a", "b"],
        "nested": {"x": 1},
    }
    restored = from_firestore_document(to_firestore_document(document))
    assert restored == document


def test_the_service_account_can_be_a_path_or_the_json(tmp_path, monkeypatch):
    path = tmp_path / "service.json"
    path.write_text(json.dumps({"project_id": "demo"}), encoding="utf-8")

    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", str(path))
    assert load_service_account()["project_id"] == "demo"

    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", '{"project_id": "inline"}')
    assert load_service_account()["project_id"] == "inline"


def test_a_malformed_service_account_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    """A bad paste in the host's dashboard must not stop the app from booting.

    This is the failure a real deployment meets: the variable exists but is not
    valid JSON. The point of the fallback is that attendance keeps working.
    """
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo")
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", "arena/branch-name-not-json")

    settings = Settings(data_file=str(tmp_path / "ledger.json"))
    settings.storage_backend = "auto"
    settings.firebase_project_id = "demo"

    store, report = build_store(settings)

    assert store.name == "local-json"            # it started anyway
    assert report["fallback"] is True
    assert any("Firestore unavailable" in note for note in report["notes"])


def test_asking_for_firestore_without_credentials_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)

    settings = Settings(data_file=str(tmp_path / "ledger.json"))
    settings.storage_backend = "firestore"
    settings.firebase_project_id = ""

    store, report = build_store(settings)
    assert store.name == "local-json"
    assert report["fallback"] is True


def test_local_backend_is_used_when_asked_for(tmp_path):
    settings = Settings(data_file=str(tmp_path / "ledger.json"))
    settings.storage_backend = "local"
    store, report = build_store(settings)
    assert store.name == "local-json"
    assert report["fallback"] is False
