"""
Chain API: the explorer, verification, the tamper lab, public anchoring and
system status.

Everything that reports on the ledger rather than changing attendance lives
here -- reading blocks and transactions, re-verifying the chain, running the
four-level tamper demonstration, publishing Merkle roots to a public ledger,
and the diagnostics the Settings page shows.
"""


import time

from flask import Blueprint, Response, jsonify, request

from ..anchoring import ANCHOR_CONTRACT_SOURCE, provider_catalogue
from ..blockchain import crypto as ecdsa
from ..blockchain import crypto as keccak
from ..blockchain.chain import Blockchain, benchmark as pow_benchmark, meets_difficulty, mine
from ..blockchain.crypto import (
    address_is_valid,
    keccak256_hex,
    merkle_proof,
    merkle_root,
    sign_message,
    verify_merkle_proof,
    verify_signature,
)
from ..ledger import LedgerError, Services
from ..seed import build_seed_payload
from ..services import QRTokenError, issue_token, new_session_secret, verify_token

bp = Blueprint("chain_api", __name__, url_prefix="/api")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


# ============================================================================
# Explorer, verification and the tamper lab
# ============================================================================


import time

from flask import Blueprint, Response, jsonify, request

from ..ledger import Services
from ..ledger import LedgerError


def chain_levels():
    """The four tamper layers, validated before we touch the chain."""
    from ..blockchain.chain import Blockchain

    return set(Blockchain.TAMPER_LEVELS)


@bp.get("/chain/stats")
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


@bp.get("/chain/blocks")
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


@bp.get("/chain/blocks/<int:index>")
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


@bp.get("/chain/transactions/<tx_id>")
def get_transaction(tx_id: str):
    """Full audit receipt: the record, its signature, its Merkle proof."""
    return jsonify({"ok": True, **(_svc().ledger.receipt(tx_id))})


@bp.get("/chain/verify")
def verify():
    """Validate the chain.

    ``?deep=1`` additionally re-verifies every ECDSA signature (slow the first
    time, instant afterwards thanks to the per-block verification cache).
    """
    deep = request.args.get("deep", "0") in {"1", "true", "yes"}
    report = _svc().ledger.verify_chain(deep=deep, parallel=True)
    return jsonify({"ok": True, "report": report})


@bp.post("/chain/tamper")
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


@bp.get("/chain/export")
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


@bp.get("/chain/signatures")
def signature_audit():
    return jsonify({"ok": True, "audit": _svc().ledger.signature_audit_status()})


@bp.get("/chain/audit")
def audit_trail():
    limit = int(request.args.get("limit", 100))
    entries = _svc().repo.list_audit(limit=limit)
    return jsonify({"ok": True, "count": len(entries), "entries": [dict(e) for e in entries]})


@bp.get("/chain/pending")
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

# ============================================================================
# Public anchoring
# ============================================================================


import base64

from flask import Blueprint, Response, jsonify, request

from ..anchoring import provider_catalogue
from ..ledger import Services
from ..ledger import LedgerError


@bp.get("/anchors")
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


@bp.get("/anchors/catalogue")
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


@bp.post("/anchors")
def create_anchor():
    """Publish the Merkle root of every unanchored record."""
    payload = request.get_json(silent=True) or {}
    anchor = _svc().anchoring.anchor_now(
        actor=payload.get("actor", "faculty"), note=payload.get("note", "")
    )
    return jsonify({"ok": True, "anchor": anchor}), 201


@bp.get("/anchors/<anchor_id>")
def get_anchor(anchor_id: str):
    anchor = _svc().repo.get_anchor(anchor_id)
    if anchor is None:
        raise LedgerError(f"Unknown anchor {anchor_id}", "UNKNOWN_ANCHOR", 404)
    return jsonify({"ok": True, "anchor": dict(anchor)})


@bp.get("/anchors/<anchor_id>/verify")
def verify_anchor(anchor_id: str):
    """Re-derive the anchored root from the chain and re-check the signature."""
    return jsonify({"ok": True, **_svc().anchoring.verify_anchor(anchor_id)})


@bp.get("/anchors/<anchor_id>/proof.ots")
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


@bp.get("/anchors/contract/source")
def contract_source():
    """The Solidity contract the Ethereum provider targets."""
    from ..anchoring import ANCHOR_CONTRACT_SOURCE

    return Response(ANCHOR_CONTRACT_SOURCE, mimetype="text/plain")

# ============================================================================
# System status, seeding, difficulty and self-test
# ============================================================================


import time

from flask import Blueprint, jsonify, request

from ..blockchain import crypto as ecdsa
from ..blockchain import crypto as keccak
from ..blockchain.chain import (
    benchmark as pow_benchmark,
    meets_difficulty,
    mine,
)
from ..blockchain.crypto import (
    address_is_valid,
    keccak256_hex,
    merkle_proof,
    merkle_root,
    sign_message,
    verify_merkle_proof,
    verify_signature,
)
from ..ledger import Services
from ..ledger import LedgerError


@bp.get("/system/status")
def status():
    return jsonify({"ok": True, "status": _svc().status()})


@bp.get("/system/health")
def health():
    services = _svc()
    try:
        store_health = services.store.health()
    except Exception as exc:  # pragma: no cover
        store_health = {"ok": False, "error": str(exc)}
    return jsonify(
        {
            "ok": True,
            "time": time.time(),
            "storage": store_health,
            "chain_blocks": len(services.ledger.chain.chain),
            "register_seeded": services.is_seeded(),
        }
    )


@bp.post("/system/seed")
def seed():
    """Populate the register from the BCOE seed data (idempotent)."""
    from ..seed import build_seed_payload

    services = _svc()
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force", False))
    per_year = int(payload.get("students_per_year", 60))

    if services.is_seeded() and not force:
        return jsonify(
            {
                "ok": True,
                "skipped": True,
                "message": f"Register already contains {services.repo.student_count()} students.",
            }
        )

    data = build_seed_payload(students_per_year=per_year)
    services.repo.upsert_students(data["students"])
    services.repo.upsert_many_faculty(data["faculty"])
    services.repo.upsert_many_subjects(data["subjects"])
    student_keys = services.register_keys_for_students(data["students"])
    faculty_keys = services.register_keys_for_faculty(data["faculty"])
    services.repo.log_audit(
        "REGISTER_SEEDED",
        actor="admin",
        detail={"students": len(data["students"]), "faculty": len(data["faculty"])},
    )
    return jsonify(
        {
            "ok": True,
            "students": len(data["students"]),
            "faculty": len(data["faculty"]),
            "subjects": len(data["subjects"]),
            "keys_created": student_keys + faculty_keys,
        }
    )


@bp.post("/system/difficulty")
def set_difficulty():
    """Change the Proof-of-Work difficulty live -- useful during a demo."""
    payload = request.get_json(silent=True) or {}
    result = _svc().ledger.set_difficulty(int(payload.get("difficulty", 4)))
    return jsonify({"ok": True, **result})


@bp.post("/system/benchmark")
def benchmark():
    """Time the Proof-of-Work on this machine, for the 'how hard is mining' question."""
    payload = request.get_json(silent=True) or {}
    # Each step multiplies expected work by 16; cap it so a demo request
    # cannot hang the browser for minutes. The CLI has no such limit.
    difficulty = max(1, min(5, int(payload.get("difficulty", 4))))
    result = pow_benchmark(difficulty)
    return jsonify({"ok": True, "benchmark": result.to_dict()})


@bp.get("/system/config")
def config_view():
    """Configuration with all secrets removed."""
    return jsonify({"ok": True, "config": _svc().config.as_dict()})


@bp.post("/system/self-test")
def self_test():
    """Run the cryptographic self-tests from the web UI."""
    from ..services import qr

    checks: list[dict] = []

    keypair = ecdsa.keypair_from_private(1)
    expected = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    checks.append(
        {
            "name": "secp256k1 public key for private key 1",
            "expected": expected[:24] + "...",
            "actual": keypair.public_hex[:24] + "...",
            "passed": keypair.public_hex == expected,
        }
    )

    signature = ecdsa.sign_message(keypair.private_hex, b"bcoe-test")
    checks.append(
        {
            "name": "ECDSA sign/verify",
            "expected": "valid",
            "actual": "valid" if ecdsa.verify_signature(keypair.public_hex, b"bcoe-test", signature) else "invalid",
            "passed": ecdsa.verify_signature(keypair.public_hex, b"bcoe-test", signature),
        }
    )
    checks.append(
        {
            "name": "Tampered message rejected",
            "expected": "rejected",
            "actual": "rejected"
            if not ecdsa.verify_signature(keypair.public_hex, b"bcoe-test!", signature)
            else "accepted",
            "passed": not ecdsa.verify_signature(keypair.public_hex, b"bcoe-test!", signature),
        }
    )

    empty = keccak.keccak256_hex(b"")
    checks.append(
        {
            "name": "Keccak-256 of empty string (Ethereum vector)",
            "expected": "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
            "actual": empty,
            "passed": empty
            == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        }
    )
    selector = keccak.function_selector("transfer(address,uint256)")
    checks.append(
        {
            "name": "ERC-20 selector transfer(address,uint256)",
            "expected": "a9059cbb",
            "actual": selector,
            "passed": selector == "a9059cbb",
        }
    )

    leaves = [f"tx-{i}" for i in range(7)]
    root = merkle_root(leaves)
    proofs_ok = all(
        verify_merkle_proof(leaf, merkle_proof(leaves, i), root)
        for i, leaf in enumerate(leaves)
    )
    checks.append(
        {
            "name": "Merkle inclusion proofs (7 leaves)",
            "expected": "all valid",
            "actual": "all valid" if proofs_ok else "failed",
            "passed": proofs_ok,
        }
    )

    mined = mine(
        {
            "index": 1, "timestamp": 0.0, "prev_hash": "0" * 64,
            "merkle_root": "0" * 64, "difficulty": 3, "miner": "web-test",
        },
        difficulty=3,
    )
    checks.append(
        {
            "name": "Proof-of-Work (difficulty 3)",
            "expected": "hash starts with 000",
            "actual": f"{mined.block_hash[:12]}... after {mined.attempts} attempts",
            "passed": meets_difficulty(mined.block_hash, 3),
        }
    )

    secret = qr.new_session_secret()
    token = qr.issue_token("LEC-WEBTEST", secret, 30)
    fresh_ok = qr.verify_token(
        token.token, secret=secret, session_id="LEC-WEBTEST", ttl=30
    )["fresh"]
    checks.append(
        {
            "name": "Rotating QR token accepted for its own session",
            "expected": "accepted",
            "actual": "accepted" if fresh_ok else "rejected",
            "passed": fresh_ok,
        }
    )
    try:
        qr.verify_token(token.token, secret=secret, session_id="OTHER", ttl=30)
        cross_blocked = False
        cross_actual = "ACCEPTED (bad!)"
    except qr.QRTokenError:
        cross_blocked = True
        cross_actual = "rejected"
    checks.append(
        {
            "name": "QR token rejected for a different session",
            "expected": "rejected",
            "actual": cross_actual,
            "passed": cross_blocked,
        }
    )

    return jsonify(
        {
            "ok": True,
            "passed": sum(1 for c in checks if c["passed"]),
            "total": len(checks),
            "all_passed": all(c["passed"] for c in checks),
            "checks": checks,
        }
    )
