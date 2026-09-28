"""
HTTP layer.

Three blueprints, split by who is talking:

``pages``
    Server-rendered HTML for the browser.

``attendance_api``
    ``/api/...`` endpoints used during a lecture: sessions, the rotating QR,
    marking, the register and the analytics reports.

``chain_api``
    ``/api/...`` endpoints that report on the ledger: the explorer,
    verification, the tamper lab, anchoring and system status.
"""

from . import attendance, chain, pages

#: Registered by `create_app`, in this order.
BLUEPRINTS = (pages.bp, attendance.bp, chain.bp)

__all__ = ["BLUEPRINTS", "attendance", "chain", "pages"]
