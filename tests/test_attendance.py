"""Sessions, the rotating QR token, marking, and sealing a block."""

from __future__ import annotations

import time

import pytest

from app.data import check_token, current_token

TTL = 30


# --------------------------------------------------------------------------
# the QR token
# --------------------------------------------------------------------------
def test_a_fresh_token_checks_out():
    token = current_token("s1", "secret", TTL)
    assert check_token("s1", token["window"], token["signature"], "secret", TTL)


def test_the_token_changes_every_window():
    first = current_token("s1", "secret", TTL, at=1000)
    later = current_token("s1", "secret", TTL, at=1000 + TTL)
    assert first["signature"] != later["signature"]


def test_a_token_from_the_last_window_is_still_accepted():
    token = current_token("s1", "secret", TTL, at=1000)
    window = token["window"]
    assert check_token("s1", window, token["signature"], "secret", TTL, at=1000 + TTL)


def test_an_old_token_is_rejected():
    token = current_token("s1", "secret", TTL, at=1000)
    with pytest.raises(ValueError, match="expired"):
        check_token("s1", token["window"], token["signature"], "secret", TTL, at=1000 + TTL * 5)


def test_a_forged_signature_is_rejected():
    token = current_token("s1", "secret", TTL, at=1000)
    with pytest.raises(ValueError, match="not issued"):
        check_token("s1", token["window"], "0" * 16, "secret", TTL, at=1000)


def test_a_token_for_another_session_is_rejected():
    token = current_token("s1", "secret", TTL, at=1000)
    with pytest.raises(ValueError, match="not issued"):
        check_token("s2", token["window"], token["signature"], "secret", TTL, at=1000)


# --------------------------------------------------------------------------
# marking
# --------------------------------------------------------------------------
@pytest.fixture
def prepared(db):
    db.add_student("BCOE23AI001", "Ansh Vaze")
    db.add_student("BCOE23AI002", "Aarya Halde")
    return db


def test_a_student_can_be_marked_once(prepared):
    session = prepared.open_session("CSDO7022")
    record = prepared.mark(session["id"], "BCOE23AI001")

    assert record["status"] == "PRESENT"
    assert record["name"] == "Ansh Vaze"
    assert len(prepared.records_for(session["id"])) == 1


def test_marking_twice_is_refused(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    with pytest.raises(ValueError, match="already marked"):
        prepared.mark(session["id"], "BCOE23AI001")


def test_an_unknown_roll_number_is_refused(prepared):
    session = prepared.open_session("CSDO7022")
    with pytest.raises(ValueError, match="not in the class register"):
        prepared.mark(session["id"], "BCOE23AI999")


def test_an_unknown_subject_is_refused(prepared):
    with pytest.raises(ValueError, match="Unknown subject"):
        prepared.open_session("NOPE101")


def test_an_expired_session_cannot_be_marked(prepared):
    session = prepared.open_session("CSDO7022", minutes=1)
    prepared.data["sessions"][session["id"]]["expires_at"] = time.time() - 1
    with pytest.raises(ValueError, match="closed"):
        prepared.mark(session["id"], "BCOE23AI001")


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------
def test_closing_a_session_seals_its_records_into_a_block(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.mark(session["id"], "BCOE23AI002")

    result = prepared.close_session(session["id"])
    block = result["block"]

    assert result["record_count"] == 2
    assert block.index == 1
    assert len(block.records) == 2
    assert prepared.chain.is_valid()[0]


def test_every_mark_knows_which_block_holds_it(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.close_session(session["id"])

    record = prepared.records_for(session["id"])[0]
    assert record["block_index"] == 1


def test_a_session_cannot_be_sealed_twice(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.close_session(session["id"])
    with pytest.raises(ValueError, match="already sealed"):
        prepared.close_session(session["id"])


def test_marks_cannot_be_added_after_sealing(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.close_session(session["id"])
    with pytest.raises(ValueError, match="closed"):
        prepared.mark(session["id"], "BCOE23AI002")


def test_sealed_records_can_be_proved(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.mark(session["id"], "BCOE23AI002")
    prepared.close_session(session["id"])

    proof = prepared.prove("BCOE23AI002")
    assert proof is not None
    assert proof["proof_valid"] is True
    assert proof["block_index"] == 1


def test_an_unsealed_mark_cannot_be_proved(prepared):
    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    assert prepared.prove("BCOE23AI001") is None


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def test_the_chain_is_still_there_after_a_restart(settings, store, prepared):
    from app.data import Database

    session = prepared.open_session("CSDO7022")
    prepared.mark(session["id"], "BCOE23AI001")
    prepared.close_session(session["id"])

    reopened = Database(settings, store)
    assert len(reopened.chain.blocks) == 2
    assert reopened.chain.is_valid()[0]
    assert reopened.prove("BCOE23AI001")["proof_valid"] is True


def test_a_student_keeps_their_attendance_percentage(prepared):
    first = prepared.open_session("CSDO7022")
    prepared.mark(first["id"], "BCOE23AI001")
    prepared.close_session(first["id"])

    second = prepared.open_session("CSC701")
    prepared.mark(second["id"], "BCOE23AI001")
    prepared.mark(second["id"], "BCOE23AI002")
    prepared.close_session(second["id"])

    assert prepared.attendance_percentage("BCOE23AI001") == 100.0
    assert prepared.attendance_percentage("BCOE23AI002") == 50.0
