"""
Pure-Python ECDSA over the secp256k1 curve (the same curve Bitcoin and Ethereum use).

Why implement this instead of importing a library?
--------------------------------------------------
1. Zero external dependencies -> the project runs with nothing but the Python
   standard library.
2. Every line is explainable in a viva. There is no "magic" inside.
3. It demonstrates the actual mathematics behind blockchain digital signatures:
   elliptic-curve point addition, scalar multiplication, the discrete-log
   problem, and the ECDSA signing/verification equations.

Security notes
--------------
* ``k`` (the per-signature ephemeral nonce) is generated deterministically using
  RFC 6979. Reusing or leaking ``k`` leaks the private key, which is exactly how
  the Sony PS3 master key was broken in 2010. RFC 6979 makes that impossible.
* Signatures are normalised to "low-S" form (canonical signatures), which is
  what BIP-62 requires on Bitcoin and what prevents signature malleability.

Curve parameters (SEC 2 / Standards for Efficient Cryptography Group)
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

# --------------------------------------------------------------------------
# secp256k1 domain parameters
# --------------------------------------------------------------------------
# y^2 = x^3 + a*x + b  (mod p), where a = 0 and b = 7 for this curve
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
A = 0
B = 7

# Generator point G
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (Gx, Gy)

# Infinity is represented as None (the point at infinity is the group identity)
Point = "tuple[int, int] | None"


class ECDSAError(ValueError):
    """Raised for malformed keys, signatures, or curve points."""


# --------------------------------------------------------------------------
# Elliptic-curve arithmetic
# --------------------------------------------------------------------------
def inverse_mod(value: int, modulus: int) -> int:
    """Modular multiplicative inverse using the extended Euclidean algorithm.

    pow(value, -1, modulus) would also work on Python 3.8+, but implementing it
    explicitly is more instructive and lets us explain the maths on demand.
    """
    if value == 0:
        raise ZeroDivisionError("No modular inverse for zero")
    lm, hm = 1, 0
    low, high = value % modulus, modulus
    while low > 1:
        ratio = high // low
        nm, new = hm - lm * ratio, high - low * ratio
        lm, low, hm, high = nm, new, lm, low
    return lm % modulus


def is_on_curve(point: Point) -> bool:
    """Check y^2 == x^3 + 7 (mod p). The point at infinity is always valid."""
    if point is None:
        return True
    x, y = point
    return (y * y - (x * x * x + A * x + B)) % P == 0


def point_neg(point: Point) -> Point:
    """Return -P, the reflection of P across the x-axis."""
    if point is None:
        return None
    x, y = point
    return x, (-y) % P


def point_add(p1: Point, p2: Point) -> Point:
    """Add two points on the curve using the chord-and-tangent rule."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1

    x1, y1 = p1
    x2, y2 = p2

    if x1 == x2 and (y1 + y2) % P == 0:
        # P + (-P) = point at infinity
        return None

    if p1 == p2:
        # Point doubling: slope = (3x^2 + a) / 2y
        if y1 == 0:
            return None
        lam = (3 * x1 * x1 + A) * inverse_mod(2 * y1, P) % P
    else:
        # Point addition: slope = (y2 - y1) / (x2 - x1)
        lam = (y2 - y1) * inverse_mod(x2 - x1, P) % P

    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return x3, y3


def scalar_mult(k: int, point: Point) -> Point:
    """Multiply a point by a scalar using the double-and-add algorithm.

    The security of ECDSA rests on the fact that recovering ``k`` from ``k*G``
    (the "elliptic curve discrete logarithm problem") is computationally
    infeasible. This is O(log k) point operations -- fast, but its inverse is not.
    """
    if point is None:
        return None
    if k % N == 0 or point is None:
        return None
    if k < 0:
        return scalar_mult(-k, point_neg(point))

    result = None
    addend = point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def _decompress_point(x: int, odd: bool) -> tuple[int, int]:
    """Recover y from x, using the compressed SEC1 form (prefix 02/03).

    Because p == 3 (mod 4) we can take a shortcut: the square root of a value v
    modulo p is v^((p+1)/4) when v is a quadratic residue.
    """
    if x >= P:
        raise ECDSAError("x coordinate out of range")
    alpha = (pow(x, 3, P) + A * x + B) % P
    beta = pow(alpha, (P + 1) // 4, P)
    if (beta * beta) % P != alpha:
        raise ECDSAError("Point is not on the secp256k1 curve")
    y = beta if (beta % 2 == 1) == odd else P - beta
    return x, y


# --------------------------------------------------------------------------
# Key pairs
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class KeyPair:
    """A secp256k1 key pair with SEC1-compatible hex serialisation."""

    private_key: int
    public_key: Point

    # -- serialisation -----------------------------------------------------
    @property
    def private_hex(self) -> str:
        return f"{self.private_key:064x}"

    @property
    def public_hex(self) -> str:
        """Compressed public key: 33 bytes (02/03 prefix + x coordinate)."""
        x, y = self.public_key  # type: ignore[misc]
        prefix = "03" if y & 1 else "02"
        return prefix + f"{x:064x}"

    @property
    def public_uncompressed_hex(self) -> str:
        x, y = self.public_key  # type: ignore[misc]
        return "04" + f"{x:064x}{y:064x}"

    @property
    def address(self) -> str:
        """A Bitcoin-style address: Base58Check(RIPEMD160(SHA256(pubkey)))."""
        return public_key_to_address(self.public_hex)


def generate_keypair() -> KeyPair:
    """Generate a fresh key pair using the OS cryptographic random source."""
    while True:
        private_key = int.from_bytes(os.urandom(32), "big")
        if 1 <= private_key < N:
            break
    return keypair_from_private(private_key)


def keypair_from_private(private_key: int | str) -> KeyPair:
    """Rebuild a key pair from a private key (int or 64-char hex string)."""
    if isinstance(private_key, str):
        private_key = int(private_key, 16)
    if not 1 <= private_key < N:
        raise ECDSAError("Private key must be in the range [1, n-1]")
    public_key = scalar_mult(private_key, G)
    if public_key is None:
        raise ECDSAError("Invalid private key produced the point at infinity")
    return KeyPair(private_key=private_key, public_key=public_key)


def parse_public_key(public_hex: str) -> Point:
    """Parse a compressed (02/03) or uncompressed (04) SEC1 public key."""
    public_hex = public_hex.strip().lower()
    if public_hex.startswith("04"):
        x = int(public_hex[2:66], 16)
        y = int(public_hex[66:130], 16)
        point = (x, y)
        if not is_on_curve(point):
            raise ECDSAError("Public key is not a valid curve point")
        return point
    if public_hex.startswith(("02", "03")):
        x = int(public_hex[2:], 16)
        return _decompress_point(x, odd=public_hex.startswith("03"))
    raise ECDSAError("Unsupported public key encoding")


# --------------------------------------------------------------------------
# Signing / verification
# --------------------------------------------------------------------------
def _bits2int(data: bytes) -> int:
    """RFC 6979 bits2int conversion."""
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - N.bit_length()
    return value >> excess if excess > 0 else value


def _int2octets(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _deterministic_k(private_key: int, digest: bytes) -> int:
    """RFC 6979 deterministic nonce generation (HMAC-SHA256 based).

    HMAC_DRBG-style construction. Producing the same ``k`` for the same
    (key, message) pair makes signatures reproducible and removes any reliance
    on the quality of a random number generator.
    """
    hlen = hashlib.sha256().digest_size
    x_octets = _int2octets(private_key)
    h_octets = _int2octets(_bits2int(digest) % N)

    v = b"\x01" * hlen
    k = b"\x00" * hlen
    k = hmac.new(k, v + b"\x00" + x_octets + h_octets, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x_octets + h_octets, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()

    while True:
        candidate = b""
        while len(candidate) < 32:
            v = hmac.new(k, v, hashlib.sha256).digest()
            candidate += v
        secret = _bits2int(candidate)
        if 1 <= secret < N:
            return secret
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(private_key: int | str, message: bytes) -> str:
    """Sign ``message`` and return the signature as 128 hex chars (r||s).

    The message is hashed with SHA-256 first, then the ECDSA equations are
    applied::

        r = (k * G).x mod n
        s = k^-1 * (z + r * d) mod n
    """
    if isinstance(private_key, str):
        private_key = int(private_key, 16)
    if not 1 <= private_key < N:
        raise ECDSAError("Private key out of range")

    digest = hashlib.sha256(message).digest()
    z = _bits2int(digest)

    for attempt in range(16):
        k = _deterministic_k(private_key, digest + bytes([attempt]))
        point = scalar_mult(k, G)
        if point is None:
            continue
        r = point[0] % N
        if r == 0:
            continue
        s = (inverse_mod(k, N) * (z + r * private_key)) % N
        if s == 0:
            continue
        # Enforce low-S (canonical) signatures.
        if s > N // 2:
            s = N - s
        return f"{r:064x}{s:064x}"
    raise ECDSAError("Unable to produce a signature (extremely unlikely)")


def verify(public_key: str | Point, message: bytes, signature: str) -> bool:
    """Verify a 128-hex-char signature against a message and public key.

    The check is::

        w  = s^-1 mod n
        u1 = z*w mod n ,  u2 = r*w mod n
        verify that (u1*G + u2*Q).x mod n == r
    """
    try:
        if len(signature) != 128:
            return False
        r = int(signature[:64], 16)
        s = int(signature[64:], 16)
        if not (1 <= r < N and 1 <= s < N):
            return False

        point = parse_public_key(public_key) if isinstance(public_key, str) else public_key
        if point is None or not is_on_curve(point):
            return False

        digest = hashlib.sha256(message).digest()
        z = _bits2int(digest)

        w = inverse_mod(s, N)
        u1 = (z * w) % N
        u2 = (r * w) % N

        candidate = point_add(scalar_mult(u1, G), scalar_mult(u2, point))
        if candidate is None:
            return False
        return candidate[0] % N == r
    except (ECDSAError, ZeroDivisionError, ValueError):
        return False


# --------------------------------------------------------------------------
# Addresses (Base58Check, exactly like Bitcoin)
# --------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_encode(data: bytes) -> str:
    """Base58 encoding -- excludes 0, O, I and l to avoid visual ambiguity."""
    number = int.from_bytes(data, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    # Preserve leading zero bytes as '1'
    for byte in data:
        if byte == 0:
            encoded = _B58_ALPHABET[0] + encoded
        else:
            break
    return encoded or _B58_ALPHABET[0]


def base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + _B58_ALPHABET.index(char)
    combined = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    padding = len(value) - len(value.lstrip(_B58_ALPHABET[0]))
    return b"\x00" * padding + combined


def hash160(data: bytes) -> bytes:
    """SHA-256 followed by RIPEMD-160 -- the classic Bitcoin address digest."""
    sha = hashlib.sha256(data).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except ValueError:  # pragma: no cover - depends on the OpenSSL build
        return hashlib.sha256(sha).digest()[:20]


def public_key_to_address(public_hex: str, prefix: int = 0x00) -> str:
    """Convert a SEC1 public key into a Base58Check address.

    The 4-byte double-SHA256 checksum lets anyone detect a typo in an address
    before using it, which is why blockchain addresses are self-validating.
    """
    payload = bytes([prefix]) + hash160(bytes.fromhex(public_hex))
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def address_is_valid(address: str) -> bool:
    """Validate an address by recomputing its Base58Check checksum."""
    try:
        decoded = base58_decode(address)
        payload, checksum = decoded[:-4], decoded[-4:]
        expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return hmac.compare_digest(checksum, expected)
    except (ValueError, IndexError):
        return False


__all__ = [
    "ECDSAError",
    "KeyPair",
    "G",
    "N",
    "P",
    "point_add",
    "scalar_mult",
    "is_on_curve",
    "generate_keypair",
    "keypair_from_private",
    "parse_public_key",
    "sign",
    "verify",
    "base58_encode",
    "base58_decode",
    "hash160",
    "public_key_to_address",
    "address_is_valid",
]
