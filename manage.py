#!/usr/bin/env python3
"""Command line for the attendance system.

    python manage.py serve                     start the web app
    python manage.py seed                      create the 120-student register
    python manage.py demo                      register + four weeks of lectures
    python manage.py verify                    check every block hash
    python manage.py add-student ROLL "Name"   add one student
    python manage.py tamper                    edit the file, to demonstrate detection
    python manage.py reset                     delete everything and start again
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app                      # noqa: E402
from app.config import load_settings            # noqa: E402
from app.data import Database                   # noqa: E402
from app.seed import seed_demo, seed_register   # noqa: E402
from app.store import build_store               # noqa: E402


def build() -> tuple:
    settings = load_settings()
    store, report = build_store(settings)
    return settings, store, Database(settings, store), report


def cmd_serve(args) -> int:
    settings, store, database, report = build()
    application = create_app(settings, store)
    print(f"\n  {settings.college_name}")
    print(f"  Storage : {store.name}")
    for note in report.get("notes", []):
        print(f"            {note}")
    print(f"  Chain   : {len(database.chain.blocks)} blocks, difficulty {settings.difficulty}")
    print(f"  Open    : http://localhost:{settings.port}\n")
    application.run(host=settings.host, port=settings.port, debug=settings.debug, threaded=True)
    return 0


def cmd_seed(args) -> int:
    settings, store, database, _ = build()
    if database.data["students"] and not args.force:
        print(f"The register already has {len(database.data['students'])} students.")
        print("Use --force to add the demo register on top.")
        return 0
    count = seed_register(database, settings, args.count)
    print(f"Created {count} students in {store.name}.")
    return 0


def cmd_demo(args) -> int:
    settings, store, database, _ = build()
    started = time.time()
    result = seed_demo(database, settings, weeks=args.weeks)
    print(
        f"Created {result['students']} students, {result['sessions']} lectures and "
        f"{result['records']} attendance marks in {time.time() - started:.1f}s."
    )
    print(f"Blocks on the chain: {len(database.chain.blocks)}")
    print("\nStart the app with:  python manage.py serve")
    return 0


def cmd_verify(args) -> int:
    _, _, database, _ = build()
    ok, message = database.chain.is_valid()
    print(("PASS  " if ok else "FAIL  ") + message)
    for block in database.chain.blocks:
        print(
            f"  #{block.index:<3} {block.hash[:20]}...  "
            f"{len(block.records):>3} records  {block.timestamp and time.strftime('%d %b %H:%M', time.localtime(block.timestamp))}"
        )
    return 0 if ok else 1


def cmd_add_student(args) -> int:
    _, _, database, _ = build()
    student = database.add_student(args.roll_no, args.name, args.division, args.year)
    print(f"Added {student['roll_no']} - {student['name']}.")
    return 0


def cmd_tamper(args) -> int:
    """Edit one field inside one stored block, to show that verify catches it.

    This only makes sense on the local JSON file: it is the "somebody opened the
    database and changed a mark" story, and it is exactly what the hash chain is
    there to detect.
    """
    settings, store, _, _ = build()
    if store.name != "local-json":
        print("Tampering only works on the local JSON file (set STORAGE_BACKEND=local).")
        return 1

    path = settings.storage_path
    data = json.loads(path.read_text(encoding="utf-8"))
    blocks = data.get("blocks", {})
    block = blocks.get(str(args.block))
    if block is None:
        print(f"No block #{args.block} in {path}.")
        return 1
    if args.record >= len(block["records"]):
        print(f"Block #{args.block} has {len(block['records'])} records.")
        return 1

    record = block["records"][args.record]
    old = record.get(args.field)
    record[args.field] = args.value
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    print(f'Changed "{args.field}" in block #{args.block}, record {args.record}:')
    print(f'  was: {old!r}')
    print(f'  now: {args.value!r}')
    print('\nNow run:  python manage.py verify')
    return 0


def cmd_reset(args) -> int:
    _, store, database, _ = build()
    database.reset()
    print(f"Cleared the register and started a new chain in {store.name}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BCOE blockchain attendance system")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="start the web application").set_defaults(func=cmd_serve)

    seed = sub.add_parser("seed", help="create the demo register")
    seed.add_argument("--count", type=int, default=120)
    seed.add_argument("--force", action="store_true")
    seed.set_defaults(func=cmd_seed)

    demo = sub.add_parser("demo", help="register plus past lectures")
    demo.add_argument("--weeks", type=int, default=4)
    demo.set_defaults(func=cmd_demo)

    sub.add_parser("verify", help="check every block in the chain").set_defaults(func=cmd_verify)

    add = sub.add_parser("add-student", help="add one student")
    add.add_argument("roll_no")
    add.add_argument("name")
    add.add_argument("--division", default="A")
    add.add_argument("--year", default="BE")
    add.set_defaults(func=cmd_add_student)

    tamper = sub.add_parser("tamper", help="edit a stored record, to test detection")
    tamper.add_argument("--block", type=int, default=1)
    tamper.add_argument("--record", type=int, default=0)
    tamper.add_argument("--field", default="name")
    tamper.add_argument("--value", required=True)
    tamper.set_defaults(func=cmd_tamper)

    sub.add_parser("reset", help="clear everything").set_defaults(func=cmd_reset)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
