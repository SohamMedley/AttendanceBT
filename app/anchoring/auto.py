"""
Auto provider -- try for a real anchor, degrade honestly.

A college demo has to survive a flaky campus network. The rule this provider
follows is simple:

1. **Try the real thing first.** Submit the Merkle root to the public
   OpenTimestamps calendars.
2. **If that fails, do not fail the operation.** The anchor transaction is
   already on our own chain, so the record is never lost. We fall back to the
   simulated provider and *say so*, storing ``fallback_from`` and
   ``fallback_reason`` in the anchor record.

The distinction is never hidden: the UI shows a green "submitted" badge only for
a genuine external anchor, and an amber "simulated" badge otherwise, with the
exact reason the real submission did not happen. You can retry later --
``AnchorService.retry_pending()`` re-submits every SIMULATED/FAILED anchor whose
root has not yet been accepted by a calendar.
"""

from __future__ import annotations

from typing import Any

from .base import STATUS_FAILED, STATUS_SIMULATED, AnchorProvider, AnchorReceipt
from .opentimestamps import OpenTimestampsProvider
from .simulated import SimulatedProvider


class AutoProvider(AnchorProvider):
    """OpenTimestamps when reachable, simulated when not."""

    name = "auto"
    network = "bitcoin (opentimestamps) with offline fallback"
    independent = False  # only true when the real submission succeeds

    def __init__(self, config) -> None:
        super().__init__(config)
        self.primary = OpenTimestampsProvider(config)
        self.fallback = SimulatedProvider(config)

    def submit(self, merkle_root: str, context: dict[str, Any]) -> AnchorReceipt:
        receipt = self.primary.submit(merkle_root, context)

        if receipt.status in {STATUS_FAILED} or not receipt.independent:
            # The real calendar was unreachable -- keep the operation useful.
            simulated = self.fallback.submit(merkle_root, context)
            simulated.extra.update(
                {
                    "fallback_from": self.primary.name,
                    "fallback_reason": receipt.message,
                    "retry_hint": (
                        "Re-run `python manage.py anchor` when the network is back, "
                        "or POST /api/anchors/<id>/retry."
                    ),
                }
            )
            simulated.message = (
                "SIMULATED ANCHOR — the OpenTimestamps calendars could not be reached, "
                "so nothing was published externally. The Merkle root, block range and "
                "anchor chain linkage are genuine and verifiable locally. "
                "Use the Retry button when the network is available."
            )
            return simulated

        receipt.extra["providers_tried"] = [self.primary.name]
        return receipt

    def available(self) -> tuple[bool, str]:
        ok, reason = self.primary.available()
        return True, f"{reason} (falls back to simulated if unreachable)" if ok else reason

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update(
            {
                "provider": "auto",
                "network": self.network,
                "cost": "free",
                "assurance": "Bitcoin proof-of-work when online, local-only otherwise",
                "honest_label": (
                    "The default. Tries a real public anchor, and if the network is "
                    "down it records a clearly-labelled simulated anchor instead so "
                    "no data or work is lost."
                ),
            }
        )
        return data


__all__ = ["AutoProvider"]
