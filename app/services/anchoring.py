"""
Anchor service -- decides *what* to anchor and records the proof.

Two roots are involved, and conflating them is a common mistake:

``records_root``
    The Merkle root of every attendance transaction sealed since the previous
    anchor. This is the value that actually goes to the public ledger, and it is
    what makes "these exact records existed at this moment" provable.

``anchor_tx_id``
    The hash of the anchor transaction that we write into *our own* chain. That
    transaction's payload contains ``records_root`` plus the block range and a
    link to the previous anchor root, so our chain carries an auditable record
    of every public commitment.

The anchor chain (``previous_anchor_root``) is what prevents an attacker from
quietly *deleting* an inconvenient anchor: anchors form a hash-linked chain of
their own, and each one is committed to a block of the main chain.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from ..anchoring import build_provider
from ..blockchain.merkle import merkle_root as compute_merkle_root
from ..blockchain.transaction import TX_ATTENDANCE, build_anchor_transaction
from .identity import KeyStore, ensure_institution_identity
from .ledger import Ledger, LedgerError, MarkResult  # noqa: F401  (re-exported types)
from .repository import Repository

log = logging.getLogger("bcoe.anchor")


class AnchorService:
    """Creates anchor transactions and submits roots to the configured provider."""

    def __init__(self, config, repository: Repository, ledger: Ledger, keystore: KeyStore) -> None:
        self.config = config
        self.repo = repository
        self.ledger = ledger
        self.keystore = keystore
        self.provider = build_provider(config)

    # ------------------------------------------------------------------
    def _attendance_tx_ids(self, from_block: int, to_block: int) -> list[str]:
        """Attendance transaction ids inside a block range, in chain order.

        Order matters: the Merkle root is only reproducible if both the anchoring
        and the verifying side walk the chain the same way.
        """
        tx_ids: list[str] = []
        for block in self.ledger.chain.chain:
            if from_block <= block.index <= to_block:
                tx_ids.extend(
                    tx.tx_id for tx in block.transactions if tx.tx_type == TX_ATTENDANCE
                )
        return tx_ids

    def _pending_range(self) -> tuple[int, int, list[str]]:
        """Block range and transaction ids sealed since the last anchor."""
        latest = self.repo.latest_anchor()
        from_block = int(latest.get("to_block", 0)) + 1 if latest else 1
        to_block = self.ledger.chain.head.index
        return from_block, to_block, self._attendance_tx_ids(from_block, to_block)

    # ------------------------------------------------------------------
    def anchor_now(self, *, actor: str = "faculty", note: str = "") -> dict[str, Any]:
        """Seal pending records, publish the root, and store the proof."""
        institution = ensure_institution_identity(self.keystore)

        from_block, to_block, tx_ids = self._pending_range()
        latest = self.repo.latest_anchor()
        previous_root = latest.get("merkle_root") if latest else None

        # Two modes, both fully reproducible by `verify_anchor`:
        #
        # INCREMENTAL  a range with new records -> root over exactly those records
        # CUMULATIVE   nothing new since the last anchor -> re-commit to *every*
        #              attendance record on the chain. Still a meaningful public
        #              commitment, and still independently verifiable, which a
        #              "root of whatever the head block happens to be" would not be.
        if tx_ids:
            mode = "INCREMENTAL"
            records_root = compute_merkle_root(tx_ids)
        else:
            mode = "CUMULATIVE"
            from_block = 1
            to_block = self.ledger.chain.head.index
            tx_ids = self._attendance_tx_ids(1, to_block)
            records_root = compute_merkle_root(tx_ids)
            note = note or (
                "No new attendance records since the last anchor; re-committing to "
                "every record on the chain"
            )
        anchor_id = (
            f"ANC-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        )
        created_at = time.time()

        # -- 1. write the anchor transaction into our own chain ---------
        transaction = build_anchor_transaction(
            anchor_id=anchor_id,
            merkle_root=records_root,
            previous_anchor_root=previous_root,
            from_block=from_block,
            to_block=max(to_block, from_block - 1),
            tx_count=len(tx_ids),
            created_at=created_at,
            chain_name="BCOE-ATTENDANCE-CHAIN",
        )
        transaction.payload["note"] = note
        transaction.payload["provider"] = self.provider.name
        transaction.sender_pubkey = institution.public_key
        transaction.sign(institution.private_key)
        transaction.tx_id = transaction.compute_id()

        accepted, reason = self.ledger.chain.add_transaction(transaction)
        if not accepted:
            raise LedgerError(f"Anchor transaction rejected: {reason}", "ANCHOR_REJECTED", 409)

        # Sealed immediately, regardless of difficulty, so the anchor is on-chain
        # before we hand the root to an external provider.
        seal_block = self.ledger.chain.mine_pending(
            note=f"Anchor checkpoint {anchor_id}", miner=institution.address
        )
        if seal_block is not None:
            self.ledger.persist_block(seal_block)

        # -- 2. publish the root to the chosen provider ----------------
        receipt = self.provider.submit(
            records_root,
            {
                "anchor_id": anchor_id,
                "previous_anchor_root": previous_root,
                "block_index": seal_block.index if seal_block else None,
                "created_at": created_at,
                "tx_count": len(tx_ids),
            },
        )

        record = {
            **receipt.to_dict(),
            "previous_anchor_root": previous_root,
            "mode": mode,
            "from_block": from_block,
            "to_block": max(to_block, from_block - 1),
            "records_anchored": len(tx_ids),
            "anchor_tx_id": transaction.tx_id,
            "anchor_block_index": seal_block.index if seal_block else None,
            "anchor_block_hash": seal_block.hash if seal_block else None,
            "institution_address": institution.address,
            "note": note,
        }
        self.repo.save_anchor(record)
        self.repo.log_audit(
            "ANCHOR_CREATED",
            actor=actor,
            target=anchor_id,
            detail={
                "provider": receipt.provider,
                "status": receipt.status,
                "root": records_root,
                "records": len(tx_ids),
                "independent": receipt.independent,
            },
        )
        return record

    # ------------------------------------------------------------------
    def verify_anchor(self, anchor_id: str) -> dict[str, Any]:
        """Re-check an anchor against the chain, without trusting the stored record."""
        anchor = self.repo.get_anchor(anchor_id)
        if anchor is None:
            raise LedgerError(f"Unknown anchor {anchor_id}", "UNKNOWN_ANCHOR", 404)

        from_block = int(anchor.get("from_block", 1))
        to_block = int(anchor.get("to_block", 0))
        stored_root = anchor.get("merkle_root")

        # The committed set is exactly the attendance records in the stored block
        # range -- re-derive them from the chain in the same order, so the root is
        # genuinely recomputed rather than read back from the anchor record.
        tx_ids = self._attendance_tx_ids(from_block, to_block)
        recomputed = compute_merkle_root(tx_ids) if tx_ids else None

        found = self.ledger.chain.find_transaction(anchor.get("anchor_tx_id", ""))
        signature_ok = bool(found and found[1].verify_signature())

        checks = [
            {
                "check": "Merkle root recomputed from the chain",
                "expected": stored_root,
                "actual": recomputed,
                "passed": (recomputed == stored_root) if recomputed else None,
                "detail": (
                    f"{len(tx_ids)} attendance records in blocks "
                    f"{from_block}–{to_block} ({anchor.get('mode', 'INCREMENTAL')} anchor)"
                ),
            },
            {
                "check": "Anchor transaction found on the chain",
                "expected": anchor.get("anchor_tx_id"),
                "actual": found[1].tx_id if found else None,
                "passed": bool(found),
            },
            {
                "check": "Anchor signed by the institutional key",
                "expected": anchor.get("institution_address"),
                "actual": anchor.get("institution_address"),
                "passed": signature_ok,
            },
        ]
        return {
            "anchor_id": anchor_id,
            "provider": anchor.get("provider"),
            "status": anchor.get("status"),
            "independent": anchor.get("independent"),
            "records_anchored": anchor.get("records_anchored"),
            "merkle_root": anchor.get("merkle_root"),
            "checks": checks,
            "verified": all(check["passed"] for check in checks if check["passed"] is not None),
            "caveat": (
                "Offline verification proves the root matches our chain. Proving it "
                "was published externally requires the provider's own receipt "
                "(see the stored proof or the explorer link)."
            ),
        }

    # ------------------------------------------------------------------
    def pending_summary(self) -> dict[str, Any]:
        """How much is waiting to be anchored -- shown on the Anchoring page."""
        from_block, to_block, tx_ids = self._pending_range()
        latest = self.repo.latest_anchor()
        return {
            "provider": self.provider.name,
            "provider_info": self.provider.describe(),
            "pending_records": len(tx_ids),
            "from_block": from_block,
            "to_block": to_block,
            "pending_root": compute_merkle_root(tx_ids) if tx_ids else None,
            "last_anchor": latest,
            "anchor_count": len(self.repo.list_anchors()),
            "auto_anchor_enabled": False,
            "head_hash": self.ledger.chain.head.hash,
        }


__all__ = ["AnchorService"]
