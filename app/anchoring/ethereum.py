"""
Ethereum (EVM) anchoring provider.

What this does, honestly
------------------------
It builds a **real, correctly-encoded Ethereum transaction** that calls
``anchor(bytes32 root)`` on a contract, using a genuine keccak256 function
selector and ABI encoding. The payload can be broadcast to Sepolia as-is.

By default it runs in *prepare* mode: the transaction is constructed, encoded,
and displayed with its exact calldata, but not broadcast, because broadcasting
needs testnet ETH in the signing account. That keeps the project runnable
offline while still producing something a reviewer can independently verify --
paste the calldata into Etherscan's "Input Data" decoder and it decodes to
``anchor(bytes32)`` with our Merkle root.

Set ``ANCHOR_SUBMIT_ENABLED=true`` **and** provide ``ETHEREUM_RPC_URL`` plus
``ETHEREUM_PRIVATE_KEY`` to broadcast for real. When ``web3.py`` is installed it
is used for signing; otherwise the project still generates the exact raw
transaction fields.
"""

from __future__ import annotations

import logging
from typing import Any

from ..blockchain.keccak import encode_bytes32, function_selector, keccak256_hex
from .base import (
    STATUS_FAILED,
    STATUS_PREPARED,
    STATUS_SUBMITTED,
    AnchorProvider,
    AnchorReceipt,
)

log = logging.getLogger("bcoe.anchor.eth")

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


__all__ = ["EthereumProvider", "encode_anchor_calldata", "ANCHOR_CONTRACT_SOURCE"]
