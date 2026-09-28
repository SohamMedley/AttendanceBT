"""
Anchoring: publishing a fingerprint of the local chain to a public one.

Why anchor at all?
------------------
Our Proof-of-Work chain is secure *as long as nobody controls more than half the
hashing power*. In a single-college deployment the college itself runs the only
node, so a determined administrator could, in principle, re-mine the whole chain
and rewrite history. Chain validation would pass, because the rewrite would be
internally consistent.

Anchoring removes that possibility. Periodically we publish the Merkle root of
all attendance records to an independent, public, append-only ledger:

* every previous record is committed to, cryptographically, in a single 32-byte
  value;
* that value now exists somewhere the college does **not** control;
* so a later rewrite is provably detectable -- the public record no longer
  matches, and it cannot be changed by anyone inside the college.

This "hash the private data, publish the hash, keep the data private" pattern is
exactly what real systems do, because student attendance must stay confidential
(GDPR/DPDP) while still being provably unaltered.

Providers
---------
``opentimestamps``  Real, free, no account, no gas. Stamps the digest into the
                    Bitcoin blockchain's *aggregate* -- the highest-assurance
                    option that costs nothing.
``ethereum``        Builds a genuine ``anchor(bytes32)`` transaction for an
                    EVM chain (Sepolia by default). Prepared and broadcastable;
                    submitting needs testnet ETH.
``simulated``       Fully offline, deterministic. Always available, so the demo
                    never depends on the campus network. Clearly labelled as
                    simulated in the UI and in every stored receipt.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

STATUS_SUBMITTED = "SUBMITTED"
STATUS_PENDING = "PENDING"
STATUS_SIMULATED = "SIMULATED"
STATUS_PREPARED = "PREPARED"
STATUS_FAILED = "FAILED"


@dataclass
class AnchorReceipt:
    """The outcome of trying to anchor a Merkle root."""

    anchor_id: str
    provider: str
    network: str
    merkle_root: str
    status: str
    message: str
    created_at: float = field(default_factory=time.time)
    proof: str = ""                      # base64 provider receipt, if any
    proof_encoding: str = ""
    transaction: dict[str, Any] = field(default_factory=dict)
    explorer_url: str = ""
    independent: bool = False            # is this outside the college's control?
    verifiable_offline: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "provider": self.provider,
            "network": self.network,
            "merkle_root": self.merkle_root,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
            "proof": self.proof,
            "proof_encoding": self.proof_encoding,
            "transaction": self.transaction,
            "explorer_url": self.explorer_url,
            "independent": self.independent,
            "verifiable_offline": self.verifiable_offline,
            "extra": self.extra,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Same, but with the (possibly long) proof blob elided for lists."""
        data = self.to_dict()
        if data["proof"]:
            data["proof_preview"] = data["proof"][:64] + "..."
            data["proof_length"] = len(self.proof)
        data.pop("proof", None)
        return data


class AnchorProvider(abc.ABC):
    """Interface every anchoring backend implements."""

    name: str = "base"
    network: str = "unknown"
    #: True when the anchor lands somewhere the institute does not control.
    independent: bool = False

    def __init__(self, config) -> None:
        self.config = config

    @abc.abstractmethod
    def submit(self, merkle_root: str, context: dict[str, Any]) -> AnchorReceipt:
        """Publish ``merkle_root`` and return a receipt. Must never raise."""

    def available(self) -> tuple[bool, str]:
        """Can this provider be used right now? ``(ok, reason)``."""
        return True, "ok"

    def describe(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {
            "provider": self.name,
            "network": self.network,
            "available": ok,
            "reason": reason,
            "independent": self.independent,
        }


__all__ = [
    "AnchorProvider",
    "AnchorReceipt",
    "STATUS_SUBMITTED",
    "STATUS_PENDING",
    "STATUS_SIMULATED",
    "STATUS_PREPARED",
    "STATUS_FAILED",
]
