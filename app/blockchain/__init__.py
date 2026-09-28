"""
The attendance blockchain.

Two modules, in dependency order:

``crypto``
    Keccak-256, secp256k1/ECDSA and Merkle trees -- everything that is pure
    mathematics and depends on nothing else in the project.

``chain``
    Transactions, blocks, Proof of Work and the chain they form.

Both are re-exported here so the rest of the application can simply write
``from ..blockchain import ecdsa``-style imports without caring how the
package is arranged internally.
"""

from __future__ import annotations

from . import chain, crypto
from .chain import (
    DEFAULT_DIFFICULTY,
    MAX_MEMPOOL,
    TX_ANCHOR,
    TX_ATTENDANCE,
    Block,
    Blockchain,
    Transaction,
    ValidationIssue,
    ValidationReport,
    build_anchor_transaction,
    build_attendance_transaction,
    create_genesis_block,
    estimate_attempts,
    meets_difficulty,
    mine,
    next_difficulty,
    target_from_difficulty,
)
from .crypto import (
    ECDSAError,
    KeyPair,
    address_is_valid,
    base58_decode,
    base58_encode,
    function_selector,
    generate_keypair,
    hash160,
    hash_leaf,
    hash_pair,
    is_on_curve,
    keccak256,
    keccak256_hex,
    keypair_from_private,
    merkle_levels,
    merkle_proof,
    merkle_root,
    parse_public_key,
    point_add,
    scalar_mult,
    sign_message,
    verify_merkle_proof,
    verify_signature,
)

#: ``ecdsa``, ``keccak`` and ``merkle`` used to be separate modules. They are
#: now sections of ``crypto``, but these aliases keep the readable
#: ``ecdsa.sign_message(...)`` spelling available for anyone who prefers it.
ecdsa = crypto
keccak = crypto
merkle = crypto

__all__ = [
    "Block",
    "Blockchain",
    "Transaction",
    "create_genesis_block",
    "crypto",
    "chain",
    "ecdsa",
    "keccak",
    "merkle",
    "merkle_root",
    "merkle_proof",
    "verify_merkle_proof",
    "sign_message",
    "verify_signature",
    "generate_keypair",
    "keypair_from_private",
    "keccak256",
    "keccak256_hex",
    "function_selector",
    "mine",
    "meets_difficulty",
    "estimate_attempts",
    "next_difficulty",
]
