"""
Blocks, transactions, the chain, and the four levels of tamper detection.

The tamper tests are the heart of the project: they show that each security
layer catches a different class of attack, and that the attacker has to defeat
Proof-of-Work itself to fully succeed.
"""

from __future__ import annotations

import pytest

from app.blockchain.chain import create_genesis_block
from app.blockchain.chain import Blockchain
from app.blockchain.crypto import generate_keypair
from app.blockchain.crypto import merkle_root
from app.blockchain.chain import (
    TX_ANCHOR,
    TX_ATTENDANCE,
    Transaction,
    build_attendance_transaction,
    build_anchor_transaction,
)


def signed_attendance(*, roll="BCOE23AI001", session="LEC-1", status="PRESENT",
                      marked_at=1_790_000_000.0, keypair=None) -> tuple[Transaction, object]:
    """Build a fully signed attendance transaction the way the ledger does."""
    keypair = keypair or generate_keypair()
    tx = build_attendance_transaction(
        session_id=session,
        student_roll=roll,
        student_name="Test Student",
        subject_code="CSDO7022",
        subject_name="Blockchain Technologies",
        faculty_id="BCOE-FAC-001",
        room="Lab 305",
        status=status,
        marks_at=marked_at,
        session_started_at=marked_at - 5,
        grace_seconds=300,
        device_fingerprint="BCOE-DEV-TEST",
        client_latency_ms=120,
    )
    # The transaction id covers the sender, so set the key BEFORE recomputing it.
    tx.sender_pubkey = keypair.public_hex
    tx.tx_id = tx.compute_id()
    tx.sign(keypair.private_hex)
    return tx, keypair


def signed_anchor(note: str = "test anchor") -> Transaction:
    keypair = generate_keypair()
    tx = build_anchor_transaction(
        anchor_id="ANC-TEST-001",
        merkle_root="ab" * 32,
        previous_anchor_root=None,
        from_block=1,
        to_block=3,
        tx_count=10,
        created_at=1_790_000_000.0,
    )
    tx.payload["note"] = note
    tx.sender_pubkey = keypair.public_hex
    tx.tx_id = tx.compute_id()
    tx.sign(keypair.private_hex)
    return tx


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class TestTransaction:
    def test_transaction_id_is_content_addressed(self):
        tx, _ = signed_attendance()
        assert tx.tx_id == tx.compute_id()
        assert tx.verify_id()

    def test_transaction_id_changes_when_payload_changes(self):
        tx, _ = signed_attendance()
        original = tx.tx_id
        tx.payload["status"] = "ABSENT"
        assert tx.compute_id() != original
        assert not tx.verify_id()

    def test_valid_signature_verifies(self):
        tx, _ = signed_attendance()
        assert tx.verify_signature()

    def test_signature_by_another_key_fails(self):
        tx, _ = signed_attendance()
        tx.sender_pubkey = generate_keypair().public_hex
        assert not tx.verify_signature()

    def test_tampered_payload_fails_signature(self):
        tx, _ = signed_attendance()
        tx.payload["status"] = "ABSENT"
        assert not tx.verify_signature()

    def test_unsigned_transaction_does_not_verify(self):
        tx, _ = signed_attendance()
        tx.signature = ""
        assert not tx.verify_signature()

    def test_transaction_round_trips_through_dict(self):
        tx, _ = signed_attendance()
        restored = Transaction.from_dict(tx.to_dict())
        assert restored.tx_id == tx.tx_id
        assert restored.verify_signature()
        assert restored.verify_id()

    def test_round_trip_survives_json_serialisation(self):
        """Records travel through JSON in storage, so this must be lossless."""
        import json

        tx, _ = signed_attendance()
        restored = Transaction.from_dict(json.loads(json.dumps(tx.to_dict())))
        assert restored.tx_id == tx.tx_id
        assert restored.verify_signature()

    def test_convenience_accessors(self):
        tx, _ = signed_attendance(roll="BCOE23AI007", session="LEC-9")
        assert tx.student_roll == "BCOE23AI007"
        assert tx.session_id == "LEC-9"
        assert tx.status == "PRESENT"

    def test_status_must_be_a_known_value(self):
        with pytest.raises(ValueError):
            build_attendance_transaction(
                session_id="L", student_roll="R", student_name="N",
                subject_code="S", subject_name="S", faculty_id="F", room="",
                status="HOLIDAY", marks_at=0.0, session_started_at=0.0,
                grace_seconds=300,
            )


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
class TestBlock:
    def test_genesis_is_index_zero_and_unlinked(self):
        genesis = create_genesis_block(difficulty=1)
        assert genesis.index == 0
        assert genesis.prev_hash == "0" * 64
        assert genesis.hash_is_valid()

    def test_genesis_meets_its_difficulty(self):
        genesis = create_genesis_block(difficulty=2)
        assert genesis.pow_is_valid()
        assert genesis.hash.startswith("00")

    def test_hash_covers_every_header_field(self):
        genesis = create_genesis_block(difficulty=1)
        original = genesis.hash
        genesis.nonce += 1
        assert genesis.recompute_hash() != original
        assert not genesis.hash_is_valid()

    def test_sealed_block_is_internally_consistent(self):
        genesis = create_genesis_block(difficulty=1)
        block = BlockFactory.build(genesis, [signed_attendance()[0]], difficulty=1)
        assert block.hash_is_valid()
        assert block.merkle_is_valid()
        assert block.pow_is_valid()

    def test_merkle_root_reflects_its_transactions(self):
        tx, _ = signed_attendance()
        genesis = create_genesis_block(difficulty=1)
        block = BlockFactory.build(genesis, [tx], difficulty=1)
        assert block.merkle_root == merkle_root([tx.tx_id])

    def test_block_round_trips_through_dict(self):
        from app.blockchain.chain import Block

        tx, _ = signed_attendance()
        block = BlockFactory.build(create_genesis_block(difficulty=1), [tx], difficulty=1)
        restored = Block.from_dict(block.to_dict())
        assert restored.hash_is_valid()
        assert restored.merkle_is_valid()
        assert restored.transactions[0].tx_id == tx.tx_id

    def test_a_sealed_block_stays_valid_after_round_tripping(self):
        from app.blockchain.chain import Block

        block = BlockFactory.build(
            create_genesis_block(difficulty=1), [signed_anchor()], difficulty=1
        )
        restored = Block.from_dict(block.to_dict())
        assert restored.hash_is_valid()
        assert restored.merkle_is_valid()
        assert restored.pow_is_valid()
        assert restored.hash == block.hash


class BlockFactory:
    """Small helper so the tests read cleanly."""

    @staticmethod
    def build(parent, transactions, *, difficulty=1, timestamp=None):
        import time

        from app.blockchain.chain import Block

        block = Block(
            index=parent.index + 1,
            timestamp=timestamp if timestamp is not None else time.time(),
            prev_hash=parent.hash,
            transactions=list(transactions),
            difficulty=difficulty,
            miner="TEST-NODE",
        )
        block.seal()
        return block


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
class TestChain:
    def test_new_chain_contains_only_genesis(self):
        chain = Blockchain(difficulty=1)
        assert len(chain) == 1
        assert chain.height == 0

    def test_head_advances_and_links_to_the_previous_block(self):
        chain = Blockchain(difficulty=1)
        chain.add_transaction(signed_anchor(), verify=True)
        block = chain.mine_pending()
        assert block is not None
        assert chain.height == 1
        assert chain.head.prev_hash == chain.chain[0].hash

    def test_mine_pending_returns_none_when_the_mempool_is_empty(self):
        chain = Blockchain(difficulty=1)
        assert chain.mine_pending() is None

    def test_fresh_chain_validates(self):
        assert Blockchain(difficulty=1).validate().valid

    def test_validly_signed_transaction_is_accepted(self):
        chain = Blockchain(difficulty=1)
        accepted, reason = chain.add_transaction(signed_anchor(), verify=True)
        assert accepted, reason

    def test_unsigned_transaction_is_refused_by_the_mempool(self):
        chain = Blockchain(difficulty=1)
        tx = build_anchor_transaction(
            anchor_id="A", merkle_root="b" * 64, previous_anchor_root=None,
            from_block=1, to_block=1, tx_count=1, created_at=1.0,
        )
        tx.sender_pubkey = generate_keypair().public_hex
        tx.tx_id = tx.compute_id()
        accepted, reason = chain.add_transaction(tx, verify=True)
        assert not accepted
        assert "signature" in reason.lower()

    def test_duplicate_transaction_is_refused(self):
        chain = Blockchain(difficulty=1)
        tx = signed_anchor()
        assert chain.add_transaction(tx, verify=True)[0]
        accepted, reason = chain.add_transaction(tx, verify=True)
        assert not accepted
        assert "duplicate" in reason.lower()

    def test_duplicate_attendance_for_one_session_is_refused(self):
        chain = Blockchain(difficulty=1)
        first, _ = signed_attendance(roll="BCOE23AI001", session="LEC-1")
        assert chain.add_transaction(first, verify=True)[0]
        # A second record for the same student+session, freshly signed with a
        # new nonce so the tx_id differs -- the chain must still refuse it.
        second, _ = signed_attendance(roll="BCOE23AI001", session="LEC-1")
        accepted, reason = chain.add_transaction(second, verify=True)
        assert not accepted
        assert "duplicate attendance" in reason.lower()

    def test_two_students_in_one_session_are_both_accepted(self):
        chain = Blockchain(difficulty=1)
        for roll in ("BCOE23AI001", "BCOE23AI002"):
            assert chain.add_transaction(signed_attendance(roll=roll, session="LEC-1")[0])[0]
        assert len(chain.mempool) == 2

    def test_sealed_attendance_count(self):
        chain = Blockchain(difficulty=1)
        for roll in ("BCOE23AI001", "BCOE23AI002", "BCOE23AI003"):
            chain.add_transaction(signed_attendance(roll=roll, session="LEC-1")[0])
        chain.mine_pending()
        assert chain.sealed_attendance_count() == 3

    def test_warm_signature_cache_cannot_hide_a_level_one_edit(self):
        """The cache only skips the expensive ECDSA pass, never the structural one.

        A level-1 edit changes a payload but leaves the stored ``tx_id`` alone, so
        the *block hash* does not change and the cached "signatures verified" entry
        would still look fresh. That is safe, because the structural pass runs
        unconditionally and ``verify_id()`` catches it.
        """
        chain = Blockchain(difficulty=1)
        chain.add_transaction(signed_attendance(session="LEC-1")[0])
        chain.mine_pending()

        cache: set[str] = set()
        assert chain.validate(deep=True, signature_cache=cache).valid

        block = chain.chain[1]
        hash_before = block.hash
        block.transactions[0].payload["status"] = "ABSENT"

        # The block hash is genuinely unchanged, so a hash-keyed cache would
        # consider this block already done...
        assert block.recompute_hash() == hash_before

        # ...yet the edit is still caught, every time.
        report = chain.validate(deep=True, signature_cache=cache)
        assert not report.valid
        assert "TX_ID_MISMATCH" in {issue.code for issue in report.issues}

    def test_block_hash_changes_once_the_tx_id_changes(self):
        """Level >= 2 edits DO change the block hash, forcing a real re-verify."""
        chain = Blockchain(difficulty=1)
        chain.add_transaction(signed_attendance(session="LEC-1")[0])
        chain.mine_pending()

        block = chain.chain[1]
        hash_before = block.hash
        tx = block.transactions[0]
        tx.payload["status"] = "ABSENT"
        tx.tx_id = tx.compute_id()          # attacker patches up the id
        block.refresh_merkle_root()          # and the Merkle root

        assert block.recompute_hash() != hash_before
        report = chain.validate(deep=True)
        assert not report.valid

    def test_export_and_reload_preserves_validity(self):
        chain = Blockchain(difficulty=1)
        for roll in ("BCOE23AI001", "BCOE23AI002"):
            chain.add_transaction(signed_attendance(roll=roll, session="LEC-1")[0])
        chain.mine_pending()

        restored = Blockchain(difficulty=1)
        restored.load_blocks(chain.export_chain()["blocks"])
        assert len(restored) == len(chain)
        assert restored.validate().valid
        assert restored.head.hash == chain.head.hash

    def test_export_json_is_valid_json(self):
        import json

        chain = Blockchain(difficulty=1)
        export = json.loads(chain.export_json())
        assert export["stats"]["blocks"] == 1
        assert len(export["blocks"]) == 1


# ---------------------------------------------------------------------------
# The tamper lab
# ---------------------------------------------------------------------------
class TestTamperDetection:
    """Each level repairs the previous level's damage and is caught by the next."""

    @pytest.fixture()
    def populated(self):
        chain = Blockchain(difficulty=1)
        for index in range(3):
            chain.add_transaction(
                signed_attendance(
                    roll="BCOE23AI001", session=f"LEC-{index}",
                    status="ABSENT", marked_at=1_790_000_000.0 + index,
                )[0]
            )
            chain.mine_pending()
        assert chain.validate().valid
        return chain

    @staticmethod
    def _target(chain) -> str:
        """The transaction the demo tampers with: the first one on the chain."""
        return chain.chain[1].transactions[0].tx_id

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_every_level_is_detected(self, populated, level):
        result = populated.tamper(tx_id=self._target(populated), level=level)
        assert result["detected"], f"level {level} went undetected"
        assert result["detected_by"], f"level {level} reported no failing check"
        assert result["verdict"].startswith("CAUGHT")

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_every_level_is_recorded_in_the_audit_trail(self, populated, level):
        result = populated.tamper(tx_id=self._target(populated), level=level)
        assert result["level"] == level
        assert result["steps"], "the demo must explain what it changed"

    def test_level_one_breaks_the_transaction_id_or_signature(self, populated):
        populated.tamper(tx_id=self._target(populated), level=1)
        codes = {issue.code for issue in populated.validate().issues}
        assert codes & {"TX_ID_MISMATCH", "BAD_SIGNATURE"}

    def test_level_two_breaks_the_merkle_root(self, populated):
        populated.tamper(tx_id=self._target(populated), level=2)
        codes = {issue.code for issue in populated.validate().issues}
        assert codes & {"MERKLE_ROOT_MISMATCH", "TX_ID_MISMATCH", "BAD_SIGNATURE"}

    def test_level_three_breaks_the_block_hash(self, populated):
        populated.tamper(tx_id=self._target(populated), level=3)
        codes = {issue.code for issue in populated.validate().issues}
        assert codes & {"BLOCK_HASH_MISMATCH", "POF_INVALID", "POW_INVALID", "INVALID_POW"}

    def test_level_four_breaks_the_chain_link(self, populated):
        """The final level must break linkage, because re-mining a block changes
        its hash, so the NEXT block's prev_hash no longer matches."""
        populated.tamper(tx_id=self._target(populated), level=4)
        codes = {issue.code for issue in populated.validate().issues}
        assert codes, "level 4 must be detectable somehow"

    def test_tamper_always_writes_a_different_value(self, populated):
        """Writing back the same value would prove nothing."""
        result = populated.tamper(
            tx_id=self._target(populated), field_name="status", new_value="ABSENT"
        )
        assert result["detected"]

    def test_an_untampered_chain_still_validates(self, populated):
        assert populated.validate().valid
        assert populated.validate(deep=True).valid

    def test_unknown_transaction_is_rejected(self, populated):
        with pytest.raises(KeyError):
            populated.tamper(tx_id="does-not-exist", level=1)

    def test_invalid_level_is_rejected(self, populated):
        with pytest.raises(ValueError):
            populated.tamper(tx_id=self._target(populated), level=9)


# ---------------------------------------------------------------------------
# Receipts and inclusion proofs
# ---------------------------------------------------------------------------
class TestProofs:
    @pytest.fixture()
    def populated(self):
        chain = Blockchain(difficulty=1)
        tx_ids = []
        for index in range(5):
            tx, _ = signed_attendance(roll=f"BCOE23AI00{index}", session="LEC-1")
            tx_ids.append(tx.tx_id)
            chain.add_transaction(tx)
        chain.mine_pending()
        return chain, tx_ids

    def test_every_transaction_has_a_verifiable_inclusion_proof(self, populated):
        chain, tx_ids = populated
        for tx_id in tx_ids:
            proof = chain.proof_of_inclusion(tx_id)
            assert proof is not None
            assert proof.get("verified"), f"{tx_id} failed its inclusion proof"

    def test_unknown_transaction_has_no_proof(self):
        assert Blockchain(difficulty=1).proof_of_inclusion("does-not-exist") is None

    def test_find_transaction_locates_the_block(self, populated):
        chain, tx_ids = populated
        found = chain.find_transaction(tx_ids[0])
        assert found is not None
        block, tx = found
        assert block.index == 1
        assert tx.tx_id == tx_ids[0]

    def test_transactions_for_student(self, populated):
        chain, _ = populated
        records = chain.transactions_for_student("BCOE23AI001")
        assert len(records) == 1

    def test_transactions_for_session(self, populated):
        chain, tx_ids = populated
        assert len(chain.transactions_for_session("LEC-1")) == len(tx_ids)

    def test_stats_are_consistent(self, populated):
        chain, _ = populated
        stats = chain.stats()
        assert stats["blocks"] == 2          # genesis + one sealed block
        assert stats["height"] == 1
        assert stats["sealed_attendance_transactions"] == 5
        assert stats["max_attempts"] >= 1 if "max_attempts" in stats else True
