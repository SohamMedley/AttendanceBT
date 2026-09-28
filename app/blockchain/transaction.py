"""
Transaction model for the attendance ledger.

Two transaction types travel on this chain:

``ATTENDANCE``
    One student's presence for one lecture session. Signed with the *student's*
    own secp256k1 private key, so a student cannot repudiate their own record and
    nobody else can forge one.

``ANCHOR``
    A periodic "checkpoint" transaction whose payload is the Merkle root of the
    attendance transactions since the previous anchor. Anchor transactions are
    signed with the *institution's* key and are the hand-off point to a public
    blockchain (see ``app/anchoring``). This is the hybrid anchoring pattern used
    by real-world supply-chain and academic-credential systems.

Transaction ids are content-addressed: ``tx_id = SHA256(canonical_json(payload))``.
Because the id is derived from the content, altering any field after the fact
produces a different id, and the block's Merkle root no longer matches.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import ecdsa

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
        self.signature = ecdsa.sign(private_key, self.signing_bytes())
        return self

    def verify_signature(self) -> bool:
        """True when the signature matches both the content and the sender key."""
        if not self.signature or not self.sender_pubkey:
            return False
        return ecdsa.verify(self.sender_pubkey, self.signing_bytes(), self.signature)

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


__all__ = [
    "Transaction",
    "TX_ATTENDANCE",
    "TX_ANCHOR",
    "STATUS_PRESENT",
    "STATUS_LATE",
    "STATUS_ABSENT",
    "STATUS_MANUAL",
    "VALID_STATUSES",
    "canonical_json",
    "sha256_hex",
    "build_attendance_transaction",
    "build_anchor_transaction",
]
