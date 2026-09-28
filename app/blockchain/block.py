"""
Block model.

A block is a *header* plus a list of transactions::

    +--------------------------------------------------------------+
    |  index | timestamp | prev_hash | merkle_root | difficulty |   |
    |  miner | nonce                                        |   |
    +--------------------------------------------------------------+
    |  [ tx_id_1, tx_id_2, tx_id_3, ... ]  (transactions)          |
    +--------------------------------------------------------------+

``prev_hash`` is what turns a pile of blocks into a *chain*: each block commits
to the entire history before it. Alter block N and its hash changes, so block
N+1's ``prev_hash`` no longer matches, and so on to the tip. To rewrite history
an attacker would have to re-mine every subsequent block faster than the honest
network -- the security guarantee behind Bitcoin.

``merkle_root`` commits to every transaction in the block without embedding them
in the header, which is what keeps headers small and light clients cheap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import proof_of_work as pow_module
from .merkle import merkle_root as compute_merkle_root
from .transaction import Transaction

CHAIN_VERSION = 1
GENESIS_MESSAGE = (
    "BCOE-ATTENDANCE-CHAIN Genesis | Bharat College of Engineering, Kanhor, "
    "Badlapur (W), Maharashtra | University of Mumbai | Dept. of CSE (AI & ML) | "
    "Blockchain Technologies (CSDO7022) Major Project"
)


@dataclass
class Block:
    """One block of the attendance chain."""

    index: int
    timestamp: float
    prev_hash: str
    transactions: list[Transaction] = field(default_factory=list)
    difficulty: int = pow_module.DEFAULT_DIFFICULTY
    nonce: int = 0
    miner: str = "BCOE-NODE-01"
    version: int = CHAIN_VERSION
    note: str = ""
    hash: str = ""
    merkle_root: str = ""
    mining_stats: dict[str, Any] | None = None

    # -- hashing -----------------------------------------------------------
    def header_fields(self) -> dict[str, Any]:
        """Everything the block hash commits to."""
        return {
            "version": self.version,
            "index": self.index,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root,
            "difficulty": self.difficulty,
            "miner": self.miner,
            "nonce": self.nonce,
        }

    def compute_merkle_root(self) -> str:
        return compute_merkle_root([tx.tx_id for tx in self.transactions])

    def compute_hash(self, nonce: int | None = None) -> str:
        fields = self.header_fields()
        if nonce is not None:
            fields["nonce"] = nonce
        return _sha256(pow_module._header_string(fields))

    def refresh_merkle_root(self) -> str:
        self.merkle_root = self.compute_merkle_root()
        return self.merkle_root

    # -- mining ------------------------------------------------------------
    def seal(self, difficulty: int | None = None) -> dict[str, Any]:
        """Run Proof-of-Work and freeze the block's hash."""
        if difficulty is not None:
            self.difficulty = difficulty
        self.refresh_merkle_root()
        result = pow_module.mine(self.header_fields(), self.difficulty)
        self.nonce = result.nonce
        self.hash = result.block_hash
        self.mining_stats = result.to_dict()
        return self.mining_stats

    # -- integrity ---------------------------------------------------------
    def recompute_hash(self) -> str:
        """Hash the block *as currently stored* -- the tamper-detection check."""
        return self.compute_hash()

    def hash_is_valid(self) -> bool:
        return self.hash == self.recompute_hash()

    def merkle_is_valid(self) -> bool:
        return self.merkle_root == self.compute_merkle_root()

    def pow_is_valid(self) -> bool:
        return pow_module.meets_difficulty(self.hash, self.difficulty)

    # -- serialisation -----------------------------------------------------
    def to_dict(self, include_transactions: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "version": self.version,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root,
            "difficulty": self.difficulty,
            "nonce": self.nonce,
            "miner": self.miner,
            "hash": self.hash,
            "tx_count": len(self.transactions),
            "note": self.note,
            "mining_stats": self.mining_stats,
        }
        if include_transactions:
            data["transactions"] = [tx.to_dict() for tx in self.transactions]
            data["tx_ids"] = [tx.tx_id for tx in self.transactions]
        return data

    @property
    def size_bytes(self) -> int:
        import json

        return len(json.dumps(self.to_dict(), default=str).encode("utf-8"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Block":
        transactions = [
            Transaction.from_dict(tx) for tx in data.get("transactions", [])
        ]
        block = cls(
            index=int(data["index"]),
            timestamp=float(data["timestamp"]),
            prev_hash=data["prev_hash"],
            transactions=transactions,
            difficulty=int(data.get("difficulty", pow_module.DEFAULT_DIFFICULTY)),
            nonce=int(data.get("nonce", 0)),
            miner=data.get("miner", "BCOE-NODE-01"),
            version=int(data.get("version", CHAIN_VERSION)),
            note=data.get("note", ""),
            hash=data.get("hash", ""),
        )
        block.merkle_root = data.get("merkle_root", block.compute_merkle_root())
        block.mining_stats = data.get("mining_stats")
        return block


def create_genesis_block(difficulty: int = pow_module.DEFAULT_DIFFICULTY) -> Block:
    """Build the chain's first block.

    The genesis block is hard-coded by convention: it has no predecessor, so it
    is the root of trust every node starts from. Bitcoin's genesis block carries
    the famous headline "The Times 03/Jan/2009 Chancellor on brink of second
    bailout for banks"; ours records the institute and the project it belongs to.
    """
    block = Block(
        index=0,
        timestamp=1735689600.0,  # 2025-01-01 00:00:00 UTC -- fixed, deterministic
        prev_hash="0" * 64,
        transactions=[],
        difficulty=max(1, min(difficulty, 2)),  # keep genesis cheap
        miner="BCOE-GENESIS",
        note=GENESIS_MESSAGE,
    )
    block.seal()
    return block


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["Block", "create_genesis_block", "GENESIS_MESSAGE", "CHAIN_VERSION"]
