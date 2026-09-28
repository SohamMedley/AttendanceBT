"""
OpenTimestamps provider -- Bitcoin-backed anchoring with no account and no fees.

How it works
------------
OpenTimestamps aggregates millions of 32-byte digests into a single Merkle tree
and commits *that* one root into a Bitcoin transaction. So your digest ends up
timestamped by the Bitcoin blockchain without you paying any transaction fee or
holding any cryptocurrency. It is, for this project, the closest thing to "free
public blockchain notarisation" that actually exists.

The protocol, reduced to what we need
-------------------------------------
1. ``POST {calendar}/digest`` with the raw 32-byte digest as the body and
   ``Accept: application/vnd.opentimestamps.v1``.
2. The calendar replies with a serialised pending ``Timestamp`` (a binary
   attestation blob). We store it, base64-encoded, inside the anchor record.
3. Later, ``GET {calendar}/timestamp/{digest}`` returns the *upgraded*
   timestamp containing the real Bitcoin block-height attestation. The anchor
   moves from PENDING to SUBMITTED once a Bitcoin block confirms it (typically
   within a few hours).

Verification is independent: install ``opentimestamps-client`` and run
``ots verify`` against the downloaded ``.ots`` proof. The proof file can be
exported straight from the Anchoring page of this application.

Because attendance data is personal, we submit **only the Merkle root** -- a
256-bit number that reveals nothing about any student -- never the records
themselves.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import urllib.error
import urllib.request
from typing import Any

from .base import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUBMITTED,
    AnchorProvider,
    AnchorReceipt,
)

log = logging.getLogger("bcoe.anchor.ots")

OTS_ACCEPT = "application/vnd.opentimestamps.v1"
DEFAULT_CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://alice.btc.calendar.opentimestamps.org",
)


class OpenTimestampsProvider(AnchorProvider):
    """Submits the Merkle root to public OpenTimestamps calendars."""

    name = "opentimestamps"
    network = "bitcoin (via opentimestamps calendar)"
    independent = True

    def __init__(self, config, calendars: tuple[str, ...] | None = None) -> None:
        super().__init__(config)
        self.calendars = calendars or getattr(
            config.anchoring, "calendar_urls", DEFAULT_CALENDARS
        )

    # ------------------------------------------------------------------
    def submit(self, merkle_root: str, context: dict[str, Any]) -> AnchorReceipt:
        anchor_id = context["anchor_id"]
        digest = bytes.fromhex(merkle_root)
        if len(digest) != 32:
            return AnchorReceipt(
                anchor_id=anchor_id,
                provider=self.name,
                network=self.network,
                merkle_root=merkle_root,
                status=STATUS_FAILED,
                message="Merkle root must be 32 bytes",
                independent=self.independent,
            )

        failures: list[str] = []
        for calendar in self.calendars:
            try:
                body, status = self._post_digest(calendar, digest)
                if body:
                    return AnchorReceipt(
                        anchor_id=anchor_id,
                        provider=self.name,
                        network=self.network,
                        merkle_root=merkle_root,
                        status=STATUS_PENDING,
                        message=(
                            "Digest accepted by the OpenTimestamps calendar "
                            f"({calendar}). It will be committed to Bitcoin within "
                            "a few hours; the proof is stored below and can be "
                            "upgraded with `ots upgrade`."
                        ),
                        proof=base64.b64encode(body).decode("ascii"),
                        proof_encoding="base64",
                        explorer_url=f"{calendar}/timestamp/{merkle_root}",
                        independent=True,
                        extra={
                            "calendar": calendar,
                            "http_status": status,
                            "digest_hex": merkle_root,
                            "verify_command": (
                                "ots verify data.ots   # after: ots upgrade data.ots"
                            ),
                        },
                    )
                failures.append(f"{calendar}: empty response")
            except urllib.error.HTTPError as exc:
                failures.append(f"{calendar}: HTTP {exc.code}")
            except Exception as exc:  # network unreachable, DNS, TLS, timeout
                failures.append(f"{calendar}: {type(exc).__name__} {exc}")

        return AnchorReceipt(
            anchor_id=anchor_id,
            provider=self.name,
            network=self.network,
            merkle_root=merkle_root,
            status=STATUS_FAILED,
            message=(
                "Could not reach any OpenTimestamps calendar. The Merkle root is "
                "recorded locally and can be submitted later. Details: "
                + "; ".join(failures[:3])
            ),
            independent=False,
            extra={"attempts": failures, "digest_hex": merkle_root},
        )

    # ------------------------------------------------------------------
    def _post_digest(self, calendar: str, digest: bytes) -> tuple[bytes, int]:
        """POST the raw digest and return the calendar's timestamp blob."""
        url = calendar.rstrip("/") + "/digest"
        request = urllib.request.Request(
            url,
            data=digest,
            headers={
                "Accept": OTS_ACCEPT,
                "Content-Type": "application/octet-stream",
                "User-Agent": "BCOE-AttendanceChain/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.read(), response.status

    def upgrade(self, merkle_root: str, proof_b64: str) -> dict[str, Any]:
        """Query calendars for the Bitcoin-confirmed version of a pending proof.

        Returns a dict describing whether an upgrade was found. Call this from
        the Anchoring page a few hours after submission.
        """
        digest = bytes.fromhex(merkle_root)
        for calendar in self.calendars:
            try:
                url = f"{calendar.rstrip('/')}/timestamp/{merkle_root}"
                request = urllib.request.Request(
                    url, headers={"Accept": OTS_ACCEPT}, method="GET"
                )
                with urllib.request.urlopen(request, timeout=25) as response:
                    body = response.read()
                if body and body != base64.b64decode(proof_b64):
                    return {
                        "upgraded": True,
                        "calendar": calendar,
                        "proof": base64.b64encode(body).decode("ascii"),
                        "size": len(body),
                        "message": (
                            "An upgraded (Bitcoin-attested) proof is available. Run "
                            "`ots upgrade` on the exported .ots file, or verify "
                            "directly with `ots verify`."
                        ),
                    }
            except Exception as exc:
                log.debug("OTS upgrade failed for %s: %s", calendar, exc)
        return {
            "upgraded": False,
            "message": (
                "No Bitcoin attestation yet. OpenTimestamps calendars usually bundle "
                "submissions into a Bitcoin block within a few hours."
            ),
        }

    def available(self) -> tuple[bool, str]:
        return True, "requires outbound HTTPS to public calendars"

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update(
            {
                "cost": "free (no account, no gas, no crypto)",
                "assurance": "bitcoin proof-of-work",
                "calendars": list(self.calendars),
                "privacy": "only the 32-byte Merkle root leaves the server",
            }
        )
        return data


__all__ = ["OpenTimestampsProvider"]


def digest_of(payload: bytes) -> str:
    """Handy helper: the SHA-256 digest used when stamping arbitrary data."""
    return hashlib.sha256(payload).hexdigest()
