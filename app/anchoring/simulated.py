"""
Offline "simulated" anchoring -- the demo never depends on the network.

This provider must be understood correctly, and the distinction is important
enough that the UI labels it explicitly on screen.

**It is not a real anchor.** No external party stores anything. But it is not
meaningless either, because it performs the same *cryptographic* operation the
real providers do, and records the same chain of custody:

1. it links each anchor to the previous one (``previous_anchor_root``), forming
   an anchor chain -- so removing or reordering an anchor is detectable;
2. it commits the Merkle root of every attendance record sealed since the last
   anchor;
3. it produces a deterministic, verifiable proof blob bound to that root;
4. it records the exact bytes that *would* have been submitted.

That means the whole local flow -- root computation, anchor transaction,
institutional signature, chain linkage, export and verification -- is fully
exercised and testable with no internet access, and switching to OpenTimestamps
or Ethereum later is a one-line configuration change
(``ANCHOR_PROVIDER=opentimestamps``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from .base import STATUS_SIMULATED, AnchorProvider, AnchorReceipt


class SimulatedProvider(AnchorProvider):
    """Deterministic local anchor; clearly marked as simulated everywhere."""

    name = "simulated"
    network = "local-test-mode"
    independent = False

    def submit(self, merkle_root: str, context: dict[str, Any]) -> AnchorReceipt:
        anchor_id = context["anchor_id"]
        previous = context.get("previous_anchor_root") or "0" * 64
        block_index = context.get("block_index")
        created_at = context.get("created_at", time.time())

        # A deterministic, HMAC-bound "receipt". It commits to the root, the
        # previous anchor and the block range, so the receipt cannot be reused
        # for a different anchor.
        message = f"{anchor_id}|{merkle_root}|{previous}|{block_index}".encode("utf-8")
        receipt = hmac.new(
            b"BCOE-ATTENDANCE-CHAIN-SIMULATED-ANCHOR", message, hashlib.sha256
        ).digest()

        return AnchorReceipt(
            anchor_id=anchor_id,
            provider=self.name,
            network=self.network,
            merkle_root=merkle_root,
            status=STATUS_SIMULATED,
            message=(
                "SIMULATED ANCHOR (offline mode). The Merkle root, block range and "
                "chain linkage are all genuine and verifiable locally, but nothing "
                "was published to an external ledger. Set ANCHOR_PROVIDER to "
                "opentimestamps or ethereum to anchor for real."
            ),
            proof=base64.b64encode(receipt).decode("ascii"),
            proof_encoding="base64",
            transaction={
                "type": "LOCAL_ANCHOR",
                "anchor_id": anchor_id,
                "merkle_root": merkle_root,
                "previous_anchor_root": previous,
                "block_index": block_index,
                "created_at": created_at,
            },
            independent=False,
            verifiable_offline=True,
            extra={
                "algorithm": "HMAC-SHA256 over anchor_id|root|previous_root|block_index",
                "receipt_hex": receipt.hex(),
                "note": "No external ledger was contacted.",
            },
        )

    def available(self) -> tuple[bool, str]:
        return True, "always available (offline)"

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update(
            {
                "cost": "free",
                "assurance": "local only -- NOT independently timestamped",
                "honest_label": (
                    "Use this when the campus network is down. Every record is "
                    "tagged SIMULATED so it can never be mistaken for a real "
                    "public-chain anchor."
                ),
            }
        )
        return data


__all__ = ["SimulatedProvider"]
