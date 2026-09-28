"""The blockchain itself.

This is the part the mini project is really about, so it is deliberately small
and readable -- about 200 lines with no dependencies, only hashlib.

A block holds the attendance records of ONE lecture session:

    index          position in the chain (genesis is 0)
    timestamp      when the block was mined
    session_id     which lecture these records belong to
    records        the attendance records, in order
    merkle_root    one hash that stands for all of those records
    previous_hash  the hash of the block before this one
    nonce          the number found by mining
    hash           sha256 of everything above

Two properties make this useful for attendance:

* Changing any record changes the Merkle root, which changes the block hash,
  which breaks the link to the next block. So an edit is always detectable.
* The Merkle root lets one record be proved to be part of a block by showing
  about log2(n) hashes instead of the whole block.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

#: The hash of the "previous block" of the first block. There is no block 0.
GENESIS_PREVIOUS_HASH = "0" * 64


def sha256(value: str) -> str:
    """Hex SHA-256 of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


#: Fields that are bookkeeping rather than attendance, and so are not hashed.
#: `block_index` is filled in after the block is mined, and `id` is derived from
#: the session and the roll number.
UNHASHED_FIELDS = ("id", "block_index")


def hash_record(record: dict[str, Any]) -> str:
    """Hash one attendance record.

    EVERY other field is covered, not just a chosen few, and they are hashed as
    sorted JSON so the order they happen to sit in cannot change the hash.

    This matters more than it looks. An earlier version hashed only the roll
    number, status and timestamp -- so editing a student's *name* in the stored
    file left the whole chain verifying perfectly. The tamper command caught it,
    which is exactly why tamper detection should be tested with a real edit and
    not a made-up one.
    """
    fields = {k: v for k, v in record.items() if k not in UNHASHED_FIELDS}
    return sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str))


def merkle_root(hashes: list[str]) -> str:
    """Combine record hashes into a single root.

    Pairs are hashed together level by level. When a level has an odd number of
    hashes the last one is paired with itself, which is the usual convention.
    """
    if not hashes:
        return sha256("")
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(hashes: list[str], index: int) -> list[dict[str, str]]:
    """The hashes needed to prove that ``hashes[index]`` is in the tree.

    Returns a list of steps; each step says which side the sibling sits on.
    """
    if not hashes:
        raise ValueError("empty tree")
    if not 0 <= index < len(hashes):
        raise ValueError("index outside the tree")

    level = list(hashes)
    position = index
    proof: list[dict[str, str]] = []

    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = position - 1 if position % 2 else position + 1
        proof.append(
            {"position": "left" if position % 2 else "right", "hash": level[sibling]}
        )
        level = [sha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
        position //= 2

    return proof


def verify_merkle_proof(record_hash: str, proof: Iterable[dict[str, str]], root: str) -> bool:
    """Recompute the root from a record hash and its proof."""
    current = record_hash
    for step in proof:
        sibling = step["hash"]
        current = (
            sha256(sibling + current)
            if step["position"] == "left"
            else sha256(current + sibling)
        )
    return current == root


@dataclass
class Block:
    """One sealed lecture."""

    index: int
    timestamp: float
    session_id: str
    records: list[dict[str, Any]]
    previous_hash: str = GENESIS_PREVIOUS_HASH
    nonce: int = 0
    merkle_root: str = ""
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.merkle_root:
            self.merkle_root = merkle_root([hash_record(r) for r in self.records])

    # ------------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        """Exactly the fields the block hash covers."""
        return {
            "index": self.index,
            "timestamp": round(self.timestamp, 6),
            "session_id": self.session_id,
            "merkle_root": self.merkle_root,
            "record_count": len(self.records),
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
        }

    def calculate_hash(self) -> str:
        return sha256(json.dumps(self.payload(), sort_keys=True, separators=(",", ":")))

    def mine(self, difficulty: int) -> "Block":
        """Find a nonce so the hash starts with ``difficulty`` zeros."""
        target = "0" * max(0, difficulty)
        self.nonce = 0
        while True:
            self.hash = self.calculate_hash()
            if self.hash.startswith(target):
                return self
            self.nonce += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "records": self.records,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
            "merkle_root": self.merkle_root,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        block = cls(
            index=int(data["index"]),
            timestamp=float(data["timestamp"]),
            session_id=str(data.get("session_id", "")),
            records=list(data.get("records", [])),
            previous_hash=str(data.get("previous_hash", GENESIS_PREVIOUS_HASH)),
            nonce=int(data.get("nonce", 0)),
            merkle_root=str(data.get("merkle_root", "")),
            hash=str(data.get("hash", "")),
        )
        return block


class Blockchain:
    """An ordered list of blocks that validates itself."""

    def __init__(self, difficulty: int = 4) -> None:
        self.difficulty = difficulty
        self.blocks: list[Block] = []

    # ------------------------------------------------------------------
    def create_genesis(self, when: float | None = None) -> Block:
        """The first block, which anchors the chain."""
        self.blocks = []
        block = Block(
            index=0,
            timestamp=when if when is not None else time.time(),
            session_id="genesis",
            records=[],
            previous_hash=GENESIS_PREVIOUS_HASH,
        ).mine(self.difficulty)
        self.blocks.append(block)
        return block

    def last(self) -> Block:
        if not self.blocks:
            self.create_genesis()
        return self.blocks[-1]

    def add_session_block(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        when: float | None = None,
    ) -> Block:
        """Seal one lecture's records into a new block."""
        block = Block(
            index=len(self.blocks),
            timestamp=when if when is not None else time.time(),
            session_id=session_id,
            records=[dict(r) for r in records],
            previous_hash=self.last().hash,
        ).mine(self.difficulty)
        self.blocks.append(block)
        return block

    # ------------------------------------------------------------------
    def is_valid(self) -> tuple[bool, str]:
        """Re-check every block. Returns (ok, message)."""
        if not self.blocks:
            return False, "The chain is empty."

        target = "0" * max(0, self.difficulty)
        previous = self.blocks[0]

        for position, block in enumerate(self.blocks):
            if block.index != position:
                return False, f"Block {position} has index {block.index}."
            if block.calculate_hash() != block.hash:
                return False, (
                    f"Block {block.index} has been changed: its contents no longer "
                    f"match its hash."
                )
            if not block.hash.startswith(target):
                return False, f"Block {block.index} does not meet the difficulty."
            if block.index == 0:
                if block.previous_hash != GENESIS_PREVIOUS_HASH:
                    return False, "The genesis block has the wrong previous hash."
            elif block.previous_hash != previous.hash:
                return False, f"Block {block.index} is not linked to block {previous.index}."
            expected_root = merkle_root([hash_record(r) for r in block.records])
            if expected_root != block.merkle_root:
                return False, f"The records in block {block.index} do not match its Merkle root."
            previous = block

        return True, f"The chain is intact: {len(self.blocks)} blocks verified."

    def find_record(self, roll_no: str, session_id: str | None = None) -> tuple[Block, int] | None:
        """Find a sealed record, with the block it lives in."""
        for block in reversed(self.blocks):
            for position, record in enumerate(block.records):
                if record.get("roll_no") != roll_no:
                    continue
                if session_id and record.get("session_id") != session_id:
                    continue
                return block, position
        return None

    def prove_record(self, roll_no: str, session_id: str | None = None) -> dict[str, Any] | None:
        """Produce a Merkle proof that a record is inside the chain."""
        found = self.find_record(roll_no, session_id)
        if found is None:
            return None
        block, position = found
        record = block.records[position]
        proof = merkle_proof([hash_record(r) for r in block.records], position)
        return {
            "block_index": block.index,
            "record": record,
            "record_hash": hash_record(record),
            "proof": proof,
            "root": block.merkle_root,
            "block_hash": block.hash,
        }

    # ------------------------------------------------------------------
    def to_list(self) -> list[dict[str, Any]]:
        return [block.to_dict() for block in self.blocks]

    def load(self, blocks: list[dict[str, Any]]) -> None:
        """Replace the in-memory chain with the stored one."""
        self.blocks = [Block.from_dict(b) for b in blocks]
        if not self.blocks:
            self.create_genesis()

    def stats(self) -> dict[str, Any]:
        return {
            "blocks": len(self.blocks),
            "records": sum(len(b.records) for b in self.blocks),
            "difficulty": self.difficulty,
            "last_hash": self.blocks[-1].hash if self.blocks else "",
        }
