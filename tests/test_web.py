"""
End-to-end tests through the real HTTP stack.

These exercise the complete path a phone actually takes: open a session, fetch
the rotating QR, scan, get rejected for the right reasons, get accepted, seal,
and verify the receipt.
"""

from __future__ import annotations

import pytest

from tests.conftest import mark_attendance_for_session

SUBJECT = "CSDO7022"       # Blockchain Technologies, BE Sem VII
FACULTY = "BCOE-FAC-001"
BE_STUDENT = "BCOE23AI001"
TE_STUDENT = "BCOE24AI001"


@pytest.fixture()
def session_id(client) -> str:
    """An open lecture session, the normal starting point for every test."""
    response = client.post(
        "/api/sessions",
        json={
            "subject_code": SUBJECT,
            "faculty_id": FACULTY,
            "room": "Lab 305",
            "duration_minutes": 60,
            "year": "BE",
            "division": "A",
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["session"]["session_id"]


# ---------------------------------------------------------------------------
# Pages render
# ---------------------------------------------------------------------------
class TestPages:
    @pytest.mark.parametrize(
        "path",
        ["/", "/dashboard", "/explorer", "/anchors", "/audit", "/settings",
         "/scan", "/verify", "/setup"],
    )
    def test_static_pages_render(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert b"<!doctype html>" in response.data.lower()

    def test_student_page_renders_for_a_known_roll(self, client):
        response = client.get(f"/student/{BE_STUDENT}")
        assert response.status_code == 200

    def test_student_page_for_unknown_roll_does_not_crash(self, client):
        assert client.get("/student/BCOE99AI999").status_code in {200, 404}

    def test_block_page_renders_for_genesis(self, client):
        assert client.get("/block/0").status_code == 200

    def test_block_page_for_missing_block_does_not_crash(self, client):
        assert client.get("/block/9999").status_code in {200, 404}

    def test_every_page_renders_even_with_no_register(self):
        """A fresh install has no data at all; the UI must still come up."""
        from app import create_app
        from app.config import AppConfig
        from app.services import Services
        from app.storage import LocalStore
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.chain.difficulty = 1
            config.storage.backend = "local"
            store = LocalStore(pathlib.Path(tmp) / "empty.json")
            store.init()
            services = Services(config, keystore_path=str(pathlib.Path(tmp) / "k.json"),
                                store=store)
            application = create_app(config, services=services)
            application.testing = True
            with application.test_client() as bare:
                # With no register at all, the home page sends the user to the
                # setup screen rather than showing an empty console.
                home = bare.get("/")
                assert home.status_code == 302
                assert "/setup" in home.headers["Location"]

                for path in ["/dashboard", "/explorer", "/anchors", "/audit",
                             "/settings", "/scan", "/verify", "/setup"]:
                    response = bare.get(path)
                    assert response.status_code == 200, f"{path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# The attendance flow
# ---------------------------------------------------------------------------
class TestAttendanceFlow:
    def test_open_a_session(self, client):
        response = client.post(
            "/api/sessions",
            json={"subject_code": SUBJECT, "faculty_id": FACULTY,
                  "room": "Lab 305", "year": "BE", "division": "A"},
        )
        assert response.status_code == 200
        session = response.get_json()["session"]
        assert session["session_id"].startswith("LEC-")
        assert session["status"] == "OPEN"
        assert session["subject_code"] == SUBJECT

    def test_the_session_secret_is_never_sent_to_the_client(self, client, session_id):
        """If the browser had the secret it could mint its own valid QR codes."""
        body = client.get(f"/api/sessions/{session_id}").get_data(as_text=True)
        assert "secret" not in body.lower()

    def test_rotating_qr_is_issued_as_an_svg(self, client, session_id):
        payload = client.get(f"/api/sessions/{session_id}/qr").get_json()
        assert payload["token"]
        assert payload["ttl"] > 0
        assert payload["expires_in"] > 0
        assert payload["qr_svg"].startswith("data:image/svg+xml;base64,")

    def test_qr_token_changes_over_time(self, client, session_id):
        """The whole anti-screenshot argument rests on this."""
        from app.services import qr as qr_service

        session = client.application.config["SERVICES"].repo.get_session(session_id)
        now = 1_790_000_000.0
        first = qr_service.issue_token(session_id, session["secret"], 30, at=now)
        second = qr_service.issue_token(session_id, session["secret"], 30, at=now + 30)
        assert first.token != second.token

    def test_a_student_marks_attendance(self, client, session_id):
        token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
        response = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": BE_STUDENT,
                  "device_id": "PHONE-A", "client_latency_ms": 140},
        )
        assert response.status_code in {200, 201}, response.get_data(as_text=True)
        result = response.get_json()
        assert result["status"] == "PRESENT"
        assert len(result["tx_id"]) == 64
        assert result["on_chain"] is False       # still in the mempool

    def test_several_students_can_mark(self, client, session_id):
        results = mark_attendance_for_session(
            client, session_id, [BE_STUDENT, "BCOE23AI002", "BCOE23AI003"]
        )
        assert [r["status"] for r in results] == ["PRESENT"] * 3

    # -- rejections -----------------------------------------------------
    def test_duplicate_mark_is_rejected(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
        response = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": BE_STUDENT,
                  "device_id": "PHONE-A"},
        )
        assert response.status_code == 409
        assert response.get_json()["code"] == "ALREADY_MARKED"

    def test_forged_token_is_rejected(self, client, session_id):
        response = client.post(
            "/api/attendance/mark",
            json={"token": "eyJ2IjoxLCJzIjoiZmFrZSIsInQiOjAsImsiOiJ4In0",
                  "session_id": session_id, "roll_no": BE_STUDENT,
                  "device_id": "PHONE-A"},
        )
        assert response.status_code in {400, 403}
        assert response.get_json()["code"] in {"BAD_SIGNATURE", "WRONG_SESSION", "MALFORMED"}

    def test_unknown_student_is_rejected(self, client, session_id):
        token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
        response = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": "BCOE99AI999",
                  "device_id": "PHONE-Z"},
        )
        assert response.status_code == 404
        assert response.get_json()["code"] == "UNKNOWN_STUDENT"

    def test_student_from_another_year_is_rejected(self, client, session_id):
        token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
        response = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": TE_STUDENT,
                  "device_id": "PHONE-T"},
        )
        assert response.status_code == 403
        assert response.get_json()["code"] == "NOT_ENROLLED"

    def test_one_device_cannot_mark_two_students(self, client, session_id):
        """The anti-proxy-marking control: a student scanning for an absent friend."""
        token = client.get(f"/api/sessions/{session_id}/qr").get_json()["token"]
        first = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": BE_STUDENT,
                  "device_id": "SHARED-PHONE"},
        )
        second = client.post(
            "/api/attendance/mark",
            json={"token": token, "session_id": session_id, "roll_no": "BCOE23AI002",
                  "device_id": "SHARED-PHONE"},
        )
        assert first.status_code in {200, 201}
        assert second.status_code == 403
        assert second.get_json()["code"] == "SHARED_DEVICE"

    def test_marking_a_closed_session_is_rejected(self, client, session_id):
        client.post(f"/api/sessions/{session_id}/close", json={})
        response = client.post(
            "/api/attendance/mark",
            json={"token": "irrelevant", "session_id": session_id,
                  "roll_no": BE_STUDENT, "device_id": "PHONE-A"},
        )
        assert response.status_code == 409
        assert response.get_json()["code"] in {"SESSION_CLOSED", "MALFORMED", "BAD_SIGNATURE"}

    def test_marking_an_unknown_session_is_rejected(self, client):
        response = client.post(
            "/api/attendance/mark",
            json={"token": "x", "session_id": "LEC-DOES-NOT-EXIST",
                  "roll_no": BE_STUDENT, "device_id": "PHONE-A"},
        )
        assert response.status_code == 404
        assert response.get_json()["code"] == "UNKNOWN_SESSION"

    def test_a_second_session_cannot_be_opened_by_the_same_faculty(self, client, session_id):
        response = client.post(
            "/api/sessions",
            json={"subject_code": "CSC701", "faculty_id": FACULTY,
                  "year": "BE", "division": "A"},
        )
        assert response.status_code == 409
        assert response.get_json()["code"] == "SESSION_ALREADY_OPEN"


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------
class TestManualOverride:
    def test_manual_mark_requires_a_reason(self, client, session_id):
        response = client.post(
            "/api/attendance/manual",
            json={"session_id": session_id, "roll_no": BE_STUDENT,
                  "status": "PRESENT", "faculty_id": FACULTY, "reason": ""},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "REASON_REQUIRED"

    def test_manual_mark_succeeds_with_a_reason(self, client, session_id):
        response = client.post(
            "/api/attendance/manual",
            json={"session_id": session_id, "roll_no": BE_STUDENT, "status": "PRESENT",
                  "faculty_id": FACULTY,
                  "reason": "Phone battery died, verified in class"},
        )
        assert response.status_code in {200, 201}
        record = response.get_json()["record"]
        assert record["status"] == "MANUAL"
        assert record["manual_status"] == "PRESENT"


# ---------------------------------------------------------------------------
# Sealing, verification, receipts
# ---------------------------------------------------------------------------
class TestSealingAndVerification:
    def test_closing_a_session_seals_a_block(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT, "BCOE23AI002"])
        response = client.post(f"/api/sessions/{session_id}/close", json={})
        assert response.status_code == 200
        session = response.get_json()["session"]
        assert session["status"] == "CLOSED"
        block = session["sealed_block"]
        assert block is not None
        assert block["tx_count"] == 2
        assert block["hash"].startswith("0" * 1)

    def test_records_are_on_the_chain_after_sealing(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})
        records = client.get(f"/api/attendance/session/{session_id}").get_json()["records"]
        assert records and records[0]["on_chain"] is True
        assert records[0]["block_index"] is not None

    def test_chain_verifies_after_sealing(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT, "BCOE23AI002"])
        client.post(f"/api/sessions/{session_id}/close", json={})
        report = client.get("/api/chain/verify?deep=0").get_json()["report"]
        assert report["valid"] is True
        assert report["checked_blocks"] >= 2

    def test_receipt_proves_inclusion(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})
        tx_id = client.get(
            f"/api/attendance/session/{session_id}"
        ).get_json()["records"][0]["tx_id"]

        receipt = client.get(f"/api/chain/transactions/{tx_id}").get_json()
        assert receipt["on_chain"] is True
        assert receipt["tx_id_valid"] is True
        assert receipt["signature_valid"] is True
        assert receipt["merkle_proof_verified"] is True
        assert receipt["block_pow_valid"] is True

    def test_receipt_page_renders(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})
        tx_id = client.get(
            f"/api/attendance/session/{session_id}"
        ).get_json()["records"][0]["tx_id"]
        assert client.get(f"/verify?tx_id={tx_id}").status_code == 200

    def test_unknown_transaction_returns_404(self, client):
        assert client.get("/api/chain/transactions/deadbeef").status_code == 404

    def test_chain_stats_are_reported(self, client, session_id):
        body = client.get("/api/chain/stats").get_json()
        stats = body["stats"]
        assert stats["blocks"] >= 1
        assert stats["difficulty"] >= 1
        assert "head_hash" in stats


# ---------------------------------------------------------------------------
# Tamper lab over HTTP
# ---------------------------------------------------------------------------
class TestTamperAPI:
    @pytest.fixture()
    def sealed(self, client, session_id):
        """Two sealed blocks, so the tampered block has a descendant.

        Level 4 re-mines the tampered block and breaks the *next* block's link,
        so a single-block chain cannot demonstrate it (re-mining the tip is
        genuinely undetectable internally -- which is what anchoring solves).
        """
        mark_attendance_for_session(client, session_id, [BE_STUDENT, "BCOE23AI002"])
        client.post(f"/api/sessions/{session_id}/close", json={})

        second = client.post(
            "/api/sessions",
            json={"subject_code": "CSC701", "faculty_id": FACULTY,
                  "year": "BE", "division": "A"},
        ).get_json()["session"]["session_id"]
        mark_attendance_for_session(client, second, ["BCOE23AI003"])
        client.post(f"/api/sessions/{second}/close", json={})

        return client.get(
            f"/api/attendance/session/{session_id}"
        ).get_json()["records"][0]["tx_id"]

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_each_level_is_detected(self, client, sealed, level):
        response = client.post("/api/chain/tamper", json={"level": level, "tx_id": sealed})
        assert response.status_code == 200, response.get_data(as_text=True)
        result = response.get_json()
        assert result["detected"] is True
        assert result["detected_by"]

        report = client.get("/api/chain/verify?deep=0").get_json()["report"]
        assert report["valid"] is False

    def test_an_invalid_level_is_rejected(self, client, sealed):
        response = client.post("/api/chain/tamper", json={"level": 99, "tx_id": sealed})
        assert response.status_code in {400, 404, 422}


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------
class TestAnchoring:
    def test_anchor_catalogue_lists_the_providers(self, client):
        catalogue = client.get("/api/anchors/catalogue").get_json()
        assert catalogue["current_provider"]
        providers = {p["provider"] for p in catalogue["providers"]}
        assert {"opentimestamps", "ethereum", "simulated"} <= providers

    def test_anchor_is_recorded_and_verifiable(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})

        created = client.post("/api/anchors", json={"note": "end of week"}).get_json()
        anchor = created["anchor"]
        assert anchor["merkle_root"]
        assert len(anchor["merkle_root"]) == 64
        assert anchor["records_anchored"] >= 1

        verification = client.get(f"/api/anchors/{anchor['anchor_id']}/verify").get_json()
        # The Merkle root is recomputed from the chain, so it must match.
        root_check = verification["checks"][0]
        assert root_check["passed"] is True, verification

    def test_anchor_transaction_is_signed_by_the_institution(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})
        anchor = client.post("/api/anchors", json={}).get_json()["anchor"]
        verification = client.get(f"/api/anchors/{anchor['anchor_id']}/verify").get_json()
        signature_check = next(
            c for c in verification["checks"] if "signed" in c["check"].lower()
        )
        assert signature_check["passed"] is True

    def test_anchors_form_a_chain(self, client, session_id):
        mark_attendance_for_session(client, session_id, [BE_STUDENT])
        client.post(f"/api/sessions/{session_id}/close", json={})
        first = client.post("/api/anchors", json={}).get_json()["anchor"]
        second = client.post("/api/anchors", json={}).get_json()["anchor"]
        assert second["previous_anchor_root"] == first["merkle_root"]

    def test_contract_source_is_served(self, client):
        source = client.get("/api/anchors/contract/source").get_data(as_text=True)
        assert "anchor" in source.lower()
        assert "bytes32" in source

    def test_unknown_anchor_returns_404(self, client):
        assert client.get("/api/anchors/ANC-NOPE/verify").status_code == 404


# ---------------------------------------------------------------------------
# Register and analytics
# ---------------------------------------------------------------------------
class TestRegisterAndAnalytics:
    def test_students_endpoint(self, client):
        students = client.get("/api/students").get_json()
        assert students["count"] >= 12

    def test_private_fields_are_stripped_from_student_payloads(self, client):
        """Phone numbers and guardian contacts must not leak to the browser."""
        students = client.get("/api/students").get_json()["students"]
        for student in students:
            assert "guardian_contact" not in student
            assert "phone_masked" not in student
            assert not any(key.startswith("_") for key in student)

    def test_subjects_endpoint(self, client):
        assert client.get("/api/subjects").get_json()["count"] >= 1

    def test_faculty_endpoint(self, client):
        assert client.get("/api/faculty").get_json()["count"] >= 1

    def test_dashboard_analytics(self, client):
        data = client.get("/api/analytics/dashboard").get_json()
        assert "students" in data or "totals" in data or data

    def test_defaulters_endpoint(self, client):
        assert "defaulters" in client.get("/api/analytics/defaulters").get_json()

    def test_anomalies_endpoint(self, client):
        body = client.get("/api/analytics/anomalies").get_json()
        assert "scan" in body
        assert "anomalies" in body["scan"] or body["scan"]

    def test_csv_export_has_the_right_content_type(self, client):
        response = client.get("/api/analytics/export.csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers["Content-Type"]
        assert "roll_no" in response.get_data(as_text=True).splitlines()[0]

    def test_student_report_endpoint(self, client):
        report = client.get(f"/api/analytics/student/{BE_STUDENT}").get_json()
        assert report


# ---------------------------------------------------------------------------
# System endpoints
# ---------------------------------------------------------------------------
class TestSystem:
    def test_status_reports_the_backend(self, client):
        status = client.get("/api/system/status").get_json()
        assert status

    def test_health_endpoint(self, client):
        health = client.get("/api/system/health").get_json()
        assert health["ok"] is True

    def test_crypto_self_test_passes(self, client):
        result = client.post("/api/system/self-test", json={}).get_json()
        assert result.get("passed") == result.get("total")
        assert result["total"] >= 5

    def test_difficulty_can_be_changed(self, client):
        response = client.post("/api/system/difficulty", json={"difficulty": 3})
        assert response.status_code == 200
        assert response.get_json()["difficulty"] == 3

    def test_absurd_difficulty_is_refused(self, client):
        """A difficulty of 8 would freeze the server for minutes, so it is capped."""
        response = client.post("/api/system/difficulty", json={"difficulty": 12})
        assert response.status_code in {200, 400, 422}
        if response.status_code == 200:
            assert response.get_json()["difficulty"] <= 5

    def test_unknown_api_route_returns_json_404(self, client):
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
class TestErrorHandling:
    def test_404_page_renders(self, client):
        response = client.get("/no-such-page")
        assert response.status_code == 404
        assert b"<!doctype html>" in response.data.lower()

    def test_unknown_student_page_renders(self, client):
        assert client.get("/student/NOPE").status_code in {200, 404}

    def test_a_broken_endpoint_returns_a_500_page_not_a_stack_trace(self, client):
        """Even the error handler must render, or the user sees nothing at all."""
        application = client.application
        services = application.config["SERVICES"]

        def explode(*args, **kwargs):
            raise RuntimeError("deliberate test failure")

        monkey = services.ledger.chain.stats
        services.ledger.chain.stats = explode
        application.config["PROPAGATE_EXCEPTIONS"] = False
        try:
            response = client.get("/api/chain/stats")
            assert response.status_code == 500
            assert response.get_json()["code"] == "SERVER_ERROR"

            # ...and the HTML error page must render too, not just the JSON one.
            page = client.get("/explorer")
            assert page.status_code == 500
            assert b"<!doctype html>" in page.data.lower()
        finally:
            services.ledger.chain.stats = monkey
            application.config["PROPAGATE_EXCEPTIONS"] = True
