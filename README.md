# Blockchain Attendance System

**A mini project for CSDO7022 — Blockchain Technology**
Bharat College of Engineering (BCOE), Badlapur (W) · University of Mumbai · CSE (AI & ML)

Attendance is recorded in a hash-chained ledger. A lecturer opens a roll call, the
students scan a QR code that changes every 30 seconds, and when the roll call closes
every mark is sealed into a block. Change any mark afterwards and the block hash no
longer matches, so the chain stops verifying — and the system will tell you which
block was edited.

Written in **Python, HTML, CSS and JavaScript**, with no page frameworks: Flask
renders HTML on the server, and the only JavaScript is the camera, the live list and
the hash checks. The blockchain itself is about 200 lines of pure Python using
nothing but `hashlib`.

---

## The flow

1. **The lecturer opens a roll call** — subject, room, how long it stays open.
2. **A QR code appears** on the projector. It carries a signed token, and the token
   changes every `QR_TOKEN_TTL` seconds, so a screenshot goes stale immediately.
3. **The student scans it** with a phone camera (or types their roll number and
   opens the link directly) and is marked present.
4. **The lecturer closes the roll call.** Every mark from that lecture is sealed
   into a new block: the records are hashed into a Merkle root, the root goes into
   the block along with the previous block's hash, and proof of work is done.
5. **Anyone can check it later** — from `/verify`, or with `python manage.py verify`.

## What the blockchain actually buys you here

| Ordinary register | This project |
| --- | --- |
| A row in a database can be edited by whoever can open the database | Each block's hash covers all of its records, so an edit breaks the hash |
| No way to tell when history changed | `verify` names the first bad block |
| "Trust me, I was present" | A Merkle proof shows one mark is inside a sealed block without revealing the rest of the register |
| Anyone can mark anyone present from home | The QR token expires in 30 seconds and is signed, so it cannot be forged |

## Technology

* **Python 3.12** — Flask for the web pages, `hashlib` for SHA-256, `hmac` for the
  QR tokens, `qrcode` for drawing the QR code as SVG.
* **HTML + CSS** — one stylesheet, plain borders and one accent colour. No framework.
* **JavaScript** — about 200 lines: the phone camera (`jsQR`), the rotating QR code,
  the live list of who has marked, and the verify buttons.
* **Storage** — a JSON file by default. Firebase Firestore is supported for cloud
  deployments, over its REST API, using only the standard library.

No React, no Node, no build step. `pip install -r requirements.txt` and run.

## Project structure

```
app/
  blockchain.py   the chain: hashing, mining, Merkle trees, validation   (~280 lines)
  data.py         students, sessions, marking, sealing, QR tokens        (~300 lines)
  store.py        JSON file and Firestore backends                       (~510 lines)
  routes.py       every page and API endpoint                            (~300 lines)
  config.py       settings and subjects                                  (~130 lines)
  seed.py         the demo register and past lectures                     (~90 lines)
  templates/      nine HTML pages
  static/         style.css, app.js, vendor/jsQR.js
tests/            55 tests: the chain, the rules, and the whole flow
manage.py         command line
```

## Running it

```bash
pip install -r requirements.txt
python manage.py demo      # 120 students, four weeks of lectures, 13 blocks
python manage.py serve     # http://localhost:5000
```

`python manage.py seed` creates just the register, if you would rather build the
lectures yourself from the web pages.

### Commands

| Command | What it does |
| --- | --- |
| `python manage.py serve` | start the web app |
| `python manage.py demo --weeks 4` | register + past lectures, already sealed into blocks |
| `python manage.py seed` | the 120-student register only |
| `python manage.py add-student BCOE23AI121 "Name"` | add one student |
| `python manage.py verify` | check every block hash and link |
| `python manage.py tamper --block 2 --record 0 --field name --value "Someone Else"` | edit the stored file, to demonstrate detection |
| `python manage.py reset` | start again |
| `python -m pytest` | run the tests |

## Demonstrating that tampering is detected

This is the demo worth showing in the viva. Two terminals:

```bash
# 1. the chain is fine
python manage.py verify
#    PASS  The chain is intact: 13 blocks verified.

# 2. somebody edits the register behind your back
python manage.py tamper --block 2 --record 0 --field name --value "Someone Else"
#    Changed "name" in block #2, record 0:
#      was: 'Shreya Kulkarni'
#      now: 'Someone Else'

# 3. the chain now fails, and says where
python manage.py verify
#    FAIL  The records in block 2 do not match its Merkle root.
```

The same is on `/verify` in the browser, and `data/ledger.json` is a plain readable
file, so you can make the edit in a text editor instead — which is a better
demonstration, because nothing is hidden.

## Firebase (optional)

The app runs with no setup at all on the local JSON file. Firebase is only needed
if you deploy somewhere whose disk is wiped — Render's free plan, for example.

1. In the [Firebase console](https://console.firebase.google.com), create a project
   and a **Firestore database** (production mode is fine).
2. Project settings → **Service accounts** → *Generate new private key*. A JSON file
   downloads.
3. Give the app those details. Locally, put them in `.env`:

   ```
   STORAGE_BACKEND=auto
   FIREBASE_PROJECT_ID=your-project-id
   FIREBASE_SERVICE_ACCOUNT=firebase-service-account.json
   ```

   On a host, paste the **entire JSON on one line** into `FIREBASE_SERVICE_ACCOUNT`
   (newlines inside the private key are fine, they are escaped in JSON).

There are **no composite indexes to create**. The app never sends a Firestore query:
it lists documents and filters them in Python, which works on a brand-new project
with nothing configured. (The earlier version of this project used Firestore queries
and every page broke with `The query requires an index` — listing is what avoids it.)

If Firebase is unreachable or the service account is malformed, the app keeps running
on the local file and says so at the top of the page. A roll call must never be
blocked by a database problem.

## Deploying on Render

`render.yaml` is a Blueprint: connect the repository, pick the branch, and Render
builds `bcoe-attendance-chain` with gunicorn. The two Firebase values are marked
`sync: false`, so Render asks for them when you create the service — paste the
project id and the service-account JSON.

## Tests

```bash
python -m pytest        # 55 tests
```

* `tests/test_blockchain.py` — hashing, mining, linking, Merkle proofs, and tampering
  with a block in several different ways.
* `tests/test_attendance.py` — the QR token (fresh, stale, forged, other session),
  marking rules, sealing, and that everything survives a restart.
* `tests/test_web.py` — every page, plus the full journey: open a session, scan the
  code, mark, seal, prove.

## Limitations, and what could come next

* Marks are marked by the student, not the lecturer. An impostor who has the live QR
  code could mark a friend by typing their roll number. Signing each mark with a key
  held on the student's phone would close that gap; it was left out to keep the
  project the size a mini project should be.
* Proof of work at difficulty 4 takes a fraction of a second. That is enough to
  demonstrate the idea, not to secure anything.
* The chain is private to the college. Anchoring its Merkle roots to a public chain
  (Ethereum, or an OpenTimestamps calendar) would let an outsider confirm that a
  block existed on a given date.
* The whole dataset is loaded into memory at start-up. That is deliberate at this
  size (a college register) and is what allows Firestore to be used without indexes.

## Concepts from the syllabus

Hashing and SHA-256 · hash chaining and immutability · Merkle trees · Merkle proofs ·
proof of work and nonces · tamper detection · hash functions as commitments ·
QR-based authentication with expiring tokens · server-side rendering with Flask.

---

Bharat College of Engineering · Department of Computer Science & Engineering (AI & ML)
University of Mumbai · Academic year 2026-27
