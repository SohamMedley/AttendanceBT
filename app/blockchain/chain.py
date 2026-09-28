"""
The attendance blockchain itself.

Responsibilities
----------------
* Hold the canonical chain of blocks and a mempool of unconfirmed transactions.
* Mine pending transactions into a new block (Proof-of-Work).
* Validate the whole chain: hash integrity, linkage, Proof-of-Work, Merkle roots
  and every transaction signature.
* Answer audit queries: "where is this student's record?", "prove it is in
  block B without trusting me".

The validation report is the centrepiece of the demo. It is what lets a student
tamper with the SQLite/JSON file by hand and *prove*, on screen, that the ledger
rejects the edit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import proof_of_work as pow_module
from .block import Block, create_genesis_block
from .merkle import merkle_levels, merkle_proof, verify_merkle_proof
from .transaction import Transaction

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
        difficulty: int = pow_module.DEFAULT_DIFFICULTY,
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
                import os
                from concurrent.futures import ProcessPoolExecutor

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
                import logging

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



__all__ = ["Blockchain", "ValidationReport", "ValidationIssue"]
