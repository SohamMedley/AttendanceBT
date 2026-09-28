"""
Key management: who owns which secp256k1 key pair.

Threat model (state this plainly in the report and the viva)
-----------------------------------------------------------
Every attendance transaction is signed with a **secp256k1 key pair**. The
project ships in "custodial" mode: the server generates and holds a key pair per
student in a local keystore so the system works on a college laptop with no
wallet app installed.

That is a deliberate, documented trade-off:

* **What it does prove** -- after the fact, *nobody* (not the student, not the
  faculty member, not the developer editing the database) can silently change a
  record, because the record's signature and Merkle root would stop matching.
  Tamper-*evidence* is fully preserved.
* **What it does not prove** -- that the *student personally* pressed the button,
  because the server holds their key. Non-repudiation against the student
  themselves therefore requires the non-custodial mode.

``MODE_NON_CUSTODIAL`` is implemented for the flow that matters: the student's
browser generates the key pair locally with the Web Crypto API (or our JS
secp256k1), keeps the private key in ``localStorage``, and sends only a signed
transaction. The server then verifies the signature and never sees the key. In
that mode the system gives true non-repudiation.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..blockchain import ecdsa

log = logging.getLogger("bcoe.identity")

MODE_CUSTODIAL = "custodial"
MODE_NON_CUSTODIAL = "non_custodial"

DEFAULT_KEYSTORE = "data/keystore/keys.json"


@dataclass
class Identity:
    """A key pair plus its derived public address."""

    owner_id: str
    role: str                    # "student" | "faculty" | "institution"
    private_key: str             # 64 hex chars
    public_key: str              # 66 hex chars (compressed SEC1)
    address: str                 # Base58Check address

    def to_public_dict(self) -> dict[str, Any]:
        """Never leak the private key to the API."""
        return {
            "owner_id": self.owner_id,
            "role": self.role,
            "public_key": self.public_key,
            "address": self.address,
        }


class KeyStore:
    """A JSON-file keystore for server-held (custodial) identities."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_KEYSTORE) -> None:
        self.path = Path(path)
        self._identities: dict[str, Identity] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        if self.path.is_file():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                for owner_id, record in raw.get("identities", {}).items():
                    self._identities[owner_id] = Identity(
                        owner_id=owner_id,
                        role=record.get("role", "student"),
                        private_key=record["private_key"],
                        public_key=record["public_key"],
                        address=record.get("address", ""),
                    )
            except (json.JSONDecodeError, KeyError) as exc:
                log.error("Keystore %s is unreadable (%s); starting empty", self.path, exc)
        self._loaded = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "warning": (
                "PRIVATE KEYS - development keystore. Never commit this file."
            ),
            "mode": MODE_CUSTODIAL,
            "identities": {
                owner_id: {
                    "role": identity.role,
                    "private_key": identity.private_key,
                    "public_key": identity.public_key,
                    "address": identity.address,
                }
                for owner_id, identity in self._identities.items()
            },
        }
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        os.replace(temporary, self.path)
        # Owner read/write only -- private keys must not be world-readable.
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - platform dependent (Windows)
            pass

    # ------------------------------------------------------------------
    def create(self, owner_id: str, role: str = "student") -> Identity:
        """Generate (or return an existing) identity for ``owner_id``."""
        self.load()
        existing = self._identities.get(owner_id)
        if existing is not None:
            return existing
        keypair = ecdsa.generate_keypair()
        identity = Identity(
            owner_id=owner_id,
            role=role,
            private_key=keypair.private_hex,
            public_key=keypair.public_hex,
            address=keypair.address,
        )
        self._identities[owner_id] = identity
        return identity

    def create_many(self, owner_ids: list[str], role: str = "student") -> list[Identity]:
        created = [self.create(owner_id, role) for owner_id in owner_ids]
        self.save()
        return created

    def get(self, owner_id: str) -> Identity | None:
        self.load()
        return self._identities.get(owner_id)

    def require(self, owner_id: str) -> Identity:
        identity = self.get(owner_id)
        if identity is None:
            raise KeyError(f"No key pair registered for {owner_id!r}")
        return identity

    def __len__(self) -> int:
        self.load()
        return len(self._identities)

    def all(self) -> list[Identity]:
        self.load()
        return list(self._identities.values())

    # ------------------------------------------------------------------
    def verify_ownership(self, owner_id: str, public_key: str, message: bytes, signature: str) -> bool:
        """Verify that ``signature`` over ``message`` belongs to this owner's key.

        Used in non-custodial mode: the browser signs, we check it against the
        public key the identity is registered with.
        """
        identity = self.get(owner_id)
        if identity is None or identity.public_key != public_key:
            return False
        return ecdsa.verify(public_key, message, signature)

    def rotate(self, owner_id: str) -> Identity:
        """Replace an identity's key pair (e.g. after a device change)."""
        self.load()
        keypair = ecdsa.generate_keypair()
        identity = Identity(
            owner_id=owner_id,
            role=self._identities.get(owner_id, Identity(owner_id, "student", "", "", "")).role,
            private_key=keypair.private_hex,
            public_key=keypair.public_hex,
            address=keypair.address,
        )
        self._identities[owner_id] = identity
        self.save()
        return identity


# --------------------------------------------------------------------------
# Institutional signing identity -- signs anchor transactions
# --------------------------------------------------------------------------
INSTITUTION_OWNER_ID = "BCOE-INSTITUTION"


def get_key_store(path: str | os.PathLike[str] = DEFAULT_KEYSTORE) -> KeyStore:
    return KeyStore(path)


def ensure_institution_identity(store: KeyStore) -> Identity:
    """The college's own signing identity, used for anchor transactions."""
    identity = store.get(INSTITUTION_OWNER_ID)
    if identity is None:
        identity = store.create(INSTITUTION_OWNER_ID, role="institution")
        store.save()
    return identity


def generate_nonce(length: int = 16) -> str:
    return secrets.token_hex(length // 2)


__all__ = [
    "Identity",
    "KeyStore",
    "MODE_CUSTODIAL",
    "MODE_NON_CUSTODIAL",
    "INSTITUTION_OWNER_ID",
    "get_key_store",
    "ensure_institution_identity",
    "generate_nonce",
]
