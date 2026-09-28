#!/usr/bin/env python3
"""
Management CLI for the BCOE Blockchain Attendance System.

    python manage.py seed            # create the college register + keys
    python manage.py demo            # 6 weeks of realistic history
    python manage.py serve           # start the web application
    python manage.py verify          # re-verify the whole chain
    python manage.py tamper --level 4
    python manage.py firebase-check  # test the Firestore connection

Run ``python manage.py --help`` for the full list.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_config  # noqa: E402
from app.seed import build_seed_payload  # noqa: E402
from app.services import Services  # noqa: E402
from app.services.ledger import LedgerError  # noqa: E402

BANNER = r"""
 ____   ____ ___  _____   _   _ _____ _____ ____  _   _ _____ _____ ____
| __ ) / ___/ _ \| ____| | | | |_   _|_   _|  _ \| \ | |_   _| ____/ ___|
|  _ \| |  | | | |  _|   | |_| | | |   | | | |_) |  \| | | | |  _| \___ \
| |_) | |__| |_| | |___  |  _  | | |   | | |  _ <| |\  | | | | |___ ___) |
|____/ \____\___/|_____| |_| |_| |_|   |_| |_| \_\_| \_| |_| |_____|____/
        Blockchain Attendance System  |  Bharat College of Engineering
        University of Mumbai  |  CSDO7022 Blockchain Technologies
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _services() -> Services:
    config = load_config()
    return Services(config)


def _say(message: str) -> None:
    print(message, flush=True)


def _rule(title: str = "") -> None:
    width = 78
    if title:
        print(f"\n{title.center(width, '-')}")
    else:
        print("-" * width)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    services = _services()
    _rule("INITIALISED")
    _say(f"Storage backend : {services.store.name}")
    _say(f"Chain height    : {services.ledger.chain.height}")
    _say(f"Genesis hash    : {services.ledger.chain.chain[0].hash}")
    _say(f"Institution key : {services.institution.address}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    services = _services()
    if services.is_seeded() and not args.force:
        _say(
            f"Register already has {services.repo.student_count()} students. "
            "Re-run with --force to reseed."
        )
        return 0

    payload = build_seed_payload(students_per_year=args.students)

    _rule("SEEDING BCOE REGISTER")
    students = payload["students"]
    faculty = payload["faculty"]
    subjects = payload["subjects"]

    services.repo.upsert_students(students)
    services.repo.upsert_many_faculty(faculty)
    services.repo.upsert_many_subjects(subjects)

    keys = services.register_keys_for_students(students)
    faculty_keys = services.register_keys_for_faculty(faculty)

    services.repo.log_audit(
        "REGISTER_SEEDED",
        actor="admin",
        detail={
            "students": len(students),
            "faculty": len(faculty),
            "subjects": len(subjects),
        },
    )

    _say(f"Students  : {len(students)}  (TE: {args.students}, BE: {args.students})")
    _say(f"Faculty   : {len(faculty)}")
    _say(f"Subjects  : {len(subjects)}")
    _say(f"Key pairs : {keys} student, {faculty_keys} faculty ({len(services.keystore)} total)")
    _say("\nSample roll numbers:")
    for student in students[:3]:
        _say(f"  {student['roll_no']}  {student['name']:<24} {student['address']}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from app.demo import simulate_history

    services = _services()
    if not services.is_seeded():
        _say("Register is empty -- seeding first.\n")
        cmd_seed(argparse.Namespace(students=60, force=False))

    if args.clean:
        _say("Note: --clean starts a fresh ledger file before generating history.")

    _rule("GENERATING SIX WEEKS OF HISTORY")
    started = time.time()
    summary = simulate_history(
        config=services.config,
        repository=services.repo,
        ledger=services.ledger,
        subjects=args.subjects,
        year=args.year,
        weeks=args.weeks,
    )
    elapsed = time.time() - started

    _rule("SIMULATION COMPLETE")
    for key, value in summary.items():
        _say(f"{key.replace('_', ' ').title():<20}: {value}")
    _say(f"{'elapsed':<20}: {elapsed:.2f}s")

    _rule("VERIFYING THE GENERATED CHAIN")
    fast = services.ledger.verify_chain(deep=False)
    _say(
        f"Structural check : {'PASSED' if fast['valid'] else 'FAILED'}  "
        f"({fast['checked_blocks']} blocks, {fast['checked_transactions']} records, "
        f"{fast['duration_ms']:.0f} ms)"
    )

    _say("Running the full cryptographic audit (every ECDSA signature)...")
    started = time.time()
    deep = services.ledger.verify_chain(deep=True, parallel=True)
    elapsed = time.time() - started
    _say(
        f"Signature audit  : {'PASSED' if deep['valid'] else 'FAILED'}  "
        f"({deep['signatures_checked']} signatures re-verified in {elapsed:.0f}s)"
    )
    _say(
        "\nSignature results are cached per block hash, so future audits are "
        f"instant. Cache: {deep['signature_audit']['blocks_signature_verified']}"
        f"/{deep['signature_audit']['blocks_with_transactions']} blocks verified."
    )
    _say("\nStart the app with:  python manage.py serve")
    return 0 if (fast["valid"] and deep["valid"]) else 1


def cmd_verify(args: argparse.Namespace) -> int:
    services = _services()
    deep = bool(getattr(args, "deep", False))

    if deep:
        _say(
            "Deep audit: re-verifying every ECDSA signature from scratch.\n"
            "Pure-Python verification costs about 25 ms per record, so this\n"
            "takes roughly 30 seconds per 1,000 records."
        )

    report = services.ledger.verify_chain(deep=deep)
    _rule("CHAIN VERIFICATION" + (" (DEEP)" if deep else ""))
    _say(f"Result   : {report['headline']}")
    _say(f"Blocks   : {report['checked_blocks']}")
    _say(f"Records  : {report['checked_transactions']}")
    if deep:
        _say(f"Signatures checked : {report['signatures_checked']}")
    _say(f"Duration : {report['duration_ms']:.0f} ms")
    if not deep:
        _say("\n(Structural checks. Add --deep to re-verify every signature.)")
    if report["issues"]:
        _say("\nIssues found:")
        for issue in report["issues"]:
            _say(f"  [{issue['severity']}] block {issue['block_index']}: "
                 f"{issue['code']} - {issue['message']}")
    return 0 if report["valid"] else 1


def cmd_tamper(args: argparse.Namespace) -> int:
    services = _services()
    chain = services.ledger.chain

    candidates = [
        tx
        for block in chain.chain
        for tx in block.transactions
        if tx.tx_type == "ATTENDANCE"
    ]
    if not candidates:
        _say("No attendance transactions on the chain yet. Run `python manage.py demo`.")
        return 1

    target = candidates[args.index % len(candidates)]
    _rule(f"TAMPER DEMO - LEVEL {args.level}")
    _say(chain.TAMPER_LEVELS[args.level])
    _say(f"\nTarget: {target.payload.get('student_roll')} in "
         f"{target.payload.get('subject_code')} "
         f"(session {target.payload.get('session_id')})")

    result = chain.tamper(
        tx_id=target.tx_id,
        field_name=args.field,
        new_value=args.value,
        level=args.level,
    )
    _say("\nAttacker's steps:")
    for step in result["steps"]:
        _say(f"  - {step}")
    _say(f"\nVERDICT: {result['verdict']}")
    _say("\nDetected by:")
    for issue in result["report"]["issues"]:
        _say(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")

    if args.persist:
        services.repo.save_blocks([b.to_dict() for b in chain.chain])
        _say("\nThe tampered chain was WRITTEN TO STORAGE so you can show that "
             "reloading it still fails validation.")
    return 0


def cmd_anchor(args: argparse.Namespace) -> int:
    services = _services()
    _rule("CREATING ANCHOR")
    _say(f"Provider: {services.anchoring.provider.name}")
    anchor = services.anchoring.anchor_now(note=args.note or "Manual anchor from CLI")
    _say(f"Anchor id     : {anchor['anchor_id']}")
    _say(f"Merkle root   : {anchor['merkle_root']}")
    _say(f"Records       : {anchor['records_anchored']}")
    _say(f"Blocks        : {anchor['from_block']}..{anchor['to_block']}")
    _say(f"Status        : {anchor['status']}")
    _say(f"Independent   : {anchor['independent']}")
    _say(f"Message       : {anchor['message']}")
    if anchor.get("explorer_url"):
        _say(f"Explorer      : {anchor['explorer_url']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    services = _services()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(services.ledger.chain.export_json(), encoding="utf-8")
    size = out.stat().st_size
    _say(f"Exported {services.ledger.chain.height} blocks to {out} ({size:,} bytes)")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from app.blockchain import proof_of_work as pow_module

    _rule("PROOF-OF-WORK BENCHMARK")
    for difficulty in range(1, args.max + 1):
        result = pow_module.benchmark(difficulty)
        _say(
            f"difficulty {difficulty}: {result.attempts:>10,} hashes  "
            f"{result.elapsed_seconds:>8.3f}s  "
            f"{result.hash_rate:>14,.0f} H/s   {result.block_hash[:20]}..."
        )
    _say(
        "\nThis is the same function Bitcoin uses. Bitcoin's difficulty is about "
        "10^23 hashes per block -- the algorithm is identical, only the target differs."
    )
    return 0


def cmd_firebase_check(args: argparse.Namespace) -> int:
    from app.storage import _rs256
    from app.storage.firestore_store import FirestoreStore

    config = load_config()
    _rule("FIREBASE / FIRESTORE DIAGNOSTICS")
    project = config.storage.firebase_project_id

    credentials = _rs256.load_service_account()
    if credentials is None:
        _say("No service-account JSON found. Looked for:")
        _say("  $FIREBASE_SERVICE_ACCOUNT  (the JSON itself)")
        _say("  $FIREBASE_SERVICE_ACCOUNT_PATH / $FIREBASE_CREDENTIALS")
        _say("  ./firebase-service-account.json")
        _say("  ./secrets/firebase-service-account.json")
        _say("\nCreate one: Firebase console -> Project settings -> Service accounts")
        _say("            -> Generate new private key")
        return 1

    _say(f"Service account : {credentials.get('client_email')}")
    _say(f"Project in key  : {credentials.get('project_id')}")
    _say(f"Configured id   : {project or '(not set)'}")

    if not project:
        project = credentials.get("project_id", "")
        _say(f"Using project id from the key file: {project}")

    try:
        _say("\nSigning a JWT and requesting an access token...")
        token, expiry = _rs256.service_account_access_token(credentials)
        _say(f"  Access token acquired (expires in {int(expiry - time.time())}s)")
    except Exception as exc:
        _say(f"  FAILED: {exc}")
        _say("\nCheck the system clock: Google rejects JWTs whose 'iat' is skewed "
             "by more than a few minutes.")
        return 1

    try:
        store = FirestoreStore(project, credentials=credentials)
        store.init()
        health = store.health()
        _say(f"\nFirestore reachable: {health['ok']}")
        for collection, count in health["collections"].items():
            _say(f"  {collection:<12}: {count}")
    except Exception as exc:
        _say(f"\nFirestore call failed: {exc}")
        return 1

    _say("\nEverything looks good. Set STORAGE_BACKEND=firestore (or 'auto') and restart.")
    return 0


def cmd_student(args: argparse.Namespace) -> int:
    from app.services.analytics import student_report

    services = _services()
    student = services.repo.get_student(args.roll.upper())
    if student is None:
        _say(f"No student with roll number {args.roll}")
        return 1
    report = student_report(
        student=student,
        subjects=[
            s
            for s in services.repo.list_subjects()
            if s.get("year") == student.get("year")
        ],
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
    )
    _rule(f"{student['name']} ({student['roll_no']})")
    _say(f"{'SUBJECT':<12}{'HELD':>6}{'ATT':>6}{'%':>8}  STATUS")
    for row in report["subjects"]:
        status = "OK" if row["eligible"] else f"SHORT by {75 - row['percentage']:.1f}%"
        _say(
            f"{row['subject_code']:<12}{row['held']:>6}{row['attended']:>6}"
            f"{row['percentage']:>8.1f}  {status}"
        )
    overall = report["overall"]
    _say("-" * 78)
    _say(
        f"{'OVERALL':<12}{overall['held']:>6}{overall['attended']:>6}"
        f"{overall['percentage']:>8.1f}  "
        f"{'ELIGIBLE' if overall['eligible'] else 'NOT ELIGIBLE (<75%)'}"
    )
    return 0


def cmd_defaulters(args: argparse.Namespace) -> int:
    from app.services.analytics import defaulter_list

    services = _services()
    rows = defaulter_list(
        students=services.repo.list_students(),
        subjects=services.repo.list_subjects(),
        sessions=services.repo.list_sessions(),
        attendance=services.repo.attendance_all(),
    )
    _rule(f"DEFAULTERS BELOW 75% ({len(rows)})")
    _say(f"{'ROLL':<14}{'NAME':<24}{'HELD':>5}{'ATT':>5}{'%':>8}{'SHORT':>8}")
    for row in rows[: args.limit]:
        _say(
            f"{row['roll_no']:<14}{row['name']:<24}{row['held']:>5}{row['attended']:>5}"
            f"{row['percentage']:>8.1f}{row['shortfall']:>8.1f}"
        )
    if not rows:
        _say("No defaulters -- every student is above 75%.")
    return 0


def cmd_anomalies(args: argparse.Namespace) -> int:
    from app.services.anomaly import scan

    services = _services()
    result = scan(
        attendance=services.repo.attendance_all(),
        sessions=services.repo.list_sessions(),
    )
    _rule(f"ANOMALY SCAN - {result['alert_count']} ALERT(S)")
    for severity, count in result["by_severity"].items():
        _say(f"  {severity:<8}: {count}")
    for alert in result["alerts"][: args.limit]:
        _say(f"\n[{alert['severity']}] {alert['rule']}")
        _say(f"   {alert['message']}")
        if alert.get("evidence"):
            _say(f"   evidence: {json.dumps(alert['evidence'])[:160]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    services = _services()
    status = services.status()
    _rule("SYSTEM STATUS")
    _say(f"Storage backend : {status['storage']['backend']}")
    for note in status["storage"]["report"].get("notes", []):
        _say(f"                  - {note}")
    _say(f"Chain blocks    : {status['chain']['blocks']}")
    _say(f"Chain height    : {status['chain']['height']}")
    _say(f"Difficulty      : {status['chain']['difficulty']}")
    _say(f"Attendance txs  : {status['chain']['sealed_attendance_transactions']}")
    _say(f"Anchor provider : {status['anchor']['provider']}")
    _say(f"Register        : {status['register']}")
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    """Verify the cryptographic primitives against published test vectors."""
    from app.blockchain import ecdsa, keccak
    from app.blockchain.merkle import merkle_proof, merkle_root, verify_merkle_proof

    _rule("SELF-TEST: CRYPTOGRAPHIC PRIMITIVES")
    failures = 0

    # 1. ECDSA: private key 1 must give the well-known secp256k1 point.
    keypair = ecdsa.keypair_from_private(1)
    expected_pub = (
        "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    )
    ok = keypair.public_hex == expected_pub
    failures += not ok
    _say(f"[{'PASS' if ok else 'FAIL'}] ECDSA public key for private key 1")
    _say(f"       address = {keypair.address}")

    # 2. Sign / verify round trip, and rejection of a modified message.
    message = b"BCOE attendance test vector"
    signature = ecdsa.sign(keypair.private_hex, message)
    ok = ecdsa.verify(keypair.public_hex, message, signature)
    ok2 = not ecdsa.verify(keypair.public_hex, message + b"!", signature)
    failures += not (ok and ok2)
    _say(f"[{'PASS' if ok and ok2 else 'FAIL'}] ECDSA sign/verify and tamper rejection")

    # 3. Keccak-256 (Ethereum) published vectors.
    vectors = [
        (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
        (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    ]
    ok = all(keccak.keccak256_hex(data) == expected for data, expected in vectors)
    failures += not ok
    _say(f"[{'PASS' if ok else 'FAIL'}] Keccak-256 (Ethereum) test vectors")

    # 4. The ERC-20 selector everyone can check by hand.
    ok = keccak.function_selector("transfer(address,uint256)") == "a9059cbb"
    failures += not ok
    _say(f"[{'PASS' if ok else 'FAIL'}] keccak256('transfer(address,uint256)') == a9059cbb")

    # 5. Merkle inclusion proof.
    leaves = [f"tx-{index}" for index in range(7)]  # odd count exercises duplication
    root = merkle_root(leaves)
    proofs_ok = all(
        verify_merkle_proof(leaf, merkle_proof(leaves, index), root)
        for index, leaf in enumerate(leaves)
    )
    failures += not proofs_ok
    _say(f"[{'PASS' if proofs_ok else 'FAIL'}] Merkle proofs validate for all leaves")

    # 6. Proof-of-Work.
    from app.blockchain import proof_of_work as pow_module

    mine_result = pow_module.mine(
        {"index": 1, "timestamp": 0.0, "prev_hash": "0" * 64, "merkle_root": "0" * 64,
         "difficulty": 3, "miner": "test"},
        difficulty=3,
    )
    ok = pow_module.meets_difficulty(mine_result.block_hash, 3)
    failures += not ok
    _say(f"[{'PASS' if ok else 'FAIL'}] Proof-of-Work found {mine_result.block_hash[:16]}... "
         f"in {mine_result.attempts:,} attempts")

    # 7. QR token signing.
    from app.services import qr

    secret = qr.new_session_secret()
    token = qr.issue_token("LEC-TEST-001", secret, 30)
    verified = qr.verify_token(token.token, secret=secret, session_id="LEC-TEST-001", ttl=30)
    ok = verified["fresh"] is True
    failures += not ok
    try:
        qr.verify_token(token.token, secret=secret, session_id="LEC-TEST-999", ttl=30)
        cross_session_blocked = False
    except qr.QRTokenError:
        cross_session_blocked = True
    failures += not cross_session_blocked
    expiry_ok = True
    try:
        qr.verify_token(
            token.token, secret=secret, session_id="LEC-TEST-001", ttl=30,
            at=time.time() + 300,
        )
        expiry_ok = False
    except qr.QRTokenError:
        pass
    failures += not expiry_ok
    _say(f"[{'PASS' if ok and cross_session_blocked and expiry_ok else 'FAIL'}] "
         "QR tokens: fresh accepted, expired rejected, cross-session rejected")

    _rule()
    if failures:
        _say(f"{failures} self-test(s) FAILED")
        return 1
    _say("All self-tests passed -- cryptography and consensus primitives verified.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from app import create_app

    config = load_config()
    if args.port:
        config.port = args.port
    if args.debug:
        config.debug = True
    app = create_app(config)
    banner = config.college
    _rule("STARTING SERVER")
    _say(f"{banner.short_name} Blockchain Attendance System")
    _say(f"http://127.0.0.1:{config.port}")
    _say(f"Storage: {app.config['STORAGE_BACKEND_NAME']}  |  "
         f"Chain difficulty: {config.chain.difficulty}")
    app.run(host=config.host, port=config.port, debug=config.debug, threaded=True)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    from app.config import ROOT

    config = load_config()
    targets = [
        ROOT / config.storage.local_path,
        ROOT / "data" / "keystore" / "keys.json",
    ]
    if not _confirm(
        "This deletes the local ledger and ALL private keys. Continue?", args.yes
    ):
        _say("Cancelled.")
        return 1
    for target in targets:
        if target.exists():
            target.unlink()
            _say(f"Deleted {target}")
    _say("Done. Run `python manage.py seed` to start again.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="BCOE Blockchain Attendance System - management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="initialise storage and the genesis block")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("seed", help="create students, faculty, subjects and key pairs")
    p.add_argument("--students", type=int, default=60, help="students per year (default 60)")
    p.add_argument("--force", action="store_true", help="reseed even if data exists")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("demo", help="generate six weeks of realistic attendance history")
    p.add_argument("--weeks", type=int, default=6)
    p.add_argument("--year", default="BE", help="TE or BE")
    p.add_argument("--subjects", nargs="*", help="limit to these subject codes")
    p.add_argument("--clean", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="start the web application")
    p.add_argument("--port", type=int)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("verify", help="re-verify every block and transaction")
    p.add_argument(
        "--deep",
        action="store_true",
        help="also re-verify every ECDSA signature (slow: ~25 ms per record)",
    )
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("tamper", help="tamper with a record and show detection (demo)")
    p.add_argument("--level", type=int, default=1, choices=[1, 2, 3, 4])
    p.add_argument("--index", type=int, default=0, help="which record to attack")
    p.add_argument("--field", default="status")
    p.add_argument("--value", default="PRESENT")
    p.add_argument("--persist", action="store_true", help="write the tampered chain to storage")
    p.set_defaults(func=cmd_tamper)

    p = sub.add_parser("anchor", help="anchor pending records to the public ledger")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_anchor)

    p = sub.add_parser("export", help="export the whole chain as JSON")
    p.add_argument("--out", default="data/chain_export.json")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("bench", help="benchmark Proof-of-Work on this machine")
    p.add_argument("--max", type=int, default=5)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("firebase-check", help="diagnose the Firestore connection")
    p.set_defaults(func=cmd_firebase_check)

    p = sub.add_parser("student", help="print one student's attendance report")
    p.add_argument("roll")
    p.set_defaults(func=cmd_student)

    p = sub.add_parser("defaulters", help="list students below 75%")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_defaulters)

    p = sub.add_parser("anomalies", help="run the anti-fraud detectors")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_anomalies)

    p = sub.add_parser("status", help="show backend, chain and register status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("self-test", help="verify crypto primitives against known vectors")
    p.set_defaults(func=cmd_self_test)

    p = sub.add_parser("reset", help="delete the local ledger and keys")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        print(BANNER)
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except LedgerError as exc:
        print(f"\nRejected [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
