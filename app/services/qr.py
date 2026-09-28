"""
Rotating QR attendance tokens.

The problem
-----------
A static QR code stuck on the classroom wall is worthless: photograph it once and
you can mark yourself present from home for the rest of the semester.

The fix -- the same idea behind Google Authenticator
----------------------------------------------------
The session holds a random 32-byte **secret** that never leaves the server. The
displayed QR code is regenerated every ``ttl`` seconds (default 30) from

    HMAC-SHA256(session_secret, session_id | time_slot | version)

Because the payload is *time-bound* and *signed*, an attacker cannot forge one
without the secret, and a screenshot stops working once its slot expires. This is
a TOTP (RFC 6238) construction in spirit: a shared secret plus a time counter
produces a value that is valid only for a short window.

Two-layer design
----------------
1. **Slot signature** -- unforgeable, expires automatically.
2. **Server-side session state** -- the session can be closed at any moment, and
   the signing secret is unique per session, so token reuse across lectures is
   impossible.

Accepted clock skew is plus/minus one slot, so a student who scans at the exact
moment of rotation is never wrongly rejected. That yields an effective validity
window of ``ttl``..``3*ttl`` seconds rather than exactly ``ttl``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

TOKEN_VERSION = 1
SKEW_SLOTS = 1


class QRTokenError(ValueError):
    """Raised when a scanned token is invalid, expired, or forged."""

    def __init__(self, message: str, code: str = "INVALID_TOKEN") -> None:
        super().__init__(message)
        self.code = code


def new_session_secret() -> str:
    """32 random bytes, hex encoded. Unique per lecture session."""
    return secrets.token_hex(32)


def current_slot(ttl: int, at: float | None = None) -> int:
    """The current time-slot index. Changes every ``ttl`` seconds."""
    return int((at if at is not None else time.time()) // max(1, ttl))


def _signing_message(session_id: str, slot: int, version: int = TOKEN_VERSION) -> bytes:
    return f"BCOE-ATT|v{version}|{session_id}|{slot}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(secret: str, session_id: str, slot: int, version: int = TOKEN_VERSION) -> str:
    digest = hmac.new(
        bytes.fromhex(secret), _signing_message(session_id, slot, version), hashlib.sha256
    ).digest()
    return _b64url(digest[:16])  # 128-bit MAC is plenty for a 30-second window


@dataclass
class QRToken:
    """A short-lived, signed QR payload."""

    token: str
    session_id: str
    slot: int
    issued_at: float
    expires_in: float
    ttl: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "session_id": self.session_id,
            "slot": self.slot,
            "issued_at": self.issued_at,
            "expires_in": round(self.expires_in, 1),
            "ttl": self.ttl,
        }


def issue_token(session_id: str, secret: str, ttl: int, *, at: float | None = None) -> QRToken:
    """Build the QR payload for the current time slot."""
    now = at if at is not None else time.time()
    slot = current_slot(ttl, now)
    payload = {
        "v": TOKEN_VERSION,
        "s": session_id,
        "t": slot,
        "k": _signature(secret, session_id, slot),
    }
    token = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    slot_end = (slot + 1) * ttl
    return QRToken(
        token=token,
        session_id=session_id,
        slot=slot,
        issued_at=now,
        expires_in=max(0.0, slot_end - now),
        ttl=ttl,
    )


def parse_token(token: str) -> dict[str, Any]:
    """Decode a scanned token without trusting anything in it."""
    try:
        payload = json.loads(_b64url_decode(token.strip()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise QRTokenError("This QR code is not a valid attendance token", "MALFORMED") from exc
    if not isinstance(payload, dict) or "s" not in payload or "k" not in payload:
        raise QRTokenError("Malformed attendance token", "MALFORMED")
    return payload


def verify_token(
    token: str,
    *,
    secret: str,
    session_id: str,
    ttl: int,
    at: float | None = None,
    skew_slots: int = SKEW_SLOTS,
) -> dict[str, Any]:
    """Validate a scanned token. Raises :class:`QRTokenError` on any problem.

    Checks, in order:

    1. version supported
    2. token belongs to the session the student claims (prevents cross-session replay)
    3. HMAC signature matches -- proves the college server issued it
    4. the time slot is within the accepted window
    """
    now = at if at is not None else time.time()
    payload = parse_token(token)

    if int(payload.get("v", 0)) != TOKEN_VERSION:
        raise QRTokenError("Unsupported token version", "VERSION")

    if str(payload.get("s")) != session_id:
        raise QRTokenError(
            "This QR code belongs to a different lecture session", "WRONG_SESSION"
        )

    slot = int(payload.get("t", -1))
    expected = _signature(secret, session_id, slot)
    if not hmac.compare_digest(str(payload.get("k", "")), expected):
        raise QRTokenError("This QR code is not genuine (bad signature)", "BAD_SIGNATURE")

    live_slot = current_slot(ttl, now)
    age_slots = live_slot - slot
    if age_slots > skew_slots:
        raise QRTokenError(
            "This QR code has expired. Ask your faculty member to show the current code.",
            "EXPIRED",
        )
    if age_slots < -skew_slots:
        raise QRTokenError("This QR code is not valid yet", "NOT_YET_VALID")

    return {
        "session_id": session_id,
        "slot": slot,
        "live_slot": live_slot,
        "age_seconds": (live_slot - slot) * ttl,
        "age_slots": age_slots,
        "fresh": age_slots == 0,
    }


def token_fingerprint(token: str) -> str:
    """Short hash of a token, safe to store in logs and audit trails."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def render_svg(token: str, *, box_size: int = 10, border: int = 3) -> str:
    """Render a token as a QR code in SVG form.

    SVG (rather than a bitmap) keeps the code crisp on any projector or phone
    screen without needing Pillow installed. ``qrcode`` is pure Python for the
    SVG backend, so this works with no image library at all.
    """
    import io

    import qrcode
    import qrcode.image.svg

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    code.add_data(token)
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def render_data_uri(token: str, **kwargs) -> str:
    """The same QR code as a ``data:`` URI, ready to drop into an <img> tag."""
    import base64

    svg = render_svg(token, **kwargs)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


__all__ = [
    "QRToken",
    "QRTokenError",
    "issue_token",
    "verify_token",
    "parse_token",
    "new_session_secret",
    "current_slot",
    "token_fingerprint",
    "render_svg",
    "render_data_uri",
    "TOKEN_VERSION",
    "SKEW_SLOTS",
]
