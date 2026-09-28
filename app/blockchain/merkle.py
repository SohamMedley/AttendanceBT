"""
Merkle tree over attendance transactions.

A Merkle tree lets us compress *any number* of attendance records into a single
32-byte root. That root is written into the block header, so:

* Changing one record anywhere in a block changes the root -> the block hash
  changes -> every later block's ``prev_hash`` link breaks. Tampering is
  therefore mathematically detectable.
* A single student can be given a short *Merkle proof* (a handful of hashes)
  proving their record is inside a block, without revealing anyone else's data.
  This is the same technique Bitcoin uses for SPV (Simplified Payment
  Verification) wallets.

Implementation detail: odd nodes are duplicated (Bitcoin's rule), so trees with
non-power-of-two leaf counts still produce a well-defined root.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Sequence

HASH_PREFIX = b"\x00"  # leaf domain separator
NODE_PREFIX = b"\x01"  # internal node domain separator


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_leaf(value: str) -> str:
    """Hash a single transaction id into a leaf node."""
    return sha256_hex(HASH_PREFIX + value.encode("utf-8"))


def hash_pair(left: str, right: str) -> str:
    """Combine two child hashes into their parent."""
    return sha256_hex(NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right))


def merkle_root(leaves: Iterable[str]) -> str:
    """Compute the Merkle root of a sequence of transaction ids.

    Returns the zero-hash for an empty input (an empty block still needs a root).
    """
    level: list[str] = [hash_leaf(item) for item in leaves]
    if not level:
        return "0" * 64

    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate the last node
        level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_levels(leaves: Sequence[str]) -> list[list[str]]:
    """Return every level of the tree -- handy for drawing the tree in the UI."""
    level = [hash_leaf(item) for item in leaves]
    levels = [level[:]]
    if not level:
        return levels
    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]
        level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        levels.append(level[:])
    return levels


def merkle_proof(leaves: Sequence[str], index: int) -> list[dict[str, str]]:
    """Build an inclusion proof (audit path) for the leaf at ``index``.

    The proof is a list of ``{"position": "left"|"right", "hash": ...}`` steps.
    """
    if not leaves:
        raise ValueError("Cannot build a proof for an empty tree")
    if not 0 <= index < len(leaves):
        raise IndexError("Leaf index out of range")

    proof: list[dict[str, str]] = []
    level = [hash_leaf(item) for item in leaves]
    position = index

    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]
        sibling = position ^ 1  # XOR flips the last bit -> the sibling index
        proof.append(
            {
                "position": "left" if sibling < position else "right",
                "hash": level[sibling],
            }
        )
        level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        position //= 2

    return proof


def verify_merkle_proof(leaf: str, proof: Sequence[dict[str, str]], root: str) -> bool:
    """Recompute the root from a leaf + its proof and compare against ``root``."""
    computed = hash_leaf(leaf)
    for step in proof:
        if step["position"] == "left":
            computed = hash_pair(step["hash"], computed)
        else:
            computed = hash_pair(computed, step["hash"])
    return computed == root


__all__ = [
    "merkle_root",
    "merkle_levels",
    "merkle_proof",
    "verify_merkle_proof",
    "hash_leaf",
    "hash_pair",
]
