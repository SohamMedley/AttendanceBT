"""
Keccak-256 -- the hash function Ethereum uses (and Bitcoin does not).

Note the trap: Ethereum's ``keccak256`` is **not** NIST's ``SHA3-256``, even
though they look similar. Both use the same Keccak-f[1600] permutation, but the
padding differs:

* Keccak (Ethereum): domain/padding byte ``0x01``
* SHA3 (NIST):       domain/padding byte ``0x06``

Python's ``hashlib.sha3_256`` therefore produces the *wrong* digest for anything
Ethereum-related, and there is no ``keccak256`` in the standard library. So we
implement it here -- about 60 lines -- which also lets us verify our own
implementation against the published test vectors:

    keccak256("")    = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
    keccak256("abc") = 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45

Used for the ERC-20 style function selector ``anchor(bytes32)`` when preparing
the Ethereum anchoring transaction.
"""

from __future__ import annotations

ROTATION_OFFSETS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

MASK = (1 << 64) - 1
RATE_BYTES = 136  # 1088-bit rate for Keccak-256 (=> 512-bit capacity)
LANES = 25


def _rol(value: int, shift: int) -> int:
    """Rotate a 64-bit lane left."""
    shift %= 64
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & MASK


def _keccak_f1600(state: list[int]) -> None:
    """The Keccak-f[1600] permutation: 24 rounds of theta, rho, pi, chi, iota."""
    for round_constant in ROUND_CONSTANTS:
        # -- theta: column parity diffusion
        c = [
            state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
            for x in range(5)
        ]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]

        # -- rho + pi: rotate lanes and permute their positions
        b = [0] * LANES
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(state[x + 5 * y], ROTATION_OFFSETS[x][y])

        # -- chi: the only non-linear step
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ (
                    (~b[(x + 1) % 5 + 5 * y] & MASK) & b[(x + 2) % 5 + 5 * y]
                )

        # -- iota: break symmetry between rounds
        state[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Compute the legacy Keccak-256 digest (Ethereum's hash) of ``data``."""
    state = [0] * LANES
    padded = bytearray(data)

    # Multi-rate pad10*1 with the Keccak domain byte 0x01 (SHA3 would use 0x06).
    padded.append(0x01)
    while len(padded) % RATE_BYTES != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    # Absorb
    for offset in range(0, len(padded), RATE_BYTES):
        block = padded[offset : offset + RATE_BYTES]
        for index in range(RATE_BYTES // 8):
            state[index] ^= int.from_bytes(block[index * 8 : index * 8 + 8], "little")
        _keccak_f1600(state)

    # Squeeze 32 bytes (the first 4 lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in state[:4])


def keccak256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return keccak256(data).hex()


def function_selector(signature: str) -> str:
    """The 4-byte function selector: the first 4 bytes of keccak256(signature)."""
    return keccak256_hex(signature)[:8]


def encode_uint256(value: int) -> str:
    return f"{value:064x}"


def encode_bytes32(data: bytes | str) -> str:
    if isinstance(data, str):
        data = bytes.fromhex(data)
    if len(data) > 32:
        raise ValueError("bytes32 cannot hold more than 32 bytes")
    return data.rjust(32, b"\x00").hex()


def address_to_topic(address: str) -> str:
    """Left-pad a 20-byte address into a 32-byte topic."""
    clean = address.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError("Ethereum addresses are 20 bytes (40 hex characters)")
    return clean.rjust(64, "0")


__all__ = [
    "keccak256",
    "keccak256_hex",
    "function_selector",
    "encode_uint256",
    "encode_bytes32",
    "address_to_topic",
]
