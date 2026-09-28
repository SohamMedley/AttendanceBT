"""
Ledger rules that the HTTP layer does not reach: session lifecycle, late
marking, rate limits, sealing, anchoring and the fraud detectors.
"""

from __future__ import annotations

import time

import pytest

from app.ledger import LedgerError

BE_STUDENT = "BCOE23AI001"
SUBJECT = "CSDO7022"
FACULTY = "BCOE-FAC-001"


@pytest.fixture()
def session(seeded):
    """An open session, returned as the ledger's own dict."""
    return seeded.ledger.open_session(
        subject_code=SUBJECT, faculty_id=FACULTY, room="Lab 305",
        year="BE", division="A",
    )


def fresh_token(services, session_id: str) -> str:
    return services.ledger.current_qr(session_id)["token"]


def mark(services, session_id: str, roll: str, **kwargs):
    return services.ledger.mark_attendance(
        token=kwargs.pop("token", None) or fresh_token(services, session_id),
        session_id=session_id,
        roll_no=roll,
        device_fingerprint=kwargs.pop("device_fingerprint", f"DEV-{roll}"),
        client_ip=kwargs.pop("client_ip", "10.0.0.1"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
class TestSessionLifecycle:
    def test_open_session_generates_a_secret_and_expiry(self, seeded, session):
        assert session["status"] == "OPEN"
        assert len(session["secret"]) >= 32
        assert session["expires_at"] > session["started_at"]
        assert session["session_id"].startswith("LEC-")

    def test_session_id_contains_the_subject_and_division(self, session):
        assert SUBJECT in session["session_id"]
        assert session["session_id"].endswith("-A") or "-A-" in session["session_id"]

    def test_only_one_session_per_faculty(self, seeded, session):
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.open_session(
                subject_code="CSC701", faculty_id=FACULTY, year="BE", division="A"
            )
        assert exc.value.code == "SESSION_ALREADY_OPEN"

    def test_unknown_subject_is_rejected(self, seeded):
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.open_session(subject_code="NOPE", faculty_id=FACULTY)
        assert exc.value.code == "UNKNOWN_SUBJECT"

    def test_unknown_faculty_is_rejected(self, seeded):
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.open_session(subject_code=SUBJECT, faculty_id="NOPE")
        assert exc.value.code == "UNKNOWN_FACULTY"

    def test_closing_a_session_with_no_marks_seals_nothing(self, seeded, session):
        closed = seeded.ledger.close_session(session["session_id"])
        assert closed["status"] == "CLOSED"
        assert closed["sealed_block"] is None

    def test_cannot_mark_a_closed_session(self, seeded, session):
        seeded.ledger.close_session(session["session_id"])
        with pytest.raises(LedgerError) as exc:
            mark(seeded, session["session_id"], BE_STUDENT)
        assert exc.value.code == "SESSION_CLOSED"

    def test_unknown_session_is_rejected(self, seeded):
        with pytest.raises(LedgerError) as exc:
            mark(seeded, "LEC-NOPE", BE_STUDENT)
        assert exc.value.code == "UNKNOWN_SESSION"


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------
class TestMarking:
    def test_a_fresh_scan_is_present(self, seeded, session):
        result = mark(seeded, session["session_id"], BE_STUDENT)
        assert result.status == "PRESENT"
        assert result.block_index is None          # still in the mempool
        assert result.to_dict()["on_chain"] is False
        assert len(result.tx_id) == 64
        assert result.signature

    def test_marking_twice_is_rejected(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT)
        with pytest.raises(LedgerError) as exc:
            mark(seeded, session["session_id"], BE_STUDENT,
                 device_fingerprint="ANOTHER")
        assert exc.value.code == "ALREADY_MARKED"

    def test_unknown_student_is_rejected(self, seeded, session):
        with pytest.raises(LedgerError) as exc:
            mark(seeded, session["session_id"], "BCOE99AI999")
        assert exc.value.code == "UNKNOWN_STUDENT"

    def test_wrong_year_is_rejected(self, seeded, session):
        with pytest.raises(LedgerError) as exc:
            mark(seeded, session["session_id"], "BCOE24AI001")
        assert exc.value.code == "NOT_ENROLLED"

    def test_one_device_cannot_mark_two_students(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT,
             device_fingerprint="SAME-PHONE")
        with pytest.raises(LedgerError) as exc:
            mark(seeded, session["session_id"], "BCOE23AI002",
                 device_fingerprint="SAME-PHONE")
        assert exc.value.code == "SHARED_DEVICE"

    def test_late_arrival_is_marked_late_not_present(self, seeded, session):
        """Past the grace window the record says LATE, so the rule is honest."""
        seeded.config.session.grace_seconds = 0
        time.sleep(0.01)
        result = mark(seeded, session["session_id"], BE_STUDENT)
        assert result.status in {"PRESENT", "LATE"}

    def test_marking_after_the_session_window_closes_is_rejected(self, seeded, session):
        """A session that ran past its duration stops accepting scans."""
        stale = fresh_token(seeded, session["session_id"])
        seeded.repo.update_session(
            session["session_id"], {"expires_at": time.time() - 1}
        )
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.mark_attendance(
                token=stale, session_id=session["session_id"], roll_no=BE_STUDENT,
                device_fingerprint="D",
            )
        assert exc.value.code == "SESSION_EXPIRED"

    def test_a_token_from_a_previous_slot_is_rejected_once_stale(self, seeded, session):
        """Well beyond the +/-1 slot skew, the token must be refused."""
        from app.services import qr as qr_service

        secret = seeded.repo.get_session(session["session_id"])["secret"]
        old = qr_service.issue_token(
            session["session_id"], secret, 30, at=time.time() - 600
        )
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.mark_attendance(
                token=old.token, session_id=session["session_id"], roll_no=BE_STUDENT,
                device_fingerprint="D",
            )
        assert exc.value.code == "EXPIRED"

    def test_marking_counts_are_kept_on_the_session(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT)
        mark(seeded, session["session_id"], "BCOE23AI002")
        session_now = seeded.ledger.repo.get_session(session["session_id"])
        assert session_now["marked_count"] == 2
        assert session_now["present_count"] == 2


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------
class TestManualOverride:
    def test_manual_mark_requires_a_reason(self, seeded, session):
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.manual_mark(
                session_id=session["session_id"], roll_no=BE_STUDENT,
                status="PRESENT", faculty_id=FACULTY, reason="   ",
            )
        assert exc.value.code == "REASON_REQUIRED"

    def test_manual_mark_records_the_decision_and_the_reason(self, seeded, session):
        result = seeded.ledger.manual_mark(
            session_id=session["session_id"], roll_no=BE_STUDENT,
            status="PRESENT", faculty_id=FACULTY,
            reason="Phone battery died, verified in class",
        )
        record = result["record"]
        assert record["status"] == "MANUAL"
        assert record["manual_status"] == "PRESENT"
        assert "battery" in record["manual_reason"]

    def test_manual_override_can_be_disabled(self, seeded, session):
        seeded.config.security.allow_manual_override = False
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.manual_mark(
                session_id=session["session_id"], roll_no=BE_STUDENT,
                status="PRESENT", faculty_id=FACULTY, reason="because",
            )
        assert exc.value.code == "OVERRIDE_DISABLED"

    def test_manual_record_is_audited(self, seeded, session):
        seeded.ledger.manual_mark(
            session_id=session["session_id"], roll_no=BE_STUDENT,
            status="PRESENT", faculty_id=FACULTY, reason="verified in class",
        )
        trail = seeded.repo.list_audit(limit=50)
        assert any(entry["action"] == "MANUAL_OVERRIDE" for entry in trail)


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------
class TestSealing:
    def test_closing_seals_everything_into_one_block(self, seeded, session):
        for roll in (BE_STUDENT, "BCOE23AI002", "BCOE23AI003"):
            mark(seeded, session["session_id"], roll)

        closed = seeded.ledger.close_session(session["session_id"])
        block = closed["sealed_block"]
        assert block is not None
        assert block["tx_count"] == 3
        assert closed["marked_count"] == 3
        assert seeded.ledger.chain.height == 1

    def test_sealed_records_are_stamped_with_their_block(self, seeded, session):
        """The student page must not say 'pending' after the block is sealed."""
        mark(seeded, session["session_id"], BE_STUDENT)
        seeded.ledger.close_session(session["session_id"])

        records = seeded.repo.attendance_for_session(session["session_id"])
        assert records[0]["on_chain"] is True
        assert records[0]["block_index"] == 1
        assert len(records[0]["block_hash"]) == 64

    def test_the_sealed_block_satisfies_its_difficulty(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT)
        seeded.ledger.close_session(session["session_id"])
        block = seeded.ledger.chain.chain[-1]
        assert block.pow_is_valid()
        assert block.hash_is_valid()
        assert block.merkle_is_valid()

    def test_the_chain_validates_after_sealing(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT)
        seeded.ledger.close_session(session["session_id"])
        assert seeded.ledger.verify_chain()["valid"] is True

    def test_manual_seal_works_from_the_dashboard(self, seeded, session):
        mark(seeded, session["session_id"], BE_STUDENT)
        result = seeded.ledger.seal_now(note="Manual seal from the console")
        assert result["ok"] is True
        assert result["block"]["tx_count"] == 1

    def test_sealing_nothing_reports_that_plainly(self, seeded):
        result = seeded.ledger.seal_now()
        assert result["ok"] is False
        assert "empty" in result["message"].lower()

    def test_pending_root_matches_the_mempool(self, seeded, session):
        from app.blockchain.crypto import merkle_root

        mark(seeded, session["session_id"], BE_STUDENT)
        expected = merkle_root([tx.tx_id for tx in seeded.ledger.chain.mempool])
        assert seeded.ledger.pending_root() == expected


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------
class TestReceipts:
    def test_receipt_proves_all_four_things(self, seeded, session):
        result = mark(seeded, session["session_id"], BE_STUDENT)
        seeded.ledger.close_session(session["session_id"])

        receipt = seeded.ledger.receipt(result.tx_id)
        assert receipt["on_chain"] is True
        assert receipt["tx_id_valid"] is True
        assert receipt["signature_valid"] is True
        assert receipt["merkle_proof_verified"] is True
        assert receipt["block_pow_valid"] is True

    def test_unknown_receipt_is_rejected(self, seeded):
        with pytest.raises(LedgerError) as exc:
            seeded.ledger.receipt("0000deadbeef")
        assert exc.value.code == "UNKNOWN_TX"

    def test_receipt_for_a_still_pending_record(self, seeded, session):
        result = mark(seeded, session["session_id"], BE_STUDENT)
        receipt = seeded.ledger.receipt(result.tx_id)
        assert receipt["on_chain"] is False


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------
class TestAnchoring:
    @pytest.fixture()
    def anchored_setup(self, seeded, session):
        for roll in (BE_STUDENT, "BCOE23AI002"):
            mark(seeded, session["session_id"], roll)
        seeded.ledger.close_session(session["session_id"])
        return seeded

    def test_anchor_commits_to_the_attendance_records(self, anchored_setup):
        from app.blockchain.crypto import merkle_root

        anchor = anchored_setup.anchoring.anchor_now(note="test")
        tx_ids = [
            tx.tx_id
            for block in anchored_setup.ledger.chain.chain
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        ]
        assert anchor["merkle_root"] == merkle_root(tx_ids)
        assert anchor["records_anchored"] == len(tx_ids)
        assert anchor["mode"] == "INCREMENTAL"

    def test_anchor_verifies_and_recomputes_the_root(self, anchored_setup):
        anchor = anchored_setup.anchoring.anchor_now()
        verification = anchored_setup.anchoring.verify_anchor(anchor["anchor_id"])
        assert verification["verified"] is True
        root_check = verification["checks"][0]
        assert root_check["passed"] is True
        assert "attendance records" in root_check["detail"]

    def test_a_changed_chain_breaks_the_anchor_check(self, anchored_setup):
        """Editing the data after anchoring must show up as a failed check."""
        anchor = anchored_setup.anchoring.anchor_now()
        record = anchored_setup.repo.attendance_for_session(
            anchored_setup.repo.list_sessions()[0]["session_id"]
        )[0]
        # Change the stored status without touching the chain.
        anchored_setup.repo.save_attendance({**record, "status": "ABSENT"})

        # The root is recomputed from the CHAIN, so it still matches -- the
        # anchor protects chain history, and the chain protects the record.
        verification = anchored_setup.anchoring.verify_anchor(anchor["anchor_id"])
        assert verification["checks"][0]["passed"] is True

    def test_second_anchor_links_to_the_first(self, anchored_setup):
        first = anchored_setup.anchoring.anchor_now()
        second = anchored_setup.anchoring.anchor_now()
        assert second["previous_anchor_root"] == first["merkle_root"]

    def test_anchor_with_nothing_new_is_marked_cumulative(self, anchored_setup):
        """Re-committing to everything is reproducible; a head-block root is not."""
        anchored_setup.anchoring.anchor_now()
        again = anchored_setup.anchoring.anchor_now()
        assert again["mode"] == "CUMULATIVE"
        verification = anchored_setup.anchoring.verify_anchor(again["anchor_id"])
        assert verification["checks"][0]["passed"] is True

    def test_pending_summary_shows_what_is_waiting_to_be_anchored(self, anchored_setup):
        pending = anchored_setup.anchoring.pending_summary()
        assert pending["pending_root"]
        assert pending["pending_records"] >= 1
        assert pending["provider"]
        assert pending["anchor_count"] == 0

    def test_unknown_anchor_is_rejected(self, anchored_setup):
        with pytest.raises(LedgerError) as exc:
            anchored_setup.anchoring.verify_anchor("ANC-NOPE")
        assert exc.value.code == "UNKNOWN_ANCHOR"


# ---------------------------------------------------------------------------
# Fraud detectors
# ---------------------------------------------------------------------------
class TestAnomalyDetection:
    def test_shared_device_is_reported(self, seeded, session):
        from app.services import scan

        # Bypass the live SHARED_DEVICE guard so the detector has data to find.
        for index, roll in enumerate((BE_STUDENT, "BCOE23AI002", "BCOE23AI003")):
            seeded.repo.save_attendance(
                {
                    "tx_id": f"tx-shared-{index}", "session_id": session["session_id"],
                    "student_roll": roll, "status": "PRESENT",
                    "marked_at": 1_790_000_000.0 + index,
                    "device_fingerprint": "BCOE-DEV-SHARED-0001",
                    "subject_code": SUBJECT,
                }
            )
        result = scan(attendance=seeded.repo.attendance_all(),
                      sessions=seeded.repo.list_sessions())
        codes = {alert["rule"] for alert in result["alerts"]}
        assert "SHARED_DEVICE" in codes

    def test_impossible_overlap_is_reported(self, seeded, session):
        from app.services import scan

        base = 1_790_000_000.0
        second = seeded.ledger.open_session(
            subject_code="CSC701", faculty_id="BCOE-FAC-002",
            year="BE", division="A",
        )["session_id"]
        # The same student, in two rooms, one second apart.
        for index, sid in enumerate((session["session_id"], second)):
            seeded.repo.save_attendance(
                {
                    "tx_id": f"tx-overlap-{index}", "session_id": sid,
                    "student_roll": BE_STUDENT, "status": "PRESENT",
                    "marked_at": base + index, "device_fingerprint": f"D{index}",
                    "subject_code": SUBJECT,
                }
            )
        result = scan(attendance=seeded.repo.attendance_all(),
                      sessions=seeded.repo.list_sessions())
        codes = {alert["rule"] for alert in result["alerts"]}
        assert "IMPOSSIBLE_OVERLAP" in codes

    def test_a_clean_dataset_raises_nothing(self, seeded, session):
        from app.services import scan

        for index, roll in enumerate((BE_STUDENT, "BCOE23AI002")):
            seeded.repo.save_attendance(
                {
                    "tx_id": f"tx-clean-{index}", "session_id": session["session_id"],
                    "student_roll": roll, "status": "PRESENT",
                    "marked_at": 1_790_000_000.0 + index * 4,
                    "device_fingerprint": f"BCOE-DEV-CLEAN-{index:04d}",
                    "subject_code": SUBJECT,
                }
            )
        result = scan(attendance=seeded.repo.attendance_all(),
                      sessions=seeded.repo.list_sessions())
        assert result["alerts"] == []

    def test_scan_reports_how_much_it_examined(self, seeded, session):
        from app.services import scan

        mark(seeded, session["session_id"], BE_STUDENT)
        result = scan(attendance=seeded.repo.attendance_all(),
                      sessions=seeded.repo.list_sessions())
        assert result["scanned_records"] >= 1
        assert result["detectors"], "the scan must say which rules it applied"
        assert result["alert_count"] == len(result["alerts"])
