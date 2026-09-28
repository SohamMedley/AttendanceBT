"""Anchoring API: publish Merkle roots, verify proofs, export .ots files."""

from __future__ import annotations

import base64

from flask import Blueprint, Response, jsonify, request

from ..anchoring import provider_catalogue
from ..services import Services
from ..services.ledger import LedgerError

bp = Blueprint("anchoring_api", __name__, url_prefix="/api/anchors")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


@bp.get("")
def list_anchors():
    services = _svc()
    anchors = services.repo.list_anchors(limit=int(request.args.get("limit", 50)))
    return jsonify(
        {
            "ok": True,
            "count": len(anchors),
            "pending": services.anchoring.pending_summary(),
            "anchors": [dict(a) for a in anchors],
        }
    )


@bp.get("/catalogue")
def catalogue():
    services = _svc()
    return jsonify(
        {
            "ok": True,
            "current_provider": services.anchoring.provider.name,
            "providers": provider_catalogue(services.config),
            "pending": services.anchoring.pending_summary(),
        }
    )


@bp.post("")
def create_anchor():
    """Publish the Merkle root of every unanchored record."""
    payload = request.get_json(silent=True) or {}
    anchor = _svc().anchoring.anchor_now(
        actor=payload.get("actor", "faculty"), note=payload.get("note", "")
    )
    return jsonify({"ok": True, "anchor": anchor}), 201


@bp.get("/<anchor_id>")
def get_anchor(anchor_id: str):
    anchor = _svc().repo.get_anchor(anchor_id)
    if anchor is None:
        raise LedgerError(f"Unknown anchor {anchor_id}", "UNKNOWN_ANCHOR", 404)
    return jsonify({"ok": True, "anchor": dict(anchor)})


@bp.get("/<anchor_id>/verify")
def verify_anchor(anchor_id: str):
    """Re-derive the anchored root from the chain and re-check the signature."""
    return jsonify({"ok": True, **_svc().anchoring.verify_anchor(anchor_id)})


@bp.get("/<anchor_id>/proof.ots")
def download_proof(anchor_id: str):
    """Download the OpenTimestamps proof so it can be verified independently.

    ``ots verify <file>`` checks the proof against the Bitcoin blockchain without
    trusting this application, this server, or this college.
    """
    anchor = _svc().repo.get_anchor(anchor_id)
    if anchor is None:
        raise LedgerError(f"Unknown anchor {anchor_id}", "UNKNOWN_ANCHOR", 404)
    if not anchor.get("proof"):
        raise LedgerError(
            "This anchor has no downloadable proof (the provider is simulated)",
            "NO_PROOF",
            400,
        )

    blob = base64.b64decode(anchor["proof"])
    # The .ots header lets the reference client recognise the file.
    header = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    return Response(
        header + blob,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={anchor_id}.ots"},
    )


@bp.get("/contract/source")
def contract_source():
    """The Solidity contract the Ethereum provider targets."""
    from ..anchoring import ANCHOR_CONTRACT_SOURCE

    return Response(ANCHOR_CONTRACT_SOURCE, mimetype="text/plain")


__all__ = ["bp", "init"]
