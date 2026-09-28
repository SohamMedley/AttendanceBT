"""Blockchain API: explorer, verification, receipts, Merkle proofs, tamper lab."""

from __future__ import annotations

import time

from flask import Blueprint, Response, jsonify, request

from ..services import Services
from ..services.ledger import LedgerError

bp = Blueprint("chain_api", __name__, url_prefix="/api/chain")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def chain_levels():
    """The four tamper layers, validated before we touch the chain."""
    from ..blockchain.chain import Blockchain

    return set(Blockchain.TAMPER_LEVELS)


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


@bp.get("/stats")
def stats():
    services = _svc()
    return jsonify(
        {
            "ok": True,
            "stats": services.chain_stats(),
            "signature_audit": services.ledger.signature_audit_status(),
            "storage_backend": services.store.name,
        }
    )


@bp.get("/blocks")
def list_blocks():
    services = _svc()
    limit = int(request.args.get("limit", 40))
    blocks = services.ledger.chain.chain[::-1][:limit]
    return jsonify(
        {
            "ok": True,
            "count": len(services.ledger.chain.chain),
            "blocks": [block.to_dict(include_transactions=False) for block in blocks],
        }
    )


@bp.get("/blocks/<int:index>")
def get_block(index: int):
    services = _svc()
    block = services.ledger.chain.block_by_index(index)
    if block is None:
        raise LedgerError(f"No block at index {index}", "UNKNOWN_BLOCK", 404)
    tree = services.ledger.chain.merkle_tree(index)
    data = block.to_dict(include_transactions=True)
    data["integrity"] = {
        "hash_valid": block.hash_is_valid(),
        "merkle_valid": block.merkle_is_valid(),
        "proof_of_work_valid": block.pow_is_valid(),
        "recomputed_hash": block.recompute_hash(),
    }
    return jsonify({"ok": True, "block": data, "merkle": tree})


@bp.get("/transactions/<tx_id>")
def get_transaction(tx_id: str):
    """Full audit receipt: the record, its signature, its Merkle proof."""
    return jsonify({"ok": True, **(_svc().ledger.receipt(tx_id))})


@bp.get("/verify")
def verify():
    """Validate the chain.

    ``?deep=1`` additionally re-verifies every ECDSA signature (slow the first
    time, instant afterwards thanks to the per-block verification cache).
    """
    deep = request.args.get("deep", "0") in {"1", "true", "yes"}
    report = _svc().ledger.verify_chain(deep=deep, parallel=True)
    return jsonify({"ok": True, "report": report})


@bp.post("/tamper")
def tamper():
    """Deliberately corrupt a record and show the chain catching it."""
    services = _svc()
    payload = request.get_json(silent=True) or {}
    try:
        level = int(payload.get("level", 1))
    except (TypeError, ValueError):
        raise LedgerError("level must be a number from 1 to 4", "BAD_LEVEL", 400)
    if level not in chain_levels():
        raise LedgerError(
            f"Level {level} is not one of the four tamper layers (1-4)",
            "BAD_LEVEL",
            400,
        )
    chain = services.ledger.chain

    note = None
    tx_id = payload.get("tx_id")
    if not tx_id:
        candidates = [
            tx
            for block in chain.chain
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        ]
        if not candidates:
            raise LedgerError(
                "There are no attendance records on the chain yet. "
                "Run `python manage.py demo` first.",
                "NO_RECORDS",
                400,
            )

        # Prefer a record in a block that has blocks sealed AFTER it.
        #
        # Re-mining the *tip* block is genuinely undetectable by internal checks
        # alone -- there is no later block whose prev_hash could break, and the
        # attacker recomputed every hash. That is precisely why the public
        # anchoring step exists, but it makes a poor demonstration, so the demo
        # picks a record with descendants and the linkage check can do its job.
        historical = [
            tx
            for block in chain.chain[:-1]
            for tx in block.transactions
            if tx.tx_type == "ATTENDANCE"
        ]
        if historical:
            tx_id = historical[0].tx_id
        else:
            tx_id = candidates[-1].tx_id
            note = (
                "These records are in the most recent block, so a level-4 tamper "
                "re-mines the chain tip and is NOT detectable by internal checks "
                "alone. That is exactly what public anchoring is for. Seal another "
                "block to demonstrate the link check."
            )

    result = chain.tamper(
        tx_id=tx_id,
        field_name=payload.get("field_name", "status"),
        new_value=payload.get("new_value", "PRESENT"),
        level=level,
    )
    return jsonify({"ok": True, "note": note, **result})


@bp.get("/export")
def export_chain():
    """Download the entire chain as a self-contained JSON file."""
    services = _svc()
    data = services.ledger.chain.export_json()
    filename = f"bcoe_attendance_chain_{int(time.time())}.json"
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.get("/signatures")
def signature_audit():
    return jsonify({"ok": True, "audit": _svc().ledger.signature_audit_status()})


@bp.get("/audit")
def audit_trail():
    limit = int(request.args.get("limit", 100))
    entries = _svc().repo.list_audit(limit=limit)
    return jsonify({"ok": True, "count": len(entries), "entries": [dict(e) for e in entries]})


@bp.get("/pending")
def pending():
    services = _svc()
    chain = services.ledger.chain
    return jsonify(
        {
            "ok": True,
            "mempool_size": len(chain.mempool),
            "pending_root": services.ledger.pending_root(),
            "seal_threshold": chain.seal_threshold,
            "transactions": [
                {
                    "tx_id": tx.tx_id,
                    "student_roll": tx.payload.get("student_roll"),
                    "session_id": tx.payload.get("session_id"),
                }
                for tx in chain.mempool
            ],
        }
    )


__all__ = ["bp", "init"]
