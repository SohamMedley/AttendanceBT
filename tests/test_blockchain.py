"""The blockchain: hashing, mining, linking, tampering, Merkle proofs."""

from __future__ import annotations

from app.blockchain import (
    GENESIS_PREVIOUS_HASH,
    Block,
    Blockchain,
    hash_record,
    merkle_proof,
    merkle_root,
    sha256,
    verify_merkle_proof,
)

RECORD = {
    "session_id": "CSDO7022-1",
    "roll_no": "BCOE23AI001",
    "status": "PRESENT",
    "marked_at": 1700000000.0,
}


def test_a_record_hashes_the_same_way_twice():
    assert hash_record(RECORD) == hash_record(dict(RECORD))


def test_changing_any_field_changes_the_record_hash():
    """Every field is covered, including the ones that look like decoration.

    The first version hashed only the roll number, status and timestamp, so
    changing a student's *name* in the stored file left the chain verifying
    perfectly -- which is precisely the kind of edit this project is supposed to
    catch.
    """
    edits = (
        ("roll_no", "BCOE23AI002"),
        ("status", "ABSENT"),
        ("marked_at", 1.0),
        ("name", "Someone Else"),
        ("subject_code", "CSC701"),
        ("subject_name", "Artificial Intelligence"),
    )
    for field, value in edits:
        edited = dict(RECORD, **{field: value})
        assert hash_record(edited) != hash_record(RECORD), field


def test_bookkeeping_fields_are_not_part_of_the_hash():
    """block_index is filled in after mining, so it cannot be hashed."""
    sealed = dict(RECORD, block_index=1, id="x")
    assert hash_record(sealed) == hash_record(RECORD)


def test_the_merkle_root_of_no_records_is_still_a_hash():
    assert merkle_root([]) == sha256("")


def test_one_changed_record_changes_the_root():
    records = [dict(RECORD, roll_no=f"BCOE23AI{i:03d}") for i in range(1, 9)]
    hashes = [hash_record(r) for r in records]
    before = merkle_root(hashes)

    hashes[4] = hash_record(dict(records[4], status="ABSENT"))
    assert merkle_root(hashes) != before


def test_mining_finds_a_hash_with_the_right_number_of_zeros():
    block = Block(index=1, timestamp=1.0, session_id="s", records=[RECORD])
    block.mine(3)
    assert block.hash.startswith("000")
    assert block.hash == block.calculate_hash()


def test_every_record_proves_into_the_root():
    records = [dict(RECORD, roll_no=f"BCOE23AI{i:03d}") for i in range(1, 8)]
    hashes = [hash_record(r) for r in records]
    root = merkle_root(hashes)

    for position in range(len(hashes)):
        proof = merkle_proof(hashes, position)
        assert verify_merkle_proof(hashes[position], proof, root), position


def test_a_proof_fails_for_a_record_that_is_not_in_the_block():
    records = [dict(RECORD, roll_no=f"BCOE23AI{i:03d}") for i in range(1, 5)]
    hashes = [hash_record(r) for r in records]
    outsider = hash_record(dict(RECORD, roll_no="BCOE23AI999"))

    assert not verify_merkle_proof(outsider, merkle_proof(hashes, 0), merkle_root(hashes))


def test_a_small_block_needs_only_a_short_proof():
    records = [dict(RECORD, roll_no=f"BCOE23AI{i:03d}") for i in range(1, 65)]
    proof = merkle_proof([hash_record(r) for r in records], 0)
    assert len(proof) == 6                       # log2(64): far fewer than 64 hashes


def test_a_fresh_chain_starts_with_genesis():
    chain = Blockchain(difficulty=2)
    genesis = chain.create_genesis()
    assert genesis.index == 0
    assert genesis.previous_hash == GENESIS_PREVIOUS_HASH
    assert chain.is_valid()[0]


def test_blocks_link_to_the_block_before_them():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    first = chain.add_session_block("s1", [RECORD])
    second = chain.add_session_block("s2", [dict(RECORD, roll_no="BCOE23AI002")])

    assert first.index == 1 and first.previous_hash == chain.blocks[0].hash
    assert second.previous_hash == first.hash
    assert chain.is_valid()[0]


def test_editing_a_sealed_record_is_detected():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    chain.add_session_block("s1", [RECORD])
    chain.is_valid()

    chain.blocks[1].records[0]["status"] = "ABSENT"
    ok, message = chain.is_valid()
    assert not ok
    assert "changed" in message.lower() or "merkle" in message.lower()


def test_a_renamed_student_in_a_sealed_block_is_detected():
    """The exact edit `manage.py tamper` performs, checked end to end."""
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    chain.add_session_block("s1", [dict(RECORD, name="Ansh Vaze")])
    assert chain.is_valid()[0]

    chain.blocks[1].records[0]["name"] = "Someone Else"
    ok, message = chain.is_valid()
    assert not ok
    assert "merkle" in message.lower()


def test_editing_the_merkle_root_alone_is_detected():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    block = chain.add_session_block("s1", [RECORD])

    # The realistic attack: edit the stored JSON, then mine the block again so
    # the hash and the difficulty check look perfectly normal. The records still
    # do not match the Merkle root, which is what catches it.
    block.merkle_root = "f" * 64
    block.mine(2)
    ok, message = chain.is_valid()
    assert not ok
    assert "merkle" in message.lower()


def test_a_broken_link_is_detected():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    chain.add_session_block("s1", [RECORD])
    chain.add_session_block("s2", [dict(RECORD, roll_no="BCOE23AI002")])

    chain.blocks[2].previous_hash = "a" * 64
    chain.blocks[2].mine(2)          # re-mined, so only the link is wrong
    ok, message = chain.is_valid()
    assert not ok
    assert "not linked" in message


def test_a_record_can_be_found_and_proved():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    chain.add_session_block("s1", [RECORD, dict(RECORD, roll_no="BCOE23AI002")])

    proof = chain.prove_record("BCOE23AI002")
    assert proof is not None
    assert proof["block_index"] == 1
    assert verify_merkle_proof(proof["record_hash"], proof["proof"], proof["root"])


def test_a_record_that_was_never_sealed_cannot_be_proved():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    assert chain.prove_record("BCOE23AI001") is None


def test_a_chain_survives_a_round_trip_through_json():
    chain = Blockchain(difficulty=2)
    chain.create_genesis()
    chain.add_session_block("s1", [RECORD])

    restored = Blockchain(difficulty=2)
    restored.load(chain.to_list())
    assert restored.is_valid()[0]
    assert restored.blocks[1].hash == chain.blocks[1].hash
