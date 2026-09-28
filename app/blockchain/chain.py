"""
The attendance blockchain: transactions, blocks, Proof of Work and the chain.

This is the whole ledger in one file, in dependency order -- a transaction is
signed, transactions are gathered into a block, a block is sealed by mining,
and sealed blocks are linked into a chain that can be re-verified from
scratch at any time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from typing import Any, Iterable, Mapping, Sequence
from typing import Any, Mapping

from . import crypto as ecdsa
from .crypto import (
    merkle_levels,
    merkle_proof,
    merkle_root as compute_merkle_root,
    verify_merkle_proof,
)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
# Defined here, above the classes that use them as default arguments, because a
# default argument is evaluated when the class body runs.

MAX_TARGET = 2**256 - 1

#: Leading hexadecimal zeros a block hash must have. 4 is the project default:
#: a block seals in well under a second, which keeps a live demo responsive
#: while still being genuine work.
DEFAULT_DIFFICULTY = 4

#: The block interval the difficulty is tuned for, in seconds.
TARGET_BLOCK_SECONDS = 10.0


# ============================================================================
# Transactions
# ============================================================================
#
# Transaction model for the attendance ledger.
#
# Two transaction types travel on this chain:
#
# ``ATTENDANCE``
#     One student's presence for one lecture session. Signed with the *student's*
#     own secp256k1 private key, so a student cannot repudiate their own record and
#     nobody else can forge one.
#
# ``ANCHOR``
#     A periodic "checkpoint" transaction whose payload is the Merkle root of the
#     attendance transactions since the previous anchor. Anchor transactions are
#     signed with the *institution's* key and are the hand-off point to a public
#     blockchain (see ``app/anchoring``). This is the hybrid anchoring pattern used
#     by real-world supply-chain and academic-credential systems.
#
# Transaction ids are content-addressed: ``tx_id = SHA256(canonical_json(payload))``.
# Because the id is derived from the content, altering any field after the fact
# produces a different id, and the block's Merkle root no longer matches.

TX_ATTENDANCE = "ATTENDANCE"
TX_ANCHOR = "ANCHOR"

STATUS_PRESENT = "PRESENT"
STATUS_LATE = "LATE"
STATUS_ABSENT = "ABSENT"
STATUS_MANUAL = "MANUAL"
VALID_STATUSES = {STATUS_PRESENT, STATUS_LATE, STATUS_ABSENT, STATUS_MANUAL}


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON serialisation.

    Both the signer and the verifier must serialise byte-for-byte identically
    before hashing, otherwise signatures never validate. ``sort_keys`` gives us
    that guarantee regardless of the field insertion order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass
class Transaction:
    """A signed, content-addressed record on the attendance blockchain."""

    tx_type: str
    payload: dict[str, Any]
    sender_pubkey: str
    signature: str = ""
    tx_id: str = ""
    received_at: float = field(default_factory=time.time)

    # -- construction ------------------------------------------------------
    def __post_init__(self) -> None:
        if not self.tx_id:
            self.tx_id = self.compute_id()

    def compute_id(self) -> str:
        """Content address: hash of the canonical payload + type + sender."""
        body = canonical_json(
            {
                "tx_type": self.tx_type,
                "payload": self.payload,
                "sender_pubkey": self.sender_pubkey,
            }
        )
        return sha256_hex(body)

    def signing_bytes(self) -> bytes:
        """Exact byte string that gets signed (and re-signed during verification)."""
        return canonical_json(
            {"tx_type": self.tx_type, "payload": self.payload}
        ).encode("utf-8")

    # -- crypto ------------------------------------------------------------
    def sign(self, private_key: str) -> "Transaction":
        self.signature = ecdsa.sign_message(private_key, self.signing_bytes())
        return self

    def verify_signature(self) -> bool:
        """True when the signature matches both the content and the sender key."""
        if not self.signature or not self.sender_pubkey:
            return False
        return ecdsa.verify_signature(self.sender_pubkey, self.signing_bytes(), self.signature)

    def verify_id(self) -> bool:
        """True when ``tx_id`` really is the hash of this content."""
        return self.tx_id == self.compute_id()

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "tx_type": self.tx_type,
            "payload": dict(self.payload),
            "sender_pubkey": self.sender_pubkey,
            "signature": self.signature,
            "received_at": self.received_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Transaction":
        return cls(
            tx_type=data["tx_type"],
            payload=dict(data["payload"]),
            sender_pubkey=data.get("sender_pubkey", ""),
            signature=data.get("signature", ""),
            tx_id=data.get("tx_id", ""),
            received_at=float(data.get("received_at", time.time())),
        )

    # -- convenience accessors --------------------------------------------
    @property
    def student_roll(self) -> str | None:
        return self.payload.get("student_roll")

    @property
    def session_id(self) -> str | None:
        return self.payload.get("session_id")

    @property
    def status(self) -> str | None:
        return self.payload.get("status")


def build_attendance_transaction(
    *,
    session_id: str,
    student_roll: str,
    student_name: str,
    subject_code: str,
    subject_name: str,
    faculty_id: str,
    room: str,
    status: str,
    marks_at: float,
    session_started_at: float,
    grace_seconds: int,
    device_fingerprint: str = "",
    client_latency_ms: int | None = None,
    nonce: str | None = None,
) -> Transaction:
    """Create (but do not yet sign) an attendance transaction."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown attendance status: {status}")

    payload = {
        "tx_kind": TX_ATTENDANCE,
        "session_id": session_id,
        "student_roll": student_roll,
        "student_name": student_name,
        "subject_code": subject_code,
        "subject_name": subject_name,
        "faculty_id": faculty_id,
        "room": room,
        "status": status,
        "marked_at": round(marks_at, 3),
        "session_started_at": round(session_started_at, 3),
        "grace_seconds": grace_seconds,
        "device_fingerprint": device_fingerprint,
        "client_latency_ms": client_latency_ms,
        "nonce": nonce or uuid.uuid4().hex[:16],
        "network": "BCOE-ATTENDANCE-CHAIN",
    }
    # sender_pubkey is filled in by the caller (the student's identity key)
    return Transaction(tx_type=TX_ATTENDANCE, payload=payload, sender_pubkey="")


def build_anchor_transaction(
    *,
    anchor_id: str,
    merkle_root: str,
    previous_anchor_root: str | None,
    from_block: int,
    to_block: int,
    tx_count: int,
    created_at: float,
    chain_name: str = "BCOE-ATTENDANCE-CHAIN",
) -> Transaction:
    """Create (but do not yet sign) an anchoring checkpoint transaction."""
    payload = {
        "tx_kind": TX_ANCHOR,
        "anchor_id": anchor_id,
        "merkle_root": merkle_root,
        "previous_anchor_root": previous_anchor_root,
        "from_block": from_block,
        "to_block": to_block,
        "tx_count": tx_count,
        "created_at": round(created_at, 3),
        "chain_name": chain_name,
        "anchor_version": 1,
    }
    return Transaction(tx_type=TX_ANCHOR, payload=payload, sender_pubkey="")


# ============================================================================
# Blocks
# ============================================================================
#
# Block model.
#
# A block is a *header* plus a list of transactions::
#
#     +--------------------------------------------------------------+
#     |  index | timestamp | prev_hash | merkle_root | difficulty |   |
#     |  miner | nonce                                        |   |
#     +--------------------------------------------------------------+
#     |  [ tx_id_1, tx_id_2, tx_id_3, ... ]  (transactions)          |
#     +--------------------------------------------------------------+
#
# ``prev_hash`` is what turns a pile of blocks into a *chain*: each block commits
# to the entire history before it. Alter block N and its hash changes, so block
# N+1's ``prev_hash`` no longer matches, and so on to the tip. To rewrite history
# an attacker would have to re-mine every subsequent block faster than the honest
# network -- the security guarantee behind Bitcoin.
#
# ``merkle_root`` commits to every transaction in the block without embedding them
# in the header, which is what keeps headers small and light clients cheap.

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
    difficulty: int = DEFAULT_DIFFICULTY
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
        return _sha256(_header_string(fields))

    def refresh_merkle_root(self) -> str:
        self.merkle_root = self.compute_merkle_root()
        return self.merkle_root

    # -- mining ------------------------------------------------------------
    def seal(self, difficulty: int | None = None) -> dict[str, Any]:
        """Run Proof-of-Work and freeze the block's hash."""
        if difficulty is not None:
            self.difficulty = difficulty
        self.refresh_merkle_root()
        result = mine(self.header_fields(), self.difficulty)
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
        return meets_difficulty(self.hash, self.difficulty)

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
            difficulty=int(data.get("difficulty", DEFAULT_DIFFICULTY)),
            nonce=int(data.get("nonce", 0)),
            miner=data.get("miner", "BCOE-NODE-01"),
            version=int(data.get("version", CHAIN_VERSION)),
            note=data.get("note", ""),
            hash=data.get("hash", ""),
        )
        block.merkle_root = data.get("merkle_root", block.compute_merkle_root())
        block.mining_stats = data.get("mining_stats")
        return block


def create_genesis_block(difficulty: int = DEFAULT_DIFFICULTY) -> Block:
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

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ============================================================================
# Proof of Work
# ============================================================================
#
# Proof-of-Work (PoW) mining.
#
# A block is only valid if the SHA-256 hash of its header satisfies::
#
#     int(block_hash, 16) < 2**256 / target
#
# In practice we express this as a *difficulty* ``d``: the hash must start with
# ``d`` hexadecimal zeros. Higher difficulty => exponentially more work, but
# verification stays O(1) -- a single hash. That asymmetry is the whole point of
# Proof-of-Work: expensive to produce, trivial to check.
#
# Our chain uses a small difficulty (2-5) so a classroom demo finishes instantly.
# The code is identical to what Bitcoin does at difficulty 10^23; only the number
# changes. Bitcoin additionally retargets difficulty every 2016 blocks to hold the
# block time near 10 minutes -- ``next_difficulty()`` demonstrates that idea.


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


# ============================================================================
# The chain itself
# ============================================================================
#
# The attendance blockchain itself.
#
# Responsibilities
# ----------------
# * Hold the canonical chain of blocks and a mempool of unconfirmed transactions.
# * Mine pending transactions into a new block (Proof-of-Work).
# * Validate the whole chain: hash integrity, linkage, Proof-of-Work, Merkle roots
#   and every transaction signature.
# * Answer audit queries: "where is this student's record?", "prove it is in
#   block B without trusting me".
#
# The validation report is the centrepiece of the demo. It is what lets a student
# tamper with the SQLite/JSON file by hand and *prove*, on screen, that the ledger
# rejects the edit.

MAX_MEMPOOL = 500


@dataclass
class ValidationIssue:
    """A single problem found while validating the chain."""

    severity: str  # "CRITICAL" | "WARNING"
    block_index: int
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "block_index": self.block_index,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ValidationReport:
    """Full audit result for the chain."""

    valid: bool
    checked_blocks: int
    checked_transactions: int
    issues: list[ValidationIssue] = field(default_factory=list)
    duration_ms: float = 0.0
    checked_at: float = field(default_factory=time.time)
    deep: bool = True
    signatures_checked: int = 0
    signature_blocks_verified: list[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "CRITICAL")

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_blocks": self.checked_blocks,
            "checked_transactions": self.checked_transactions,
            "issues": [issue.to_dict() for issue in self.issues],
            "duration_ms": round(self.duration_ms, 2),
            "checked_at": self.checked_at,
            "critical_count": self.critical_count,
            "deep": self.deep,
            "signatures_checked": self.signatures_checked,
            "signature_blocks_verified": self.signature_blocks_verified,
            "headline": (
                "CHAIN VERIFIED - no tampering detected"
                if self.valid
                else f"CHAIN INVALID - {self.critical_count} critical issue(s) found"
            ),
        }


def _chunk(items: Sequence[Any], parts: int) -> list[list[Any]]:
    """Split ``items`` into ``parts`` roughly equal chunks."""
    parts = max(1, parts)
    size, remainder = divmod(len(items), parts)
    chunks: list[list[Any]] = []
    start = 0
    for index in range(parts):
        end = start + size + (1 if index < remainder else 0)
        chunks.append(list(items[start:end]))
        start = end
    return [chunk for chunk in chunks if chunk]


def _verify_block_signature_chunk(block_payloads: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Verify every transaction signature in a list of blocks.

    Defined at module level (not as a method) so it can be dispatched to worker
    processes. Returns plain dicts, which are picklable.
    """
    issues: list[dict] = []
    for payload in block_payloads:
        index = int(payload.get("index", -1))
        for raw_tx in payload.get("transactions", []):
            tx = Transaction.from_dict(raw_tx)
            if not tx.verify_signature():
                issues.append(
                    {
                        "severity": "CRITICAL",
                        "block_index": index,
                        "code": "BAD_SIGNATURE",
                        "message": (
                            f"Invalid ECDSA signature on transaction "
                            f"{tx.tx_id[:12]}..."
                        ),
                        "detail": {
                            "student": tx.payload.get("student_roll"),
                            "session": tx.payload.get("session_id"),
                            "sender_pubkey": tx.sender_pubkey,
                        },
                    }
                )
    return issues


class Blockchain:
    """An append-only, Proof-of-Work secured attendance ledger."""

    #: Seal automatically once this many transactions are waiting. Batching keeps
    #: one lecture's roll-call inside a single block, which is both realistic and
    #: much faster than mining per student.
    DEFAULT_SEAL_THRESHOLD = 25

    def __init__(
        self,
        difficulty: int = DEFAULT_DIFFICULTY,
        *,
        node_name: str = "BCOE-NODE-01",
        auto_seal: bool = True,
        seal_threshold: int = DEFAULT_SEAL_THRESHOLD,
    ) -> None:
        self.difficulty = difficulty
        self.node_name = node_name
        self.auto_seal = auto_seal
        self.seal_threshold = seal_threshold
        self.chain: list[Block] = [create_genesis_block(difficulty)]
        self.mempool: list[Transaction] = []
        self.seen_tx_ids: set[str] = set()
        self.seen_student_session: set[tuple[str, str]] = set()
        self.last_validation: ValidationReport | None = None
        #: Block hashes whose signatures have already been verified. The ledger
        #: loads this from storage at startup, so re-validating a long chain is
        #: not a repeated 30-second penalty.
        self.signature_cache: set[str] | None = None

    # ------------------------------------------------------------------
    # Chain basics
    # ------------------------------------------------------------------
    @property
    def head(self) -> Block:
        return self.chain[-1]

    @property
    def height(self) -> int:
        return len(self.chain) - 1

    def __len__(self) -> int:
        return len(self.chain)

    def block_by_index(self, index: int) -> Block | None:
        if 0 <= index < len(self.chain):
            return self.chain[index]
        return None

    def block_by_hash(self, block_hash: str) -> Block | None:
        for block in self.chain:
            if block.hash == block_hash:
                return block
        return None

    # ------------------------------------------------------------------
    # Mempool
    # ------------------------------------------------------------------
    def add_transaction(self, tx: Transaction, *, verify: bool = True) -> tuple[bool, str]:
        """Validate and queue a transaction for the next block.

        Returns ``(accepted, reason)``. Rejections here are exactly the rules a
        real node enforces before relaying a transaction.
        """
        if verify:
            if not tx.verify_id():
                return False, "Transaction id does not match its content"
            if not tx.verify_signature():
                return False, "Invalid signature"

        if tx.tx_id in self.seen_tx_ids:
            return False, "Duplicate transaction (already on chain or in mempool)"

        if tx.tx_type == "ATTENDANCE":
            key = (str(tx.payload.get("student_roll")), str(tx.payload.get("session_id")))
            if key in self.seen_student_session:
                return False, "Duplicate attendance for this student and session"

        if len(self.mempool) >= MAX_MEMPOOL:
            return False, "Mempool is full"

        self.mempool.append(tx)
        self.seen_tx_ids.add(tx.tx_id)
        if tx.tx_type == "ATTENDANCE":
            self.seen_student_session.add(
                (str(tx.payload.get("student_roll")), str(tx.payload.get("session_id")))
            )

        if self.auto_seal and len(self.mempool) >= self.seal_threshold:
            return True, "Queued; mempool threshold reached, sealing a block"
        return True, "Queued in mempool"

    def is_duplicate_attendance(self, student_roll: str, session_id: str) -> bool:
        return (student_roll, session_id) in self.seen_student_session

    # ------------------------------------------------------------------
    # Mining
    # ------------------------------------------------------------------
    def mine_pending(
        self,
        *,
        difficulty: int | None = None,
        note: str = "",
        miner: str | None = None,
    ) -> Block | None:
        """Seal all pending transactions into a new block."""
        if not self.mempool:
            return None

        block = Block(
            index=self.head.index + 1,
            timestamp=time.time(),
            prev_hash=self.head.hash,
            transactions=list(self.mempool),
            difficulty=difficulty if difficulty is not None else self.difficulty,
            miner=miner or self.node_name,
            note=note,
        )
        block.seal()
        self.chain.append(block)
        self.mempool.clear()
        return block

    def force_mining_difficulty(self, difficulty: int) -> None:
        """Turn auto-sealing off above difficulty 4 so the UI can show progress."""
        self.difficulty = difficulty
        self.auto_seal = difficulty <= 4

    def sealed_attendance_count(self) -> int:
        return sum(
            1
            for block in self.chain
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        )

    # ------------------------------------------------------------------
    # Validation -- the heart of the tamper-evidence claim
    # ------------------------------------------------------------------
    def validate(
        self,
        *,
        deep: bool = True,
        signature_cache: set[str] | None = None,
        parallel: bool = False,
    ) -> ValidationReport:
        """Re-derive every hash and signature from scratch.

        Nothing stored in the file is trusted: hashes are recomputed, Merkle roots
        rebuilt, Proof-of-Work re-checked and every ECDSA signature re-verified.

        Cost control
        ------------
        Pure-Python ECDSA verification costs roughly 25 ms per record, so a
        deep audit of a few thousand records takes tens of seconds. Two
        mechanisms keep that out of the user's way:

        ``deep=False``
            Runs every *cheap* structural check (header hash, Merkle root,
            Proof-of-Work, chain linkage, transaction id). This is instant and
            still catches all four levels of the tamper demo, because each level
            breaks a hash rather than a signature.

        ``signature_cache``
            A set of block hashes whose signatures have already been verified.
            A block hash commits to every transaction in the block, so if
            anything inside a block is altered the hash changes, the cache key
            is missed, and the block is re-verified automatically. The cache can
            therefore never mask a modification -- it only avoids repeating work
            that is provably still valid.
        """
        started = time.perf_counter()
        issues: list[ValidationIssue] = []
        checked_txs = 0
        if signature_cache is None:
            # Fall back to the chain's own cache (loaded from storage). Passing an
            # explicit empty set forces a complete re-verification.
            signature_cache = set(self.signature_cache or ())

        # Which blocks still need their signatures checked?
        pending_signature_blocks: list[Block] = []
        if deep:
            for block in self.chain:
                if len(block.transactions) == 0:
                    continue
                if signature_cache and block.hash in signature_cache:
                    continue
                pending_signature_blocks.append(block)

        signature_issues: list[ValidationIssue] = []
        if pending_signature_blocks:
            signature_issues = self._verify_signatures(
                pending_signature_blocks, parallel=parallel
            )

        genesis = self.chain[0] if self.chain else None
        if genesis is None:
            issues.append(
                ValidationIssue("CRITICAL", -1, "NO_GENESIS", "The chain has no blocks")
            )
        else:
            if genesis.index != 0:
                issues.append(
                    ValidationIssue(
                        "CRITICAL", 0, "GENESIS_INDEX", "Genesis block index must be 0"
                    )
                )
            if genesis.prev_hash != "0" * 64:
                issues.append(
                    ValidationIssue(
                        "CRITICAL",
                        0,
                        "GENESIS_PREV_HASH",
                        "Genesis prev_hash must be 64 zeros",
                    )
                )

        for position, block in enumerate(self.chain):
            # 1. Header hash integrity
            if not block.hash_is_valid():
                issues.append(
                    ValidationIssue(
                        "CRITICAL",
                        block.index,
                        "BLOCK_HASH_MISMATCH",
                        f"Block {block.index} header was modified after mining",
                        {
                            "stored_hash": block.hash,
                            "recomputed_hash": block.recompute_hash(),
                        },
                    )
                )

            # 2. Merkle root
            if not block.merkle_is_valid():
                issues.append(
                    ValidationIssue(
                        "CRITICAL",
                        block.index,
                        "MERKLE_ROOT_MISMATCH",
                        f"Transactions in block {block.index} do not match its Merkle root",
                        {
                            "stored_root": block.merkle_root,
                            "recomputed_root": block.compute_merkle_root(),
                        },
                    )
                )

            # 3. Proof-of-Work
            if not block.pow_is_valid():
                issues.append(
                    ValidationIssue(
                        "CRITICAL",
                        block.index,
                        "INVALID_PROOF_OF_WORK",
                        f"Block {block.index} hash does not satisfy difficulty {block.difficulty}",
                        {"hash": block.hash, "difficulty": block.difficulty},
                    )
                )

            # 4. Chain linkage
            if position > 0:
                parent = self.chain[position - 1]
                if block.prev_hash != parent.hash:
                    issues.append(
                        ValidationIssue(
                            "CRITICAL",
                            block.index,
                            "BROKEN_LINK",
                            f"Block {block.index} does not link to block {parent.index}",
                            {
                                "expected_prev_hash": parent.hash,
                                "stored_prev_hash": block.prev_hash,
                            },
                        )
                    )
                if block.index != parent.index + 1:
                    issues.append(
                        ValidationIssue(
                            "CRITICAL",
                            block.index,
                            "INDEX_GAP",
                            f"Block index {block.index} follows {parent.index}",
                        )
                    )
                if block.timestamp < parent.timestamp - 1:
                    issues.append(
                        ValidationIssue(
                            "WARNING",
                            block.index,
                            "TIMESTAMP_REGRESSION",
                            f"Block {block.index} is timestamped before its parent",
                        )
                    )

            # 5. Transaction-level checks
            local_ids: set[str] = set()
            for tx in block.transactions:
                checked_txs += 1
                if not tx.verify_id():
                    issues.append(
                        ValidationIssue(
                            "CRITICAL",
                            block.index,
                            "TX_ID_MISMATCH",
                            f"Transaction {tx.tx_id[:12]}... content was altered",
                            {"stored_tx_id": tx.tx_id, "recomputed": tx.compute_id()},
                        )
                    )
                if tx.tx_id in local_ids:
                    issues.append(
                        ValidationIssue(
                            "CRITICAL",
                            block.index,
                            "DUPLICATE_TX",
                            f"Transaction {tx.tx_id[:12]}... appears twice in the block",
                        )
                    )
                local_ids.add(tx.tx_id)

        issues.extend(signature_issues)
        critical = [issue for issue in issues if issue.severity == "CRITICAL"]
        report = ValidationReport(
            valid=not critical,
            checked_blocks=len(self.chain),
            checked_transactions=checked_txs,
            issues=issues,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        report.deep = deep
        report.signatures_checked = sum(
            len(block.transactions) for block in pending_signature_blocks
        )
        report.signature_blocks_verified = [
            block.hash for block in pending_signature_blocks
        ] if not signature_issues else []
        self.last_validation = report
        return report

    # ------------------------------------------------------------------
    def _verify_signatures(
        self, blocks: Sequence[Block], *, parallel: bool = False
    ) -> list[ValidationIssue]:
        """Verify the ECDSA signature of every transaction in ``blocks``.

        Verification is CPU-bound and embarrassingly parallel, so with more than
        one core available we spread the blocks across a process pool. The worker
        is a module-level function so it can be pickled to the child processes.
        """
        payloads = [block.to_dict() for block in blocks]

        if parallel and len(payloads) > 1:
            try:

                workers = max(1, min(os.cpu_count() or 1, 8))
                if workers > 1:
                    with ProcessPoolExecutor(max_workers=workers) as pool:
                        chunks = pool.map(
                            _verify_block_signature_chunk,
                            _chunk(payloads, workers),
                        )
                    return [
                        ValidationIssue(**issue)
                        for chunk_issues in chunks
                        for issue in chunk_issues
                    ]
            except Exception as exc:  # pragma: no cover - pool may be unavailable

                logging.getLogger("bcoe.chain").warning(
                    "Parallel verification unavailable (%s); falling back to serial", exc
                )

        return [
            ValidationIssue(**issue)
            for issue in _verify_block_signature_chunk(payloads)
        ]

    # ------------------------------------------------------------------
    # Queries / audit
    # ------------------------------------------------------------------
    def find_transaction(self, tx_id: str) -> tuple[Block, Transaction] | None:
        for block in self.chain:
            for tx in block.transactions:
                if tx.tx_id == tx_id:
                    return block, tx
        return None

    def transactions_for_student(self, roll_no: str) -> list[dict[str, Any]]:
        """Every attendance record belonging to a student, with its on-chain proof."""
        records: list[dict[str, Any]] = []
        for block in self.chain:
            for tx in block.transactions:
                if tx.tx_type != "ATTENDANCE":
                    continue
                if str(tx.payload.get("student_roll")).upper() != roll_no.upper():
                    continue
                records.append(
                    {
                        "tx_id": tx.tx_id,
                        "block_index": block.index,
                        "block_hash": block.hash,
                        "timestamp": block.timestamp,
                        "payload": tx.payload,
                        "signature": tx.signature,
                    }
                )
        records.sort(key=lambda item: item["payload"].get("marked_at", 0), reverse=True)
        return records

    def transactions_for_session(self, session_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for block in self.chain:
            for tx in block.transactions:
                if tx.tx_type == "ATTENDANCE" and tx.payload.get("session_id") == session_id:
                    records.append(
                        {
                            "tx_id": tx.tx_id,
                            "block_index": block.index,
                            "block_hash": block.hash,
                            "payload": tx.payload,
                        }
                    )
        return records

    def proof_of_inclusion(self, tx_id: str) -> dict[str, Any] | None:
        """Build a Merkle inclusion proof for one transaction."""
        for block in self.chain:
            ids = [tx.tx_id for tx in block.transactions]
            if tx_id in ids:
                index = ids.index(tx_id)
                proof = merkle_proof(ids, index)
                return {
                    "tx_id": tx_id,
                    "block_index": block.index,
                    "block_hash": block.hash,
                    "merkle_root": block.merkle_root,
                    "leaf_index": index,
                    "proof": proof,
                    "verified": verify_merkle_proof(tx_id, proof, block.merkle_root),
                }
        return None

    def merkle_tree(self, block_index: int) -> dict[str, Any] | None:
        block = self.block_by_index(block_index)
        if block is None:
            return None
        ids = [tx.tx_id for tx in block.transactions]
        return {
            "block_index": block.index,
            "merkle_root": block.merkle_root,
            "levels": merkle_levels(ids),
            "tx_ids": ids,
        }

    # ------------------------------------------------------------------
    # Stats & serialisation
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        attendance = [
            tx
            for block in self.chain
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        ]
        anchors = [
            tx
            for block in self.chain
            for tx in block.transactions
            if tx.tx_type == "ANCHOR"
        ]
        mining_times = [
            block.mining_stats.get("elapsed_seconds", 0)
            for block in self.chain
            if block.mining_stats
        ]
        total_attempts = sum(
            block.mining_stats.get("attempts", 0)
            for block in self.chain
            if block.mining_stats
        )
        size = sum(block.size_bytes for block in self.chain)
        first_receipt = min(
            (tx.received_at for tx in attendance), default=None
        )
        last_receipt = max((tx.received_at for tx in attendance), default=None)
        return {
            "chain_name": "BCOE-ATTENDANCE-CHAIN",
            "height": self.height,
            "blocks": len(self.chain),
            "sealed_attendance_transactions": len(attendance),
            "anchor_transactions": len(anchors),
            "mempool_size": len(self.mempool),
            "difficulty": self.head.difficulty,
            "auto_seal": self.auto_seal,
            "total_hash_attempts": total_attempts,
            "total_mining_seconds": round(sum(mining_times), 3),
            "average_block_seconds": round(
                sum(mining_times) / len(mining_times), 4
            )
            if mining_times
            else 0.0,
            "estimated_bytes_on_disk": size,
            "genesis_hash": self.chain[0].hash,
            "head_hash": self.head.hash,
            "head_timestamp": self.head.timestamp,
            "first_receipt_at": first_receipt,
            "last_receipt_at": last_receipt,
            "students_covered": len(
                {tx.payload.get("student_roll") for tx in attendance}
            ),
            "subjects_covered": len(
                {tx.payload.get("subject_code") for tx in attendance}
            ),
        }

    def export_chain(self) -> dict[str, Any]:
        return {
            "chain_name": "BCOE-ATTENDANCE-CHAIN",
            "version": 1,
            "exported_at": time.time(),
            "difficulty": self.difficulty,
            "node": self.node_name,
            "stats": self.stats(),
            "blocks": [block.to_dict() for block in self.chain],
        }

    def export_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.export_chain(), indent=indent, default=str)

    def load_blocks(self, blocks: Sequence[Mapping[str, Any]]) -> None:
        """Replace the in-memory chain with previously persisted blocks.

        The genesis block is canonical and deterministic (fixed timestamp, fixed
        note, difficulty capped at 2), so if storage is missing block 0 we can
        rebuild the *exact* block that the rest of the chain links back to.
        Without this, a stored chain would have no valid root of trust and every
        ``prev_hash`` check would fail.
        """
        self.chain = [Block.from_dict(raw) for raw in blocks]
        if not self.chain or self.chain[0].index != 0:
            self.chain.insert(0, create_genesis_block(self.difficulty))
        self.reindex()

    def genesis_is_persisted(self) -> bool:
        return bool(self.chain) and self.chain[0].index == 0

    def reindex(self) -> None:
        """Rebuild the duplicate-detection indexes after loading from storage."""
        self.seen_tx_ids = {
            tx.tx_id for block in self.chain for tx in block.transactions
        }
        self.seen_student_session = {
            (str(tx.payload.get("student_roll")), str(tx.payload.get("session_id")))
            for block in self.chain
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        }
        for tx in self.mempool:
            self.seen_tx_ids.add(tx.tx_id)

    # ------------------------------------------------------------------
    # Deliberate tampering (for the live demo)
    # ------------------------------------------------------------------
    #: What each escalation level does, and what it is expected to break.
    TAMPER_LEVELS = {
        1: "Edit the attendance status only (naive data edit)",
        2: "Edit the status and also recompute the transaction id",
        3: "Edit the status, fix the transaction id, and rebuild the Merkle root",
        4: "Do all of the above and re-mine the block with a valid Proof-of-Work",
    }

    def tamper(
        self,
        *,
        tx_id: str,
        field_name: str = "status",
        new_value: Any = "PRESENT",
        level: int = 1,
    ) -> dict[str, Any]:
        """Tamper with a confirmed record to demonstrate tamper-evidence.

        The demo escalates like a real attacker would, and each level is caught by
        a *deeper* layer of the design:

        ========  =========================================================
        Level 1   payload edited -> ``tx_id`` no longer hashes correctly
                  (and the ECDSA signature no longer verifies)
        Level 2   attacker also rebuilds ``tx_id`` -> the block's Merkle root
                  no longer matches its transactions
        Level 3   attacker also rebuilds the Merkle root -> the block header
                  changed, so the stored block hash is wrong and the
                  Proof-of-Work is invalid
        Level 4   attacker also re-mines the block -> the *next* block's
                  ``prev_hash`` no longer points at it, so the chain breaks
        ========  =========================================================

        The lesson: the only way to fully succeed is to re-mine every block back
        to the tip faster than the honest network -- the exact cost that makes a
        Proof-of-Work ledger immutable.
        """
        if level not in self.TAMPER_LEVELS:
            raise ValueError("level must be 1, 2, 3 or 4")

        found = self.find_transaction(tx_id)
        if found is None:
            raise KeyError(f"Transaction {tx_id} not found")
        block, tx = found

        before = tx.payload.get(field_name)
        # Make sure the edit is a real change. "Turning a student PRESENT when
        # they were already PRESENT" would prove nothing, so pick a contrasting
        # value in that case.
        applied = new_value
        if applied == before:
            if field_name == "status":
                applied = "ABSENT" if before in {"PRESENT", "LATE", "MANUAL"} else "PRESENT"
            else:
                applied = f"{before}-tampered"

        steps: list[str] = []

        # Step 1 -- the actual data edit
        tx.payload[field_name] = applied
        steps.append(f"payload.{field_name}: {before!r} -> {applied!r}")

        if level >= 2:
            new_id = tx.compute_id()
            tx.tx_id = new_id
            steps.append(f"recomputed tx_id -> {new_id[:16]}...")

        if level >= 3:
            block.refresh_merkle_root()
            steps.append(f"rebuilt merkle_root -> {block.merkle_root[:16]}...")

        if level >= 4:
            block.seal()
            steps.append(
                f"re-mined block {block.index} -> nonce {block.nonce}, "
                f"hash {block.hash[:16]}..."
            )

        report = self.validate()
        return {
            "tx_id": tx_id,
            "block_index": block.index,
            "field": field_name,
            "before": before,
            "after": applied,
            "level": level,
            "level_description": self.TAMPER_LEVELS[level],
            "steps": steps,
            "report": report.to_dict(),
            "detected": not report.valid,
            "detected_by": [issue["code"] for issue in report.to_dict()["issues"]],
            "verdict": (
                f"CAUGHT at level {level}: "
                + ", ".join(sorted({i.code for i in report.issues}))
                if not report.valid
                else "NOT DETECTED (the attacker fully rewrote the chain tip)"
            ),
        }

    def tamper_with_transaction(
        self, tx_id: str, *, field_name: str = "status", new_value: Any = "PRESENT"
    ) -> dict[str, Any]:
        """Backwards-compatible alias for a level-1 tamper."""
        return self.tamper(
            tx_id=tx_id, field_name=field_name, new_value=new_value, level=1
        )
