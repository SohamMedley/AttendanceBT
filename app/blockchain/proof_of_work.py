"""
Proof-of-Work (PoW) mining.

A block is only valid if the SHA-256 hash of its header satisfies::

    int(block_hash, 16) < 2**256 / target

In practice we express this as a *difficulty* ``d``: the hash must start with
``d`` hexadecimal zeros. Higher difficulty => exponentially more work, but
verification stays O(1) -- a single hash. That asymmetry is the whole point of
Proof-of-Work: expensive to produce, trivial to check.

Our chain uses a small difficulty (2-5) so a classroom demo finishes instantly.
The code is identical to what Bitcoin does at difficulty 10^23; only the number
changes. Bitcoin additionally retargets difficulty every 2016 blocks to hold the
block time near 10 minutes -- ``next_difficulty()`` demonstrates that idea.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

MAX_TARGET = 2**256 - 1
DEFAULT_DIFFICULTY = 4
TARGET_BLOCK_SECONDS = 10.0


@dataclass
class MiningResult:
    """Outcome of a mining attempt -- every field is displayed in the UI."""

    nonce: int
    block_hash: str
    difficulty: int
    attempts: int
    elapsed_seconds: float
    hash_rate: float
    target: str

    def to_dict(self) -> dict[str, object]:
        return {
            "nonce": self.nonce,
            "block_hash": self.block_hash,
            "difficulty": self.difficulty,
            "attempts": self.attempts,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "hash_rate": round(self.hash_rate, 1),
            "target": self.target,
        }


def target_from_difficulty(difficulty: int) -> int:
    """Largest hash value that still counts as a valid Proof-of-Work."""
    if difficulty <= 0:
        return MAX_TARGET
    return MAX_TARGET >> (4 * difficulty)


def target_hex(difficulty: int) -> str:
    return f"{target_from_difficulty(difficulty):064x}"


def meets_difficulty(block_hash: str, difficulty: int) -> bool:
    """Check the proof: does this hash beat the target?

    Fast path -- count leading zeros first, which fails almost immediately for
    bad hashes and keeps chain validation cheap.
    """
    if difficulty <= 0:
        return True
    if block_hash[:difficulty] != "0" * difficulty:
        return False
    return int(block_hash, 16) <= target_from_difficulty(difficulty)


def mine(
    header_fields: dict[str, object],
    difficulty: int = DEFAULT_DIFFICULTY,
    *,
    start_nonce: int = 0,
    max_attempts: int | None = None,
    progress_every: int = 0,
    on_progress=None,
) -> MiningResult:
    """Search for a nonce that makes the header hash satisfy the difficulty.

    ``header_fields`` is the block header as a dict; it is serialised
    deterministically for every attempt so the only thing changing is the nonce.
    """
    target = target_hex(difficulty)
    attempts = 0
    nonce = start_nonce
    started = time.perf_counter()

    while True:
        header_fields = dict(header_fields)
        header_fields["nonce"] = nonce
        candidate = _header_string(header_fields)
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        attempts += 1

        if meets_difficulty(digest, difficulty):
            elapsed = time.perf_counter() - started
            return MiningResult(
                nonce=nonce,
                block_hash=digest,
                difficulty=difficulty,
                attempts=attempts,
                elapsed_seconds=elapsed,
                hash_rate=attempts / elapsed if elapsed > 0 else float("inf"),
                target=target,
            )

        if progress_every and attempts % progress_every == 0 and on_progress:
            on_progress(attempts, time.perf_counter() - started)

        if max_attempts is not None and attempts >= max_attempts:
            raise RuntimeError(
                f"Gave up after {attempts} attempts at difficulty {difficulty}"
            )

        nonce += 1


def _header_string(header_fields: dict[str, object]) -> str:
    """Deterministic header encoding used for hashing.

    Field order is fixed explicitly (rather than relying on dict order) so that
    the same header always produces the same hash on any machine or Python
    version -- essential for a ledger that has to be re-verified years later.
    """
    ordered = [
        "version",
        "index",
        "timestamp",
        "prev_hash",
        "merkle_root",
        "difficulty",
        "miner",
        "nonce",
    ]
    parts = [f"{key}={header_fields.get(key, '')}" for key in ordered]
    return "|".join(parts)


def next_difficulty(
    recent_blocks: list[dict],
    current_difficulty: int,
    target_seconds: float = TARGET_BLOCK_SECONDS,
    min_difficulty: int = 1,
    max_difficulty: int = 6,
) -> int:
    """Difficulty retargeting, the simplified Bitcoin rule.

    If the recent blocks came in faster than the target we raise difficulty,
    if slower we lower it. Bounded here so a live demo can never lock up.
    """
    window = recent_blocks[-10:]
    if len(window) < 3:
        return current_difficulty

    stamps = sorted(float(b.get("timestamp", 0)) for b in window)
    span = stamps[-1] - stamps[0]
    if span <= 0:
        return min(max_difficulty, current_difficulty + 1)

    average = span / (len(stamps) - 1)
    if average < target_seconds * 0.5:
        return min(max_difficulty, current_difficulty + 1)
    if average > target_seconds * 2:
        return max(min_difficulty, current_difficulty - 1)
    return current_difficulty


def estimate_attempts(difficulty: int) -> int:
    """Expected number of hashes needed (a hash succeeds with probability 16^-d)."""
    return 16**difficulty


def benchmark(difficulty: int = 4) -> MiningResult:
    """Mine a throwaway block so students can see the hash rate of their machine."""
    header = {
        "version": 1,
        "index": 999,
        "timestamp": time.time(),
        "prev_hash": "0" * 64,
        "merkle_root": "0" * 64,
        "difficulty": difficulty,
        "miner": "benchmark",
    }
    return mine(header, difficulty)


__all__ = [
    "mine",
    "MiningResult",
    "meets_difficulty",
    "target_from_difficulty",
    "target_hex",
    "next_difficulty",
    "estimate_attempts",
    "benchmark",
    "DEFAULT_DIFFICULTY",
]
