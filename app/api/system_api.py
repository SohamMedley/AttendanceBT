"""System API: status, health, seeding, difficulty, self-test."""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from ..services import Services
from ..services.ledger import LedgerError

bp = Blueprint("system_api", __name__, url_prefix="/api/system")
_services: Services | None = None


def init(services: Services) -> None:
    global _services
    _services = services


def _svc() -> Services:
    if _services is None:  # pragma: no cover
        raise LedgerError("Service layer is not initialised", "NOT_READY", 503)
    return _services


@bp.get("/status")
def status():
    return jsonify({"ok": True, "status": _svc().status()})


@bp.get("/health")
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


@bp.post("/seed")
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


@bp.post("/difficulty")
def set_difficulty():
    """Change the Proof-of-Work difficulty live -- useful during a demo."""
    payload = request.get_json(silent=True) or {}
    result = _svc().ledger.set_difficulty(int(payload.get("difficulty", 4)))
    return jsonify({"ok": True, **result})


@bp.post("/benchmark")
def benchmark():
    """Time the Proof-of-Work on this machine, for the 'how hard is mining' question."""
    from ..blockchain import proof_of_work as pow_module

    payload = request.get_json(silent=True) or {}
    # Each step multiplies expected work by 16; cap it so a demo request
    # cannot hang the browser for minutes. The CLI has no such limit.
    difficulty = max(1, min(5, int(payload.get("difficulty", 4))))
    result = pow_module.benchmark(difficulty)
    return jsonify({"ok": True, "benchmark": result.to_dict()})


@bp.get("/config")
def config_view():
    """Configuration with all secrets removed."""
    return jsonify({"ok": True, "config": _svc().config.as_dict()})


@bp.post("/self-test")
def self_test():
    """Run the cryptographic self-tests from the web UI."""
    from ..blockchain import ecdsa, keccak
    from ..blockchain.merkle import merkle_proof, merkle_root, verify_merkle_proof
    from ..blockchain import proof_of_work as pow_module
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

    signature = ecdsa.sign(keypair.private_hex, b"bcoe-test")
    checks.append(
        {
            "name": "ECDSA sign/verify",
            "expected": "valid",
            "actual": "valid" if ecdsa.verify(keypair.public_hex, b"bcoe-test", signature) else "invalid",
            "passed": ecdsa.verify(keypair.public_hex, b"bcoe-test", signature),
        }
    )
    checks.append(
        {
            "name": "Tampered message rejected",
            "expected": "rejected",
            "actual": "rejected"
            if not ecdsa.verify(keypair.public_hex, b"bcoe-test!", signature)
            else "accepted",
            "passed": not ecdsa.verify(keypair.public_hex, b"bcoe-test!", signature),
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

    mined = pow_module.mine(
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
            "passed": pow_module.meets_difficulty(mined.block_hash, 3),
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


__all__ = ["bp", "init"]
