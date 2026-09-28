"""
Minimal, dependency-free RS256 JWT signer.

Firestore's REST API needs an OAuth 2.0 access token. Google expects a signed
JWT "assertion" (the standard two-legged service-account flow). The official
``google-auth`` library does this, but we also ship this ~150-line implementation
so the project can talk to Firestore with *nothing installed* beyond the Python
standard library -- which matters when you have to demo on a college lab machine.

Only what Firestore needs is implemented:
  * DER/PKCS#8 parsing of the service-account private key (RSA)
  * RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017)
  * Compact JWS serialisation (JWT = base64url(header).base64url(claims).sig)

If ``firebase-admin`` or ``google-auth`` is installed, we prefer those -- see
``firestore_store.py``. This module is the fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

# ASN.1 DER DigestInfo prefix for SHA-256 (RFC 8017, section 9.2, notes)
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class JWTError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tiny ASN.1 DER reader
# --------------------------------------------------------------------------
def _der_read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one TLV triple. Returns (tag, value_bytes, next_offset)."""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        num_bytes = length & 0x7F
        length = int.from_bytes(data[offset : offset + num_bytes], "big")
        offset += num_bytes
    value = data[offset : offset + length]
    return tag, value, offset + length


def _der_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def parse_pkcs8_rsa_private_key(pem: str) -> tuple[int, int]:
    """Extract ``(n, d)`` -- the RSA modulus and private exponent -- from a PKCS#8 PEM.

    Structure we walk::

        PrivateKeyInfo ::= SEQUENCE {
            version Integer,
            privateKeyAlgorithm AlgorithmIdentifier,
            privateKey OCTET STRING   -- contains RSAPrivateKey
        }
        RSAPrivateKey ::= SEQUENCE {
            version, modulus(n), publicExponent(e), privateExponent(d), ...
        }

    We only need ``n`` and ``d`` to sign.
    """
    body = "".join(
        line.strip()
        for line in pem.splitlines()
        if "-----" not in line and line.strip()
    )
    der = base64.b64decode(body)

    tag, seq, _ = _der_read(der, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE at the top of the key")

    offset = 0
    _, _version, offset = _der_read(seq, offset)
    _, _algorithm, offset = _der_read(seq, offset)
    tag, octet, _ = _der_read(seq, offset)
    if tag != 0x04:
        raise JWTError("Expected an OCTET STRING wrapping the RSA key")

    offset = 0
    tag, rsa_seq, _ = _der_read(octet, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE inside the private key")

    offset = 0
    _, _v, offset = _der_read(rsa_seq, offset)          # version
    _, n_bytes, offset = _der_read(rsa_seq, offset)     # modulus
    _, _e_bytes, offset = _der_read(rsa_seq, offset)    # public exponent
    _, d_bytes, offset = _der_read(rsa_seq, offset)     # private exponent

    return _der_int(n_bytes), _der_int(d_bytes)


# --------------------------------------------------------------------------
# RSASSA-PKCS1-v1_5 signing
# --------------------------------------------------------------------------
def _pkcs1_v15_pad(message: bytes, key_length: int) -> bytes:
    """EMSA-PKCS1-v1_5 encoding: 0x00 || 0x01 || PS(0xFF...) || 0x00 || DigestInfo."""
    padding_length = key_length - len(message) - 3
    if padding_length < 8:
        raise JWTError("RSA key is too small for a SHA-256 signature")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + message


def rsa_sha256_sign(message: bytes, n: int, d: int) -> bytes:
    """Sign with RSA: signature = EM^d mod n."""
    key_length = (n.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    encoded = _pkcs1_v15_pad(digest_info, key_length)
    signature = pow(int.from_bytes(encoded, "big"), d, n)
    return signature.to_bytes(key_length, "big")


def rsa_sha256_verify(message: bytes, signature: bytes, n: int, e: int = 65537) -> bool:
    """Verify a signature. Used by our own self-test so the maths is proven."""
    key_length = (n.bit_length() + 7) // 8
    if len(signature) != key_length:
        return False
    recovered = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_length, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    return recovered == _pkcs1_v15_pad(digest_info, key_length)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_signed_jwt(
    *,
    client_email: str,
    private_key_pem: str,
    private_key_id: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
    scope: str = "https://www.googleapis.com/auth/datastore",
    lifetime: int = 3600,
) -> str:
    """Build a Google service-account JWT assertion."""
    n, d = parse_pkcs8_rsa_private_key(private_key_pem)

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": private_key_id}
    claims = {
        "iss": client_email,
        "scope": scope,
        "aud": token_uri,
        "iat": now,
        "exp": now + lifetime,
    }

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")

    signature = rsa_sha256_sign(signing_input, n, d)
    return signing_input.decode("ascii") + "." + _b64url(signature)


def exchange_jwt_for_token(
    jwt: str, token_uri: str = "https://oauth2.googleapis.com/token", timeout: int = 30
) -> dict[str, Any]:
    """Trade the JWT assertion for an OAuth 2.0 access token."""
    payload = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        token_uri,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.read().decode("utf-8", errors="replace")
        raise JWTError(f"Google token endpoint rejected the assertion: {detail}") from exc
    except Exception as exc:  # pragma: no cover - network dependent
        raise JWTError(f"Could not reach the Google token endpoint: {exc}") from exc


def service_account_access_token(credentials: dict[str, Any]) -> tuple[str, float]:
    """Return ``(access_token, expiry_epoch)`` for a service-account dict."""
    jwt = build_signed_jwt(
        client_email=credentials["client_email"],
        private_key_pem=credentials["private_key"],
        private_key_id=credentials.get("private_key_id", ""),
        token_uri=credentials.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    response = exchange_jwt_for_token(
        jwt, credentials.get("token_uri", "https://oauth2.googleapis.com/token")
    )
    if "access_token" not in response:
        raise JWTError(f"No access_token in the token response: {response}")
    expires_in = float(response.get("expires_in", 3600))
    return response["access_token"], time.time() + expires_in


def load_service_account() -> dict[str, Any] | None:
    """Locate service-account credentials.

    Order of precedence:
      1. ``FIREBASE_SERVICE_ACCOUNT``  -- the JSON itself (good for hosting env vars)
      2. ``FIREBASE_SERVICE_ACCOUNT_PATH`` -- path to the downloaded JSON file
      3. ``./firebase-service-account.json`` or ``./secrets/firebase-service-account.json``
      4. ``FIREBASE_CREDENTIALS`` (path; the variable firebase-admin itself reads)
    """
    inline = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise JWTError("FIREBASE_SERVICE_ACCOUNT is not valid JSON") from exc

    candidates: list[str] = []
    for variable in ("FIREBASE_SERVICE_ACCOUNT_PATH", "FIREBASE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)
    candidates += [
        "firebase-service-account.json",
        "secrets/firebase-service-account.json",
        "data/firebase-service-account.json",
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as handle:
                return json.load(handle)
    return None


__all__ = [
    "JWTError",
    "build_signed_jwt",
    "exchange_jwt_for_token",
    "service_account_access_token",
    "load_service_account",
    "parse_pkcs8_rsa_private_key",
    "rsa_sha256_sign",
    "rsa_sha256_verify",
]
