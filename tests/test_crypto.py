"""
Cryptographic primitives, checked against published test vectors.

These are the tests that matter most: if the cryptography is wrong, nothing built
on top of it can be right.
"""

from __future__ import annotations

import pytest

from app.blockchain import ecdsa, keccak
from app.blockchain.merkle import (
    merkle_levels,
    merkle_proof,
    merkle_root,
    verify_merkle_proof,
)
from app.blockchain.proof_of_work import (
    meets_difficulty,
    mine,
    next_difficulty,
    target_from_difficulty,
)


# ---------------------------------------------------------------------------
# secp256k1 / ECDSA
# ---------------------------------------------------------------------------
class TestECDSA:
    def test_public_key_for_private_key_one(self):
        """The canonical secp256k1 vector: private key 1 -> generator point G."""
        keypair = ecdsa.keypair_from_private(1)
        assert keypair.public_hex == (
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        )

    def test_address_for_private_key_one(self):
        """Known Bitcoin address for private key 1 (validates RIPEMD160 + Base58Check)."""
        assert ecdsa.keypair_from_private(1).address == "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"

    def test_generator_is_on_curve(self):
        assert ecdsa.is_on_curve(ecdsa.G)

    def test_curve_order_times_generator_is_infinity(self):
        """n * G must be the point at infinity -- the defining property of the group."""
        assert ecdsa.scalar_mult(ecdsa.N, ecdsa.G) is None

    def test_point_addition_is_commutative(self):
        a = ecdsa.scalar_mult(7, ecdsa.G)
        b = ecdsa.scalar_mult(11, ecdsa.G)
        assert ecdsa.point_add(a, b) == ecdsa.point_add(b, a)

    def test_scalar_multiplication_matches_repeated_addition(self):
        expected = None
        for _ in range(13):
            expected = ecdsa.point_add(expected, ecdsa.G)
        assert ecdsa.scalar_mult(13, ecdsa.G) == expected

    def test_sign_and_verify_round_trip(self):
        keypair = ecdsa.generate_keypair()
        message = b"attendance|CSDO7022|2026-09-29"
        signature = ecdsa.sign(keypair.private_hex, message)
        assert ecdsa.verify(keypair.public_hex, message, signature)

    def test_modified_message_is_rejected(self):
        keypair = ecdsa.generate_keypair()
        signature = ecdsa.sign(keypair.private_hex, b"present")
        assert not ecdsa.verify(keypair.public_hex, b"absent", signature)

    def test_wrong_public_key_is_rejected(self):
        signer = ecdsa.generate_keypair()
        impostor = ecdsa.generate_keypair()
        signature = ecdsa.sign(signer.private_hex, b"payload")
        assert not ecdsa.verify(impostor.public_hex, b"payload", signature)

    def test_signature_is_deterministic_rfc6979(self):
        """Same key + same message must give the same signature (RFC 6979)."""
        keypair = ecdsa.keypair_from_private(0xDEADBEEF)
        first = ecdsa.sign(keypair.private_hex, b"same message")
        second = ecdsa.sign(keypair.private_hex, b"same message")
        assert first == second

    def test_signature_is_low_s(self):
        """Canonical (low-S) signatures prevent malleability."""
        keypair = ecdsa.generate_keypair()
        signature = ecdsa.sign(keypair.private_hex, b"malleability check")
        s = int(signature[64:], 16)
        assert s <= ecdsa.N // 2

    def test_malformed_signatures_are_rejected(self):
        keypair = ecdsa.generate_keypair()
        assert not ecdsa.verify(keypair.public_hex, b"x", "not-hex")
        assert not ecdsa.verify(keypair.public_hex, b"x", "ab" * 64)

    def test_compressed_public_key_round_trip(self):
        keypair = ecdsa.generate_keypair()
        parsed = ecdsa.parse_public_key(keypair.public_hex)
        assert parsed == keypair.public_key

    def test_address_checksum_detects_typos(self):
        keypair = ecdsa.generate_keypair()
        address = keypair.address
        assert ecdsa.address_is_valid(address)
        # Flip one character to a different base58 character.
        broken = ("2" if address[5] != "2" else "3") + address[1:]
        assert not ecdsa.address_is_valid(broken)


# ---------------------------------------------------------------------------
# Keccak-256 (Ethereum's hash -- NOT NIST SHA3)
# ---------------------------------------------------------------------------
class TestKeccak:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
            (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
            (
                b"The quick brown fox jumps over the lazy dog",
                "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15",
            ),
            (b"testing", "5f16f4c7f149ac4f9510d9cf8cf384038ad348b3bcdc01915f95de12df9d1b02"),
        ],
    )
    def test_published_vectors(self, data, expected):
        assert keccak.keccak256_hex(data) == expected

    def test_differs_from_nist_sha3(self):
        """A common bug: using hashlib.sha3_256 where Ethereum needs Keccak-256."""
        import hashlib

        assert keccak.keccak256(b"") != hashlib.sha3_256(b"").digest()

    def test_erc20_selector(self):
        """The selector everyone can check by hand."""
        assert keccak.function_selector("transfer(address,uint256)") == "a9059cbb"

    def test_anchor_selector_is_four_bytes(self):
        assert len(keccak.function_selector("anchor(bytes32)")) == 8

    def test_bytes32_encoding_pads_to_32_bytes(self):
        encoded = keccak.encode_bytes32("ab" * 32)
        assert len(encoded) == 64


# ---------------------------------------------------------------------------
# Merkle trees
# ---------------------------------------------------------------------------
class TestMerkle:
    def test_empty_tree_root_is_zero_hash(self):
        assert merkle_root([]) == "0" * 64

    def test_single_leaf_root(self):
        assert len(merkle_root(["tx-1"])) == 64

    def test_root_changes_if_any_leaf_changes(self):
        leaves = [f"tx-{index}" for index in range(8)]
        modified = list(leaves)
        modified[5] = "tx-tampered"
        assert merkle_root(leaves) != merkle_root(modified)

    def test_root_is_order_sensitive(self):
        assert merkle_root(["a", "b"]) != merkle_root(["b", "a"])

    def test_odd_leaf_count_still_produces_a_root(self):
        assert merkle_root(["a", "b", "c", "d", "e"])

    @pytest.mark.parametrize("count", [1, 2, 3, 5, 7, 8, 13])
    def test_every_leaf_has_a_valid_proof(self, count):
        leaves = [f"tx-{index}" for index in range(count)]
        root = merkle_root(leaves)
        for index, leaf in enumerate(leaves):
            proof = merkle_proof(leaves, index)
            assert verify_merkle_proof(leaf, proof, root), f"leaf {index} failed"

    def test_proof_fails_for_a_different_leaf(self):
        leaves = ["a", "b", "c", "d"]
        proof = merkle_proof(leaves, 0)
        assert not verify_merkle_proof("not-in-the-tree", proof, merkle_root(leaves))

    def test_proof_fails_against_a_different_root(self):
        leaves = ["a", "b", "c", "d"]
        proof = merkle_proof(leaves, 1)
        assert not verify_merkle_proof("b", proof, merkle_root(["x", "y"]))

    def test_proof_length_is_logarithmic(self):
        leaves = [f"tx-{index}" for index in range(64)]
        assert len(merkle_proof(leaves, 0)) == 6

    def test_levels_reduce_to_one_root(self):
        levels = merkle_levels([f"tx-{index}" for index in range(6)])
        assert len(levels[-1]) == 1


# ---------------------------------------------------------------------------
# Proof-of-Work
# ---------------------------------------------------------------------------
class TestProofOfWork:
    def test_mined_hash_meets_difficulty(self):
        result = mine(
            {"index": 1, "timestamp": 0.0, "prev_hash": "0" * 64,
             "merkle_root": "0" * 64, "difficulty": 2, "miner": "test"},
            difficulty=2,
        )
        assert result.block_hash.startswith("00")
        assert meets_difficulty(result.block_hash, 2)

    def test_higher_difficulty_rejects_lower_proof(self):
        result = mine({"index": 1, "timestamp": 0.0, "miner": "test"}, difficulty=2)
        assert meets_difficulty(result.block_hash, 2)
        assert not meets_difficulty(result.block_hash, 6)

    def test_difficulty_zero_accepts_anything(self):
        assert meets_difficulty("ff" * 32, 0)

    def test_target_shrinks_with_difficulty(self):
        assert target_from_difficulty(5) < target_from_difficulty(3)

    def test_mining_is_deterministic_for_a_given_header(self):
        header = {"index": 3, "timestamp": 100.0, "prev_hash": "a" * 64,
                  "merkle_root": "b" * 64, "difficulty": 2, "miner": "n"}
        assert mine(dict(header), difficulty=2).nonce == mine(dict(header), difficulty=2).nonce

    def test_difficulty_retargets_when_blocks_are_too_fast(self):
        now = 1000.0
        fast = [{"timestamp": now + index * 0.5} for index in range(6)]
        assert next_difficulty(fast, 3) == 4

    def test_difficulty_retargets_when_blocks_are_too_slow(self):
        now = 1000.0
        slow = [{"timestamp": now + index * 500} for index in range(6)]
        assert next_difficulty(slow, 3) == 2
