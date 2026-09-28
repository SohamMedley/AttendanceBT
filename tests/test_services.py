"""
Services layer: rotating QR tokens, the attendance ledger, key custody,
analytics and storage round-trips.
"""

from __future__ import annotations

import pytest

from app.services import analytics
from app.services import (
    QRTokenError,
    current_slot,
    issue_token,
    new_session_secret,
    render_svg,
    token_fingerprint,
    verify_token,
)


# ---------------------------------------------------------------------------
# Rotating QR tokens
# ---------------------------------------------------------------------------
class TestRotatingQR:
    """The QR token is the anti-proxy-marking control, so it gets heavy testing."""

    @pytest.fixture()
    def session(self):
        return {"id": "LEC-20260929-100000-CSDO7022-A-ab12", "secret": new_session_secret()}

    def test_fresh_token_is_accepted(self, session):
        token = issue_token(session["id"], session["secret"], ttl=30)
        info = verify_token(
            token.token, secret=session["secret"], session_id=session["id"], ttl=30
        )
        assert info["fresh"] is True
        assert info["session_id"] == session["id"]

    def test_token_from_a_different_secret_is_rejected(self, session):
        """Forging a token requires the session secret, which never leaves the server."""
        token = issue_token(session["id"], new_session_secret(), ttl=30)
        with pytest.raises(QRTokenError) as exc:
            verify_token(
                token.token, secret=session["secret"], session_id=session["id"], ttl=30
            )
        assert exc.value.code == "BAD_SIGNATURE"

    def test_expired_token_is_rejected(self, session):
        issued = issue_token(session["id"], session["secret"], ttl=30, at=1_790_000_000.0)
        # 5 minutes later -- far beyond the +/-1 slot skew allowance.
        with pytest.raises(QRTokenError) as exc:
            verify_token(
                issued.token, secret=session["secret"], session_id=session["id"],
                ttl=30, at=1_790_000_300.0,
            )
        assert exc.value.code == "EXPIRED"

    def test_token_cannot_be_replayed_into_another_session(self, session):
        """Cross-session replay: a valid token for LEC-A must not open LEC-B."""
        other = "LEC-20260929-110000-CSC701-A-cd34"
        token = issue_token(session["id"], session["secret"], ttl=30)
        with pytest.raises(QRTokenError) as exc:
            verify_token(
                token.token, secret=session["secret"], session_id=other, ttl=30
            )
        assert exc.value.code == "WRONG_SESSION"

    def test_previous_slot_is_still_accepted(self, session):
        """One slot of clock skew is tolerated so a scan at the boundary works."""
        now = 1_790_000_000.0
        previous = issue_token(
            session["id"], session["secret"], ttl=30, at=now - 30
        )
        info = verify_token(
            previous.token, secret=session["secret"], session_id=session["id"],
            ttl=30, at=now,
        )
        assert info["fresh"] is False

    def test_token_from_the_future_is_rejected(self, session):
        now = 1_790_000_000.0
        future = issue_token(session["id"], session["secret"], ttl=30, at=now + 600)
        with pytest.raises(QRTokenError) as exc:
            verify_token(
                future.token, secret=session["secret"], session_id=session["id"],
                ttl=30, at=now,
            )
        assert exc.value.code == "NOT_YET_VALID"

    def test_malformed_tokens_are_rejected_cleanly(self, session):
        for bad in ("", "not-a-token", "eyJ4IjoxfQ", "!!!!", "a.b.c"):
            with pytest.raises(QRTokenError):
                verify_token(
                    bad, secret=session["secret"], session_id=session["id"], ttl=30
                )

    def test_unsupported_version_is_rejected(self, session):
        import base64
        import hashlib
        import hmac as hmac_module
        import json

        payload = {"v": 99, "s": session["id"], "t": current_slot(30), "k": "x"}
        token = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        with pytest.raises(QRTokenError) as exc:
            verify_token(
                token, secret=session["secret"], session_id=session["id"], ttl=30
            )
        assert exc.value.code == "VERSION"

    def test_the_secret_never_appears_in_the_token(self, session):
        token = issue_token(session["id"], session["secret"], ttl=30)
        assert session["secret"] not in token.token

    def test_two_consecutive_slots_produce_different_tokens(self, session):
        first = issue_token(session["id"], session["secret"], ttl=30, at=1_790_000_000.0)
        second = issue_token(session["id"], session["secret"], ttl=30, at=1_790_000_030.0)
        assert first.token != second.token

    def test_same_slot_produces_the_same_token(self, session):
        first = issue_token(session["id"], session["secret"], ttl=30, at=1_790_000_000.0)
        second = issue_token(session["id"], session["secret"], ttl=30, at=1_790_000_005.0)
        assert first.token == second.token

    def test_fingerprint_is_short_and_stable(self, session):
        token = issue_token(session["id"], session["secret"], ttl=30)
        assert token_fingerprint(token.token) == token_fingerprint(token.token)
        assert len(token_fingerprint(token.token)) == 16

    def test_svg_rendering_produces_a_scannable_image(self, session):
        token = issue_token(session["id"], session["secret"], ttl=30)
        svg = render_svg(token.token, box_size=4, border=2)
        assert svg.lstrip().startswith("<")
        assert "<svg" in svg
        assert len(svg) > 500

    def test_session_secrets_are_unique(self):
        assert len({new_session_secret() for _ in range(50)}) == 50


# ---------------------------------------------------------------------------
# Storage round-trips
# ---------------------------------------------------------------------------
class TestStorage:
    def test_repository_round_trips_a_student(self, services):
        services.repo.upsert_student({"roll_no": "BCOE23AI001", "name": "Ansh Vaze"})
        assert services.repo.get_student("BCOE23AI001")["name"] == "Ansh Vaze"

    def test_data_survives_a_process_restart(self, config, tmp_path):
        """The bug this guards against: reopening the file must not lose everything."""
        from app.ledger import Services
        from app.storage import LocalStore

        path = tmp_path / "restart.json"
        first_store = LocalStore(path)
        first_store.init()
        first = Services(config, keystore_path=str(tmp_path / "k1.json"), store=first_store)
        first.repo.upsert_student({"roll_no": "BCOE23AI007", "name": "Kunal Dhamale"})
        first.repo.set_meta("probe", {"ok": True})

        # A brand new store + service layer, as if the process had restarted.
        second_store = LocalStore(path)
        second_store.init()
        second = Services(config, keystore_path=str(tmp_path / "k2.json"), store=second_store)
        assert second.repo.get_student("BCOE23AI007")["name"] == "Kunal Dhamale"
        assert second.repo.get_meta("probe") == {"ok": True}

    def test_repository_counts_and_filters(self, seeded):
        be = seeded.repo.list_students(year="BE")
        te = seeded.repo.list_students(year="TE")
        assert len(be) == 6 and len(te) == 6
        assert seeded.repo.student_count() == 12

    def test_unknown_document_returns_none(self, services):
        assert services.repo.get_student("NOPE") is None
        assert services.repo.get_subject("NOPE") is None
        assert services.repo.get_anchor("NOPE") is None

    def test_audit_log_never_drops_entries(self, services):
        """Two identical actions in the same millisecond must BOTH survive.

        An earlier implementation built the id from hash((action, actor, target)),
        so rapid duplicates collided and one entry silently overwrote the other.
        """
        services.repo.log_audit("TEST", actor="pytest", detail={"a": 1})
        services.repo.log_audit("TEST", actor="pytest", detail={"a": 2})
        services.repo.log_audit("TEST", actor="pytest", detail={"a": 3})

        entries = services.repo.list_audit(limit=10)
        assert len(entries) >= 3
        assert len({entry["audit_id"] for entry in entries}) == len(entries)
        assert {entry["detail"]["a"] for entry in entries} >= {1, 2, 3}


# ---------------------------------------------------------------------------
# Key custody
# ---------------------------------------------------------------------------
class TestIdentity:
    def test_keys_are_created_once_and_reused(self, services):
        first = services.keystore.create("BCOE23AI001", role="student")
        second = services.keystore.get("BCOE23AI001")
        assert first.public_key == second.public_key

    def test_a_malformed_service_account_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch
    ):
        """A broken FIREBASE_SERVICE_ACCOUNT must fall back, never fail to boot.

        This is the failure a deploy actually meets: the variable exists but is
        not valid JSON (a truncated paste, or the wrong value pasted entirely).
        `build_store` used to catch a hand-picked list of exception types that
        did not include JWTError, so the whole application failed to start
        instead of quietly running on the local store -- the exact opposite of
        what the fallback is for.
        """
        from app.config import AppConfig
        from app.storage import build_store

        monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", "arena/branch-name-not-json")

        cfg = AppConfig()
        cfg.storage.backend = "auto"
        cfg.storage.firebase_project_id = "test-project"
        cfg.storage.firebase_service_account = ""      # no file, inline value only
        cfg.storage.local_path = str(tmp_path / "fallback.json")

        store, report = build_store(cfg)

        assert store.name == "local-json"              # it started anyway
        assert report["fallback"] is True
        assert any("not valid JSON" in note for note in report["notes"])

    def test_keys_survive_a_wiped_filesystem(self, config, tmp_path):
        """The keystore mirrors itself into a non-local store.

        On a host with an ephemeral filesystem (any container host), the file
        copy of the keystore is gone after every deploy. Without the mirror, the
        first attendance mark after a redeploy would fail with NO_IDENTITY even
        though every record is safely on the chain.
        """
        from app.ledger import Services
        from app.storage import LocalStore

        class FakeCloudStore(LocalStore):
            name = "fake-cloud"

        cloud = FakeCloudStore(tmp_path / "cloud.json")
        cloud.init()

        first = Services(config, keystore_path=str(tmp_path / "keys-a.json"), store=cloud)
        first.keystore.create("BCOE23AI001", role="student")
        first.keystore.save()
        original = first.keystore.get("BCOE23AI001").private_key

        # A redeploy: same cloud store, brand new (empty) filesystem.
        second = Services(config, keystore_path=str(tmp_path / "keys-b.json"), store=cloud)
        recovered = second.keystore.get("BCOE23AI001")
        assert recovered is not None
        assert recovered.private_key == original

    def test_different_owners_get_different_keys(self, seeded):
        a = seeded.keystore.get("BCOE23AI001")
        b = seeded.keystore.get("BCOE23AI002")
        assert a.public_key != b.public_key
        assert a.address != b.address

    def test_ownership_verification(self, services):
        from app.blockchain import ecdsa

        identity = services.keystore.create("BCOE23AI099", role="student")
        message = b"prove it is me"
        signature = ecdsa.sign_message(identity.private_key, message)
        assert services.keystore.verify_ownership(
            "BCOE23AI099", identity.public_key, message, signature
        )
        assert not services.keystore.verify_ownership(
            "BCOE23AI099", identity.public_key, b"a different message", signature
        )

    def test_public_dict_never_leaks_the_private_key(self, services):
        identity = services.keystore.create("BCOE23AI098", role="student")
        public = identity.to_public_dict()
        assert identity.private_key not in str(public)

    def test_institution_identity_is_stable(self, services):
        from app.services import ensure_institution_identity

        first = ensure_institution_identity(services.keystore)
        second = ensure_institution_identity(services.keystore)
        assert first.public_key == second.public_key


# ---------------------------------------------------------------------------
# Analytics: the 75% rule
# ---------------------------------------------------------------------------
class TestAnalytics:
    """The 75% rule and the reports built on it."""

    @staticmethod
    def _record(roll, status, session, subject="CSDO7022", manual_status=None):
        record = {
            "tx_id": f"tx-{roll}-{session}",
            "session_id": session,
            "student_roll": roll,
            "status": status,
            "subject_code": subject,
            "marked_at": 1_790_000_000.0,
        }
        if manual_status:
            record["manual_status"] = manual_status
        return record

    @staticmethod
    def _sessions(count, subject="CSDO7022", year="BE", division="A"):
        return [
            {
                "session_id": f"L{index}", "subject_code": subject, "year": year,
                "division": division, "started_at": float(index), "status": "CLOSED",
            }
            for index in range(count)
        ]

    # -- lectures needed to recover -------------------------------------
    def test_lectures_to_recover_solves_the_75_percent_rule(self):
        from app.services import _lectures_to_recover

        # (attended + x) / (held + x) >= 0.75, smallest integer x.
        # None means the target is already met; 0 means no lectures were held.
        assert _lectures_to_recover(attended=3, held=4) is None   # exactly 75%
        assert _lectures_to_recover(attended=4, held=4) is None
        assert _lectures_to_recover(attended=8, held=10) is None  # 80%
        assert _lectures_to_recover(attended=0, held=0) == 0
        assert _lectures_to_recover(attended=6, held=10) == 6     # 6/10 -> 12/16
        assert _lectures_to_recover(attended=0, held=1) == 3
        assert _lectures_to_recover(attended=1, held=2) == 2
        assert _lectures_to_recover(attended=7, held=10) == 2

    def test_recovery_actually_restores_eligibility(self):
        from app.services import _lectures_to_recover

        for attended in range(0, 10):
            held = 10
            x = _lectures_to_recover(attended, held)
            if x is None:
                assert attended / held >= 0.75
            else:
                assert (attended + x) / (held + x) >= 0.75
                if x > 0:
                    assert (attended + x - 1) / (held + x - 1) < 0.75

    # -- which statuses count -------------------------------------------
    def test_present_and_late_count_as_attended(self):
        from app.services import _counts_as_attended

        assert _counts_as_attended({"status": "PRESENT"})
        assert _counts_as_attended({"status": "LATE"})
        assert not _counts_as_attended({"status": "ABSENT"})

    def test_manual_only_counts_when_it_says_present(self):
        """A manual record has to record what it actually decided."""
        from app.services import _counts_as_attended

        assert _counts_as_attended({"status": "MANUAL", "manual_status": "PRESENT"})
        assert _counts_as_attended({"status": "MANUAL", "manual_status": "LATE"})
        assert not _counts_as_attended({"status": "MANUAL", "manual_status": "ABSENT"})
        assert not _counts_as_attended({"status": "MANUAL"})

    # -- per-subject summary --------------------------------------------
    def test_student_subject_summary(self):
        from app.services import student_subject_summary

        student = {"roll_no": "BCOE23AI001", "name": "Ansh",
                   "year": "BE", "division": "A"}
        subject = {"code": "CSDO7022", "name": "Blockchain Technologies"}
        summary = student_subject_summary(
            student=student,
            subject=subject,
            sessions=self._sessions(4),
            attendance=[
                self._record("BCOE23AI001", "PRESENT", "L0"),
                self._record("BCOE23AI001", "LATE", "L1"),
                self._record("BCOE23AI001", "ABSENT", "L2"),
                self._record("BCOE23AI001", "ABSENT", "L3"),
            ],
        )
        assert summary["held"] == 4
        assert summary["attended"] == 2          # PRESENT + LATE
        assert summary["absent"] == 2
        assert summary["late"] == 1
        assert summary["percentage"] == 50.0
        assert summary["eligible"] is False
        assert summary["lectures_to_recover"] == 4   # (2+4)/(4+4) = 75%

    def test_attendance_from_another_subject_is_not_counted(self):
        from app.services import student_subject_summary

        student = {"roll_no": "BCOE23AI001", "year": "BE", "division": "A"}
        summary = student_subject_summary(
            student=student,
            subject={"code": "CSDO7022"},
            sessions=self._sessions(2, subject="CSC701"),   # different subject
            attendance=[self._record("BCOE23AI001", "PRESENT", "L0")],
        )
        assert summary["attended"] == 0

    def test_a_student_who_never_attends_is_ineligible(self):
        from app.services import student_subject_summary

        summary = student_subject_summary(
            student={"roll_no": "BCOE23AI009", "year": "BE", "division": "A"},
            subject={"code": "CSDO7022"},
            sessions=self._sessions(10),
            attendance=[],
        )
        assert summary["held"] == 10
        assert summary["attended"] == 0
        assert summary["eligible"] is False

    # -- defaulter list --------------------------------------------------
    def test_defaulter_list_separates_good_from_poor(self):
        from app.services import defaulter_list

        students = [
            {"roll_no": "BCOE23AI001", "name": "Good", "year": "BE", "division": "A"},
            {"roll_no": "BCOE23AI002", "name": "Poor", "year": "BE", "division": "A"},
        ]
        attendance = [self._record("BCOE23AI001", "PRESENT", f"L{i}") for i in range(10)]
        attendance.append(self._record("BCOE23AI002", "PRESENT", "L0"))
        attendance += [self._record("BCOE23AI002", "ABSENT", f"L{i}") for i in range(1, 10)]

        defaulters = defaulter_list(
            students=students,
            subjects=[{"code": "CSDO7022", "name": "Blockchain"}],
            sessions=self._sessions(10),
            attendance=attendance,
        )
        rolls = {row["roll_no"] for row in defaulters}
        assert "BCOE23AI002" in rolls
        assert "BCOE23AI001" not in rolls
        poor = next(row for row in defaulters if row["roll_no"] == "BCOE23AI002")
        assert poor["percentage"] == 10.0
        assert poor["shortfall"] == 65.0

    def test_defaulter_list_is_empty_when_everyone_is_above_the_bar(self):
        from app.services import defaulter_list

        students = [{"roll_no": "BCOE23AI001", "year": "BE", "division": "A"}]
        attendance = [self._record("BCOE23AI001", "PRESENT", f"L{i}") for i in range(10)]
        assert defaulter_list(
            students=students,
            subjects=[{"code": "CSDO7022"}],
            sessions=self._sessions(10),
            attendance=attendance,
        ) == []

    def test_empty_register_does_not_crash(self):
        from app.services import defaulter_list

        assert defaulter_list(
            students=[], subjects=[], sessions=[], attendance=[]
        ) == []

    # -- dashboard -------------------------------------------------------
    def test_turnout_counts_lectures_not_records(self):
        """A record only exists when a student scans, so dividing by the number
        of records would always report 100% attendance -- a metric that measures
        nothing. The denominator must be lectures held x students enrolled.
        """
        from app.services import dashboard_stats

        students = [
            {"roll_no": "BCOE23AI001", "year": "BE", "division": "A"},
            {"roll_no": "BCOE23AI002", "year": "BE", "division": "A"},
            {"roll_no": "BCOE23AI003", "year": "BE", "division": "A"},
            {"roll_no": "BCOE23AI004", "year": "BE", "division": "A"},
        ]
        sessions = self._sessions(10)          # 4 students x 10 lectures = 40 marks
        attendance = [self._record("BCOE23AI001", "PRESENT", f"L{i}") for i in range(10)]

        data = dashboard_stats(
            students=students, subjects=[{"code": "CSDO7022"}], sessions=sessions,
            attendance=attendance, chain_stats={}, anchors=[],
        )
        assert data["expected_marks"] == 40
        assert data["average_attendance_percentage"] == 25.0   # 10 of 40

    def test_turnout_with_no_sessions_is_zero_not_a_crash(self):
        from app.services import dashboard_stats

        data = dashboard_stats(
            students=[{"roll_no": "BCOE23AI001", "year": "BE", "division": "A"}],
            subjects=[], sessions=[], attendance=[], chain_stats={}, anchors=[],
        )
        assert data["average_attendance_percentage"] == 0.0

    # -- CSV export ------------------------------------------------------
    def test_export_rows_are_one_line_per_student_per_subject(self):
        from app.services import export_rows

        rows = export_rows(
            students=[
                {"roll_no": "BCOE23AI001", "name": "Ansh", "year": "BE", "division": "A"},
                {"roll_no": "BCOE23AI002", "name": "Aarya", "year": "BE", "division": "A"},
            ],
            subjects=[{"code": "CSDO7022", "name": "Blockchain"},
                      {"code": "CSC701", "name": "Deep Learning"}],
            sessions=self._sessions(2) + self._sessions(2, subject="CSC701"),
            attendance=[self._record("BCOE23AI001", "PRESENT", "L0")],
        )
        assert len(rows) == 4
        assert {"roll_no", "subject_code", "lectures_held", "percentage",
                "eligible_75"} <= set(rows[0])
        assert rows[0]["eligible_75"] in {"YES", "NO"}
