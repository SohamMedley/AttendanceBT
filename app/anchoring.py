"""
Public anchoring: publishing a Merkle root where the college cannot edit it.

The college runs the only node of its own chain, so in principle it could
re-mine history and still pass every internal check. Anchoring removes that
possibility: the 32-byte Merkle root is committed to a ledger the college
does not control, and ONLY that hash leaves the building.

Four providers are available and the UI shows exactly which one ran, so a
simulated anchor can never be mistaken for a real public one.
"""

from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import logging
import time
import urllib.error
import urllib.request

from dataclasses import dataclass, field
from typing import Any

from .blockchain.crypto import encode_bytes32, function_selector, keccak256_hex


# ============================================================================
# The provider contract
# ============================================================================
#
# Anchoring: publishing a fingerprint of the local chain to a public one.
#
# Why anchor at all?
# ------------------
# Our Proof-of-Work chain is secure *as long as nobody controls more than half the
# hashing power*. In a single-college deployment the college itself runs the only
# node, so a determined administrator could, in principle, re-mine the whole chain
# and rewrite history. Chain validation would pass, because the rewrite would be
# internally consistent.
#
# Anchoring removes that possibility. Periodically we publish the Merkle root of
# all attendance records to an independent, public, append-only ledger:
#
# * every previous record is committed to, cryptographically, in a single 32-byte
#   value;
# * that value now exists somewhere the college does **not** control;
# * so a later rewrite is provably detectable -- the public record no longer
#   matches, and it cannot be changed by anyone inside the college.
#
# This "hash the private data, publish the hash, keep the data private" pattern is
# exactly what real systems do, because student attendance must stay confidential
# (GDPR/DPDP) while still being provably unaltered.
#
# Providers
# ---------
# ``opentimestamps``  Real, free, no account, no gas. Stamps the digest into the
#                     Bitcoin blockchain's *aggregate* -- the highest-assurance
#                     option that costs nothing.
# ``ethereum``        Builds a genuine ``anchor(bytes32)`` transaction for an
#                     EVM chain (Sepolia by default). Prepared and broadcastable;
#                     submitting needs testnet ETH.
# ``simulated``       Fully offline, deterministic. Always available, so the demo
#                     never depends on the campus network. Clearly labelled as
#                     simulated in the UI and in every stored receipt.

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


# ============================================================================
# Simulated provider (offline, clearly labelled)
# ============================================================================
#
# Offline "simulated" anchoring -- the demo never depends on the network.
#
# This provider must be understood correctly, and the distinction is important
# enough that the UI labels it explicitly on screen.
#
# **It is not a real anchor.** No external party stores anything. But it is not
# meaningless either, because it performs the same *cryptographic* operation the
# real providers do, and records the same chain of custody:
#
# 1. it links each anchor to the previous one (``previous_anchor_root``), forming
#    an anchor chain -- so removing or reordering an anchor is detectable;
# 2. it commits the Merkle root of every attendance record sealed since the last
#    anchor;
# 3. it produces a deterministic, verifiable proof blob bound to that root;
# 4. it records the exact bytes that *would* have been submitted.
#
# That means the whole local flow -- root computation, anchor transaction,
# institutional signature, chain linkage, export and verification -- is fully
# exercised and testable with no internet access, and switching to OpenTimestamps
# or Ethereum later is a one-line configuration change
# (``ANCHOR_PROVIDER=opentimestamps``).

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


# ============================================================================
# OpenTimestamps (real Bitcoin anchoring)
# ============================================================================
#
# OpenTimestamps provider -- Bitcoin-backed anchoring with no account and no fees.
#
# How it works
# ------------
# OpenTimestamps aggregates millions of 32-byte digests into a single Merkle tree
# and commits *that* one root into a Bitcoin transaction. So your digest ends up
# timestamped by the Bitcoin blockchain without you paying any transaction fee or
# holding any cryptocurrency. It is, for this project, the closest thing to "free
# public blockchain notarisation" that actually exists.
#
# The protocol, reduced to what we need
# -------------------------------------
# 1. ``POST {calendar}/digest`` with the raw 32-byte digest as the body and
#    ``Accept: application/vnd.opentimestamps.v1``.
# 2. The calendar replies with a serialised pending ``Timestamp`` (a binary
#    attestation blob). We store it, base64-encoded, inside the anchor record.
# 3. Later, ``GET {calendar}/timestamp/{digest}`` returns the *upgraded*
#    timestamp containing the real Bitcoin block-height attestation. The anchor
#    moves from PENDING to SUBMITTED once a Bitcoin block confirms it (typically
#    within a few hours).
#
# Verification is independent: install ``opentimestamps-client`` and run
# ``ots verify`` against the downloaded ``.ots`` proof. The proof file can be
# exported straight from the Anchoring page of this application.
#
# Because attendance data is personal, we submit **only the Merkle root** -- a
# 256-bit number that reveals nothing about any student -- never the records
# themselves.

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


def digest_of(payload: bytes) -> str:
    """Handy helper: the SHA-256 digest used when stamping arbitrary data."""
    return hashlib.sha256(payload).hexdigest()


# ============================================================================
# Ethereum / EVM anchoring
# ============================================================================
#
# Ethereum (EVM) anchoring provider.
#
# What this does, honestly
# ------------------------
# It builds a **real, correctly-encoded Ethereum transaction** that calls
# ``anchor(bytes32 root)`` on a contract, using a genuine keccak256 function
# selector and ABI encoding. The payload can be broadcast to Sepolia as-is.
#
# By default it runs in *prepare* mode: the transaction is constructed, encoded,
# and displayed with its exact calldata, but not broadcast, because broadcasting
# needs testnet ETH in the signing account. That keeps the project runnable
# offline while still producing something a reviewer can independently verify --
# paste the calldata into Etherscan's "Input Data" decoder and it decodes to
# ``anchor(bytes32)`` with our Merkle root.
#
# Set ``ANCHOR_SUBMIT_ENABLED=true`` **and** provide ``ETHEREUM_RPC_URL`` plus
# ``ETHEREUM_PRIVATE_KEY`` to broadcast for real. When ``web3.py`` is installed it
# is used for signing; otherwise the project still generates the exact raw
# transaction fields.


#: Function signature of the anchoring contract entry point.
ANCHOR_FUNCTION = "anchor(bytes32)"
ANCHOR_EVENT = "AnchorSubmitted(bytes32,uint256,string)"

#: Minimal Solidity contract the payload targets -- printed in the UI so the
#: reviewer can see exactly what the transaction calls.
ANCHOR_CONTRACT_SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AttendanceAnchor
/// @notice Stores Merkle roots of an off-chain attendance ledger.
/// @dev Only 32 bytes per anchor are written, so no student data is on-chain.
contract AttendanceAnchor {
    event AnchorSubmitted(bytes32 indexed root, uint256 indexed timestamp, string source);

    address public owner;
    mapping(bytes32 => uint256) public anchoredAt;
    bytes32[] public roots;

    constructor() { owner = msg.sender; }

    /// @notice Commit a Merkle root of attendance records.
    function anchor(bytes32 root) external {
        require(anchoredAt[root] == 0, "root already anchored");
        anchoredAt[root] = block.timestamp;
        roots.push(root);
        emit AnchorSubmitted(root, block.timestamp, "BCOE-ATTENDANCE-CHAIN");
    }

    /// @notice Verify that a root was anchored, and when.
    function verify(bytes32 root) external view returns (bool, uint256) {
        return (anchoredAt[root] != 0, anchoredAt[root]);
    }

    function rootCount() external view returns (uint256) { return roots.length; }
}
"""


def encode_anchor_calldata(merkle_root: str) -> str:
    """ABI-encode ``anchor(bytes32)`` for a given Merkle root.

    ``calldata = selector(4 bytes) || abi.encode(bytes32)``
    """
    selector = function_selector(ANCHOR_FUNCTION)
    return "0x" + selector + encode_bytes32(merkle_root)


class EthereumProvider(AnchorProvider):
    """Prepares (and optionally broadcasts) an EVM anchoring transaction."""

    name = "ethereum"
    network = "ethereum"

    def __init__(self, config) -> None:
        super().__init__(config)
        anchoring = config.anchoring
        self.rpc_url = anchoring.ethereum_rpc_url
        self.contract = anchoring.ethereum_contract
        self.private_key = anchoring.ethereum_private_key
        self.chain_id = int(anchoring.ethereum_chain_id)
        self.submit_enabled = bool(anchoring.submit_enabled)
        self.network = {
            1: "ethereum-mainnet",
            11155111: "sepolia-testnet",
            17000: "holesky-testnet",
            137: "polygon-mainnet",
            80002: "polygon-amoy-testnet",
        }.get(self.chain_id, f"evm-chain-{self.chain_id}")

    # ------------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.contract:
            return False, "ETHEREUM_CONTRACT is not configured"
        if self.submit_enabled and not self.rpc_url:
            return False, "ETHEREUM_RPC_URL is required to broadcast"
        return True, "prepare mode" if not self.submit_enabled else "broadcast enabled"

    # ------------------------------------------------------------------
    def submit(self, merkle_root: str, context: dict[str, Any]) -> AnchorReceipt:
        anchor_id = context["anchor_id"]
        calldata = encode_anchor_calldata(merkle_root)

        base: dict[str, Any] = {
            "to": self.contract or "0x0000000000000000000000000000000000000000",
            "data": calldata,
            "value": "0x0",
            "chain_id": self.chain_id,
            "gas": "0x186a0",  # 100000 gas is comfortable for one SSTORE + event
        }

        transaction = {
            **base,
            "function": ANCHOR_FUNCTION,
            "calldata": calldata,
            "selector": function_selector(ANCHOR_FUNCTION),
            "equivalent_keccak256_of_root": keccak256_hex(bytes.fromhex(merkle_root)),
            "abi_encoding_notes": (
                "4-byte selector keccak256('anchor(bytes32)') followed by the "
                "32-byte root. Paste the calldata into Etherscan's Input Data "
                "decoder to confirm it decodes to anchor(bytes32)."
            ),
        }

        if not (self.submit_enabled and self.rpc_url):
            return AnchorReceipt(
                anchor_id=anchor_id,
                provider=self.name,
                network=self.network,
                merkle_root=merkle_root,
                status=STATUS_PREPARED,
                message=(
                    "Transaction prepared and ABI-encoded. Broadcasting is disabled "
                    "(set ANCHOR_SUBMIT_ENABLED=true with an RPC URL and a funded "
                    "key to send it). No student data is ever placed on-chain -- "
                    "only this 32-byte root."
                ),
                transaction=transaction,
                independent=bool(self.contract),
                extra={
                    "contract_source": ANCHOR_CONTRACT_SOURCE,
                    "exact_keccak256_of_root": keccak256_hex(bytes.fromhex(merkle_root)),
                    "warning": "PREPARED ONLY - not broadcast to any network",
                },
            )

        # ---- real broadcast path -------------------------------------
        try:
            tx_hash = self._broadcast(base)
            explorer = self._explorer_url(tx_hash)
            return AnchorReceipt(
                anchor_id=anchor_id,
                provider=self.name,
                network=self.network,
                merkle_root=merkle_root,
                status=STATUS_SUBMITTED,
                message=f"Anchor transaction broadcast: {tx_hash}",
                transaction={**transaction, "tx_hash": tx_hash},
                explorer_url=explorer,
                independent=True,
                extra={"contract_source": ANCHOR_CONTRACT_SOURCE},
            )
        except Exception as exc:
            return AnchorReceipt(
                anchor_id=anchor_id,
                provider=self.name,
                network=self.network,
                merkle_root=merkle_root,
                status=STATUS_FAILED,
                message=f"Broadcast failed: {exc}. The encoded transaction is stored "
                "below and can be sent manually.",
                transaction=transaction,
                independent=False,
                extra={"contract_source": ANCHOR_CONTRACT_SOURCE},
            )

    # ------------------------------------------------------------------
    def _broadcast(self, transaction: dict[str, Any]) -> str:
        """Sign and send via web3.py if available, else via raw JSON-RPC."""
        if not self.private_key:
            raise RuntimeError("ETHEREUM_PRIVATE_KEY is not set")

        try:  # pragma: no cover - only on a machine with web3 installed
            from web3 import Web3  # type: ignore

            web3 = Web3(Web3.HTTPProvider(self.rpc_url))
            account = web3.eth.account.from_key(self.private_key)
            nonce = web3.eth.get_transaction_count(account.address)
            tx = {
                "to": Web3.to_checksum_address(transaction["to"]),
                "data": transaction["data"],
                "value": 0,
                "gas": int(transaction["gas"], 16),
                "nonce": nonce,
                "chainId": self.chain_id,
                "maxFeePerGas": web3.to_wei(20, "gwei"),
                "maxPriorityFeePerGas": web3.to_wei(2, "gwei"),
            }
            signed = account.sign_transaction(tx)
            return web3.eth.send_raw_transaction(signed.raw_transaction).hex()
        except ImportError:
            raise RuntimeError(
                "web3.py is not installed, so the transaction cannot be signed. "
                "Install it with `pip install web3`, or use the OpenTimestamps "
                "provider which needs no dependencies at all."
            ) from None

    def _explorer_url(self, tx_hash: str) -> str:
        if tx_hash and not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        prefix = {
            1: "https://etherscan.io",
            11155111: "https://sepolia.etherscan.io",
            17000: "https://holesky.etherscan.io",
            137: "https://polygonscan.com",
            80002: "https://amoy.polygonscan.com",
        }.get(self.chain_id)
        return f"{prefix}/tx/{tx_hash}" if prefix else ""

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update(
            {
                "function": ANCHOR_FUNCTION,
                "event": ANCHOR_EVENT,
                "contract": self.contract or "(not configured)",
                "chain_id": self.chain_id,
                "submit_enabled": self.submit_enabled,
                "cost": "testnet gas only (Sepolia ETH is free from a faucet)",
                "privacy": "only the 32-byte Merkle root is written on-chain",
            }
        )
        return data


# ============================================================================
# Auto provider (try for real, degrade honestly)
# ============================================================================
#
# Auto provider -- try for a real anchor, degrade honestly.
#
# A college demo has to survive a flaky campus network. The rule this provider
# follows is simple:
#
# 1. **Try the real thing first.** Submit the Merkle root to the public
#    OpenTimestamps calendars.
# 2. **If that fails, do not fail the operation.** The anchor transaction is
#    already on our own chain, so the record is never lost. We fall back to the
#    simulated provider and *say so*, storing ``fallback_from`` and
#    ``fallback_reason`` in the anchor record.
#
# The distinction is never hidden: the UI shows a green "submitted" badge only for
# a genuine external anchor, and an amber "simulated" badge otherwise, with the
# exact reason the real submission did not happen. You can retry later --
# ``AnchorService.retry_pending()`` re-submits every SIMULATED/FAILED anchor whose
# root has not yet been accepted by a calendar.

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


# ============================================================================
# Provider registry
# ============================================================================
#
# Anchoring providers and the factory that selects one.

PROVIDERS = {
    "auto": AutoProvider,
    "simulated": SimulatedProvider,
    "opentimestamps": OpenTimestampsProvider,
    "ots": OpenTimestampsProvider,
    "ethereum": EthereumProvider,
    "evm": EthereumProvider,
}


def build_provider(config) -> AnchorProvider:
    """Return the provider named in the configuration.

    An unknown name (or a provider that reports itself unavailable) falls back to
    ``simulated`` so an anchor can always be produced -- every receipt carries a
    ``status``, so a fallback is never silently passed off as a real anchor.
    """
    requested = (config.anchoring.provider or "auto").strip().lower()
    provider_class = PROVIDERS.get(requested)

    if provider_class is None:
        return AutoProvider(config)

    provider = provider_class(config)
    ok, _reason = provider.available()
    if not ok and requested not in {"simulated", "auto"}:
        return SimulatedProvider(config)
    return provider


def provider_catalogue(config) -> list[dict]:
    """Describe every provider -- drives the Anchoring settings screen."""
    aliases = {
        "auto": {"auto"},
        "opentimestamps": {"opentimestamps", "ots"},
        "ethereum": {"ethereum", "evm"},
        "simulated": {"simulated"},
    }
    selected = (config.anchoring.provider or "auto").strip().lower()

    catalogue = []
    for name in ("auto", "opentimestamps", "ethereum", "simulated"):
        provider = PROVIDERS[name](config)
        info = provider.describe()
        info["selected"] = selected in aliases[name]
        catalogue.append(info)
    return catalogue
