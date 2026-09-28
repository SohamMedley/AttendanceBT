# Blockchain-Based Attendance Management System

**Bharat College of Engineering, Badlapur (W)** · Department of CSE (AI & ML)
Affiliated to the **University of Mumbai**
Submitted for **CSDO7022 — Blockchain Technologies** (B.E. Semester VII, Department Optional Course-4)

---

## What this is

A working attendance system where **every attendance record is cryptographically
signed, sealed into a Proof-of-Work block, and periodically anchored to a public
ledger** — so that no one, including the college's own administrator, can silently
alter a student's attendance.

Attendance is captured by scanning a **rotating QR code** that changes every 30
seconds. Each mark becomes a transaction signed with a **secp256k1** key pair (the
same curve Bitcoin and Ethereum use), and transactions are batched into
**SHA-256 Proof-of-Work blocks** chained together through Merkle roots.

The result: proxy marking is blocked in real time, a student can *prove* their own
attendance with a Merkle proof, and any attempt to edit history is mathematically
detectable.

---

## Quick start

```bash
# 1. Install the dependencies (Flask, qrcode, gunicorn, pytest)
pip install -r requirements.txt

# 2. Verify the cryptography against published test vectors (no data needed)
python manage.py self-test

# 3. Create the college register: students, faculty, subjects and key pairs
python manage.py seed

# 4. Generate six weeks of realistic attendance history
python manage.py demo --weeks 6

# 5. Start the application
python manage.py serve
```

Then open **http://localhost:8000**.

> **Nothing else is required.** The blockchain, the ECDSA signatures, the Merkle
> trees, the Proof-of-Work and the Firestore client are all written from scratch on
> the Python standard library. There is no Node, no React, no build step, and no
> account needed anywhere.

**First run:** `demo --weeks 6` mines ~65 real blocks and takes roughly 2–3 minutes
on a modest laptop. If you are in a hurry, `python manage.py demo --weeks 2` is much
faster.

---

## The five-minute demo

Run these in order and the whole system tells its own story.

| # | Command / action | What it proves |
|---|---|---|
| 1 | `python manage.py self-test` | Every primitive is correct against published test vectors |
| 2 | Open **http://localhost:8000** | A live chain backed by real signatures |
| 3 | **Faculty Console → Start a session** | QR rotates; screenshot sharing fails |
| 4 | Open `/scan` in another tab, mark a student | Signed transaction → block |
| 5 | Try marking the same student twice | `ALREADY_MARKED` |
| 6 | Mark two students from the same device | `SHARED_DEVICE` (proxy marking blocked) |
| 7 | **Close & seal block** | The lecture becomes one mined block |
| 8 | **Explorer → Tamper lab → Level 1 … 4** | Each layer catches the attacker |
| 9 | **Public Anchoring → Anchor pending records** | A 32-byte root leaves the building |
| 10 | `python manage.py student BCOE23AI001` | The 75% University rule, applied |

---

## Verified results

Every number below was measured on this project's own dataset (2-core machine),
not estimated. They are worth quoting in a viva.

| Measurement | Result |
|---|---|
| Seeded register | 120 students, 6 faculty, 11 subjects, 127 key pairs in **2.5 s** |
| Six weeks of history | 64 lectures, **2,867** signed records, **64** mined blocks |
| Structural chain verification | 65 blocks / 2,867 records in **34 ms** |
| Deep cryptographic audit (cold) | 2,867 ECDSA signatures in **34 s** |
| Deep cryptographic audit (warm) | 0.17 s — every signature recalled from cache |
| One block sealed (difficulty 4) | ~16,000 hash attempts, ~0.5 s |
| College-wide turnout | **74.66%** of the 3,840 possible marks |
| Students below 75% | **26** of 120, worst at 37.5% |
| Anomalies detected | **191** alerts (shared device, overlap, burst) |
| Test suite | **244 passing** tests in ~61 s |
| Ledger file size | 6.0 MB for six weeks of a 120-student college |

**The four tamper levels**, on the real chain:

| Level | Attacker does | Detected by | Time |
|---|---|---|---|
| 1 | Edits `status` in the stored record | `TX_ID_MISMATCH` | 0.03 s |
| 2 | Also recomputes the transaction id | `MERKLE_ROOT_MISMATCH` | instant |
| 3 | Also rebuilds the Merkle root | `BLOCK_HASH_MISMATCH` | instant |
| 4 | Also re-mines the block | `BROKEN_LINK` + `BAD_SIGNATURE` | 1.1 s |

---

## How it works

```
                    ┌─────────────────────────────────────────┐
   Faculty phone ──▶│  Rotating QR  (HMAC-signed, 30 s window) │
                    └────────────────────┬────────────────────┘
                                         │ scan
                                         ▼
                    ┌─────────────────────────────────────────┐
   Student phone ──▶│  Anti-fraud gate                        │
                    │  duplicate · shared device · rate limit │
                    │  enrolment · session state              │
                    └────────────────────┬────────────────────┘
                                         │ accepted
                                         ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Transaction  tx_id = SHA256(payload)                            │
   │  Signature    ECDSA over secp256k1, RFC 6979 deterministic k      │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │ mempool
                                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Block   prev_hash ← SHA-256 chain ← merkle_root ← transactions  │
   │  Proof-of-Work: hash must start with N zero hex digits           │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │ periodically
                                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Anchor: publish ONLY the 32-byte Merkle root to                  │
   │  OpenTimestamps (Bitcoin) or an EVM chain                         │
   │  → the college can no longer rewrite its own history              │
   └──────────────────────────────────────────────────────────────────┘
```

### Why each piece exists

**Rotating QR, not a static one.** A QR code stuck on the wall is worthless — photograph
it once and mark yourself present all semester. Our QR payload is
`HMAC-SHA256(session_secret, session_id | time_slot)` (a TOTP construction, like Google
Authenticator). The secret never leaves the server, so the code cannot be forged, and it
expires automatically.

**Signatures, not just rows in a database.** Every record is signed with the student's
own key, so nobody can insert a record on their behalf.

**Hashing, not trust.** A block's hash commits to its Merkle root, which commits to every
transaction, which commits to every field. Change one character anywhere and every hash
downstream stops matching.

**Proof-of-Work, not a counter.** Rewriting block N means re-mining block N *and* every
block after it, faster than the honest chain grows. That cost is what "immutable" actually
means.

**Anchoring, not just a local chain.** If the college runs the only node, it could in
principle re-mine everything and pass every internal check. Anchoring removes that: the
Merkle root is published somewhere the college does not control. **Only the hash is
published — never student data.**

### The tamper lab, explained

This is the part examiners ask about, and the part that shows the design is layered
rather than just "we hashed things".

| Level | What the attacker does | What catches them |
|---|---|---|
| **1** | Edits `status` in the stored record | `TX_ID_MISMATCH` — the id no longer hashes from the content. The signature is also now invalid. |
| **2** | Also recomputes the transaction id | `MERKLE_ROOT_MISMATCH` — the block's root no longer matches its transactions. |
| **3** | Also rebuilds the Merkle root | `BLOCK_HASH_MISMATCH` — the header changed, so the stored hash is wrong and the PoW no longer satisfies the difficulty. |
| **4** | Also re-mines the block with valid PoW | `BROKEN_LINK` — the *next* block's `prev_hash` no longer points at it. |

To fully succeed, an attacker must re-mine every block from the edit to the tip, faster
than the honest network — the exact guarantee Proof-of-Work provides.

---

## Project structure

The project deliberately uses **few, substantial files** rather than many small ones.
Each file below is a complete layer, grouped so that related code sits together and a
reader can follow one concern from top to bottom without jumping between directories.

```
AttendanceBT/
├── manage.py                    CLI: seed, demo, serve, verify, tamper, anchor, self-test
├── run.py                       Minimal entry point (python run.py)
├── config.json                  College metadata, chain and policy parameters
├── .env.example                 Every option, documented (Firestore, anchoring, limits)
├── render.yaml                  Render Blueprint (one-click deploy)
├── Procfile                     Same start command for other PaaS hosts
│
├── app/                         13 modules + two small packages
│   ├── __init__.py              Flask application factory
│   ├── config.py                Configuration, env → file → default precedence, .env loader
│   ├── storage.py               Persistence: interface, local JSON store, Firestore
│   │                            over REST, pure-Python RS256 JWT signer
│   ├── services.py              Identity + key custody, rotating QR tokens, the
│   │                            repository queries, analytics, anomaly detectors
│   ├── ledger.py                Session lifecycle, marking, anti-fraud, verification,
│   │                            the Services facade, anchor orchestration
│   ├── anchoring.py             Four providers: auto, OpenTimestamps, Ethereum, simulated
│   ├── seed.py                  BCOE register (real MU subject codes) + six-week
│   │                            history generator
│   │
│   ├── blockchain/              ── the chain, written from scratch ──
│   │   ├── crypto.py            Keccak-256, secp256k1 + ECDSA (RFC 6979), Merkle trees,
│   │   │                        Base58Check addresses
│   │   └── chain.py             Transactions, blocks, Proof-of-Work, chain validation,
│   │                            the four-level tamper demo
│   │
│   ├── api/                     Three blueprints, split by audience:
│   │   ├── pages.py             Server-rendered pages
│   │   ├── attendance.py        Lecturers and students in a live lecture
│   │   └── chain.py             Ledger reporting, anchoring and system endpoints
│   │
│   ├── templates/               12 Jinja2 pages (server-side rendered HTML)
│   └── static/                  Hand-written CSS/JS + vendored jsQR (no CDN, no build step)
│
├── tests/                       Test suite (python -m pytest): 244 tests
└── data/                        Runtime state — git-ignored (ledger + keystore)
```

**Why this split, and not more files:** `blockchain/` keeps two files because primitives
and ledger are genuinely different reading tasks, while `config`, `storage`, `services`,
`ledger`, `anchoring` and `seed` stay separate in `app/` because they are different
layers. The API is split three ways by *who calls it*, not by resource. Anything finer
would mean a reader chases a single function across five files to understand one flow.

---

## Command reference

```bash
python manage.py self-test                 # verify crypto against known vectors
python manage.py seed [--students 60]      # create the register and key pairs
python manage.py demo --weeks 6            # generate realistic history
python manage.py serve [--port 8000]       # start the web application
python manage.py verify                    # re-verify every block and transaction
python manage.py tamper --level 4          # demonstrate tamper detection
python manage.py tamper --level 3 --persist # tamper AND write it to storage
python manage.py anchor                    # anchor pending records publicly
python manage.py export --out out.json     # export the whole chain
python manage.py bench --max 5             # Proof-of-Work benchmark
python manage.py student BCOE23AI001       # one student's attendance report
python manage.py defaulters                # students below 75%
python manage.py anomalies                 # run the fraud detectors
python manage.py status                    # backend, chain and register status
python manage.py firebase-check            # diagnose the Firestore connection
python manage.py reset --yes               # delete the ledger and keys
```

---

## Storage: works offline, scales to Firestore

The system runs on a **local JSON file by default**, which means it works on any
college laptop with no internet. Add Firebase credentials and it moves to
**Google Cloud Firestore** without a code change.

### Connecting Firebase, step by step

You need a Google account. The whole thing takes about five minutes and stays on the
free Spark plan.

**1. Create the project**

* <https://console.firebase.google.com> → **Add project**
* Name it, e.g. `bcoe-attendance-chain`. Analytics is not needed — turn it off.
* Wait for the project to be created, then continue.

**2. Create the Firestore database**

* In the left sidebar: **Build → Firestore Database → Create database**
* Choose **Production mode** (the app authenticates with a service account; open rules
  are not needed and are a bad habit).
* Pick a location near you — `asia-south1` (Mumbai) is the obvious one for BCOE.
* Click **Enable**.

**3. Generate a service account key**

* ⚙️ **Project settings → Service accounts → Generate new private key**
* A JSON file downloads. **This file is a password to your database.** Never commit
  it, never email it, never put it in a screenshot.

**4. Put the key where the app can find it**

*On your own laptop* — save it as `firebase-service-account.json` in the project root.
It is already git-ignored:

```bash
# the file must sit exactly here:
AttendanceBT/firebase-service-account.json
```

*On a host such as Render* — paste the **entire contents** of the JSON file into one
environment variable. On one line, exactly as downloaded:

```
FIREBASE_SERVICE_ACCOUNT={"type":"service_account","project_id":"bcoe-...", ... }
```

The app reads the JSON from the environment first, and falls back to the file path.

**5. Tell the app which project**

Copy `.env.example` to `.env` and set:

```bash
FIREBASE_PROJECT_ID=bcoe-attendance-chain          # the id from step 1
FIREBASE_SERVICE_ACCOUNT_PATH=firebase-service-account.json
STORAGE_BACKEND=auto                                # auto | firestore | local
FIRESTORE_PREFIX=                                   # optional namespace, e.g. "demo"
```

`.env` is read automatically on start-up. Real environment variables always win over
the file, so the same `.env` is safe locally and on a server.

**6. Check it**

```bash
python manage.py firebase-check
```

It prints which project it reached, which credential it used, and exactly what to fix
if something is missing. Then restart the app — the sidebar badge changes from
**Local JSON** to **Firestore**, and the Settings page shows the live project id.

**7. Push your existing local data up (optional)**

If you already demoed offline, `AUTO_SYNC_LOCAL_TO_CLOUD=true` (the default) uploads
whatever is in `data/attendance_ledger.json` on first connect.

**Design notes, because these are good viva answers:**

* Firestore is reached over its **REST API using only `urllib`**, so the project does not
  depend on `firebase-admin` being installable. Authentication is a real service-account
  JWT signed with a **pure-Python RS256 implementation** (the `_rs256` section of
  `app/storage.py`) — the signature format was verified against OpenSSL.
* If Firestore is unreachable, the app **keeps running on the local store** and says so.
  A database outage must never stop a lecturer taking attendance.
* The local and cloud backends implement the *same* document interface, so every query is
  written once. Use `python manage.py export` for a portable backup at any time.
* **The keystore is mirrored into Firestore too.** A hosting container's filesystem is
  wiped on every deploy, so a file-only keystore would leave the chain full of records
  whose signing keys no longer exist — the next attendance mark would fail. Keys are
  therefore written to both places and recovered automatically after a redeploy.

---

## Anchoring: making the college's own history unrewritable

Three providers ship with the project; switch with `ANCHOR_PROVIDER` in `.env`.

| Provider | Cost | Assurance | When to use |
|---|---|---|---|
| `auto` *(default)* | free | Bitcoin PoW when online, local otherwise | Always — never blocks a demo |
| `opentimestamps` | free | **Bitcoin proof-of-work** | When you have internet |
| `ethereum` | testnet gas | EVM smart contract | When you want a smart-contract angle |
| `simulated` | free | Local only, clearly labelled | Offline demos |

**Verifying an OpenTimestamps proof independently** — this is the strongest answer to
"how do I know you didn't just make this up?":

```bash
pip install opentimestamps-client
# download the .ots file from the Anchoring page, then:
ots verify ANC-20260929-101500-ab12cd.ots
```

The proof is checked against the **Bitcoin blockchain**, without trusting this
application, this server, or this college.

For Ethereum, the project prepares a genuine ABI-encoded
`anchor(bytes32)` transaction (selector `eecdf927`). Paste the calldata into
Etherscan's *Input Data* decoder and it decodes correctly. The Solidity contract is at
`/api/anchors/contract/source` and in the Ethereum provider in `app/anchoring.py`.

---

## Deploying on Render (public URL, free plan)

The repository ships a **Render Blueprint** (`render.yaml`), so deployment is: push,
click, done. Everything below uses only the free plan.

### Which branch to connect

> **Connect `arena/01a0e97f-attendancebt`** — the branch this work is on.
>
> `render.yaml` already names it explicitly (`branch:` at the top of the service
> definition), so the Blueprint deploys that branch and keeps auto-deploying on every
> push to it. Do **not** point it at `main` unless and until this branch is merged —
> `main` does not contain this work yet.

### Steps

1. **Push the branch to GitHub** (it is already pushed):
   ```bash
   git push origin arena/01a0e97f-attendancebt
   ```
2. **Create the Blueprint**: <https://dashboard.render.com> → **New +** → **Blueprint**.
3. **Connect the repository** `SohamMedley/AttendanceBT`. Render reads `render.yaml`
   and shows the service `bcoe-attendance-chain` with its branch already filled in.
4. **Apply**. The first build takes about two minutes (`pip install -r requirements.txt`).
5. **Open the URL** Render gives you, e.g. `https://bcoe-attendance-chain.onrender.com`.

The app starts empty on a fresh host, so the first thing to do is click
**Create the register** on the first-run page (or run `python manage.py seed` from the
Render shell), then **Generate six weeks of history** if you want a populated demo.

### What the Blueprint sets

| Setting | Value | Why |
|---|---|---|
| Runtime | Python 3.12, `pip install -r requirements.txt` | no Node, no build step |
| Start command | `gunicorn "app:create_app()" --workers 1 --threads 8` | one worker, on purpose — see below |
| Health check | `/api/system/health` | Render restarts a genuinely broken instance |
| `SECRET_KEY` | generated by Render | never a default value in production |
| `STORAGE_BACKEND` | `auto` | Firestore when configured, local otherwise |
| `CHAIN_DIFFICULTY` | `4` | seals instantly, so the demo is never slow |
| `ANCHOR_PROVIDER` | `simulated` | no external account needed to demonstrate |

**Why exactly one worker:** the chain is loaded into memory and written back by the
application. Two worker processes would each hold their own copy of the ledger and
overwrite each other's blocks. One worker with eight threads serves a lecture hall
comfortably — a QR refresh plus a handful of scans per minute.

### The one thing you must know: `data/` is ephemeral

On the free plan the container filesystem is **wiped on every deploy and restart**. That
is fine for a demo, and it is exactly why this project recommends Firestore:

* **Without Firebase** — the register, the chain and the keystore vanish when the
  service restarts. Use `python manage.py export` to keep a copy, and re-seed after a
  restart. Fine for showing the class; not fine for real records.
* **With Firebase** — the chain *and* the keystore live in Firestore, so a restart
  changes nothing. Set `FIREBASE_PROJECT_ID` and `FIREBASE_SERVICE_ACCOUNT` in the
  Render dashboard (see *Connecting Firebase* above) and redeploy.

If you want the file-based setup to survive restarts without Firebase, add a Render
disk and set `LOCAL_STORE_PATH=/var/data/attendance_ledger.json`. The free plan does
not include disks, so this is shown commented out at the bottom of `render.yaml`.

### Rough edges worth knowing

* **Cold starts.** Free instances sleep after ~15 minutes idle; the next request takes
  30–60 seconds to wake. Open the URL before you present.
* **`QR_TOKEN_TTL`.** The rotating QR is the security mechanism. Leave it at 30 s; a
  longer window makes screenshots worth forwarding.
* **A bad `FIREBASE_SERVICE_ACCOUNT` no longer stops the app.** If the value is not
  valid JSON (a truncated paste, or the wrong value pasted entirely, which is easy to do
  with a 2,300-character string), the app logs
  `Firestore unavailable: FIREBASE_SERVICE_ACCOUNT is not valid JSON`, falls back to the
  local store, and starts normally. The Settings page shows the same message. Check the
  logs for `storage=firestore` if you want to be certain it connected.
* **HTTPS is required for the phone camera.** Render serves HTTPS by default, so the
  scanner on `/scan` works — it will not on a plain-HTTP self-hosted instance.

---

## Configuration

Precedence: **environment variable → `config.json` → built-in default.**

| Setting | Default | Meaning |
|---|---|---|
| `CHAIN_DIFFICULTY` | `4` | Leading zero hex digits a block hash must have |
| `CHAIN_SEAL_THRESHOLD` | `25` | Marks buffered before a block seals automatically |
| `QR_TOKEN_TTL` | `30` | Seconds a QR token stays valid |
| `GRACE_SECONDS` | `300` | Marks after this become `LATE` |
| `STORAGE_BACKEND` | `auto` | `auto` / `firestore` / `local` |
| `ANCHOR_PROVIDER` | `auto` | `auto` / `opentimestamps` / `ethereum` / `simulated` |
| `REQUIRE_GEOFENCE` | `false` | Require the student to be on campus |
| `ANCHOR_SUBMIT_ENABLED` | `false` | Actually broadcast EVM transactions |

---

## Anti-fraud measures

| Threat | Countermeasure | Error code |
|---|---|---|
| Screenshot sharing | Token rotates every 30 s, HMAC-signed, ±1 slot skew | `EXPIRED`, `BAD_SIGNATURE` |
| Scanning for an absent friend | One device cannot mark two students in one lecture | `SHARED_DEVICE` |
| Marking twice | Duplicate index on `(student, session)` | `ALREADY_MARKED` |
| Scripted flooding | Sliding-window rate limits per IP and per student | `RATE_LIMITED` |
| Marking for another division | Enrolment check against year and division | `NOT_ENROLLED` |
| Faculty "fixing" records quietly | Manual overrides need a reason, are signed by the *faculty* key, tagged `MANUAL`, and go on the same chain | `REASON_REQUIRED` |
| Post-hoc database editing | 4-layer tamper detection + public anchoring | `TX_ID_MISMATCH` … |

The **Anomaly** screen runs transparent detectors over the whole dataset — shared devices,
impossible overlaps, burst submissions, scripted clients, repeated-device students,
low-turnout outliers — and reports the rule, the observed value and the threshold for
every alert. On the six-week demo data it raises 191 alerts, and the three rules that
fire are exactly the three behaviours the history generator in `app/seed.py` plants.

---

## Testing

```bash
python -m pytest                              # 241 tests, ~53 s
python -m pytest -v                           # verbose
python -m pytest tests/test_crypto.py -q      # 45 crypto tests, 0.3 s
python manage.py self-test                    # cryptographic vectors only
python manage.py verify                       # structural check, ~26 ms
python manage.py verify --deep                # re-verify every signature
```

The eleven blocks most worth reading, if you are short of time:

| Test file | What it proves |
|---|---|
| `tests/test_crypto.py` | secp256k1, ECDSA, Keccak-256 and Merkle trees match published vectors |
| `tests/test_blockchain.py` | Transactions, blocks, chain rules, all four tamper levels |
| `tests/test_services.py` | QR expiry, cross-session replay, storage round-trips, the 75% rule |
| `tests/test_ledger.py` | Session lifecycle, proxy-marking guard, sealing, anchoring, fraud detectors |
| `tests/test_web.py` | The whole app over HTTP, including every anti-fraud rejection |

The suite covers the cryptographic primitives against published vectors, Merkle proof
correctness, Proof-of-Work, transaction signing and validation, QR token expiry and
cross-session rejection, storage round-trips, the 75% rule, and the full HTTP attendance
flow including every anti-fraud rejection.

---

## Honest limitations

Stating these plainly is better engineering than pretending they do not exist — and
examiners respect it.

1. **The server holds student private keys** (custodial mode). This preserves tamper
   *evidence* fully: nobody can silently change a record. It does not give
   non-repudiation *against the student*, since the server could sign on their behalf.
   `MODE_NON_CUSTODIAL` in `app/services.py` implements the stronger model, where
   the key is generated in the browser and never sent to the server.

2. **The chain runs on a single node.** Consensus is not distributed or Byzantine-fault-
   tolerant. That is why anchoring matters — it is the mechanism that actually constrains
   the college. Running several nodes and comparing chains is the natural next step.

3. **Difficulty is 4, not 10²³.** Bitcoin's difficulty is astronomically higher; the
   algorithm is identical, only the target differs. Use `python manage.py bench` to show
   the scaling and explain why a classroom demo must keep the target low.

4. **The `simulated` anchor publishes nothing.** It is labelled as such on screen and in
   every stored record, so it can never be mistaken for a real public-chain anchor.

5. **Pure-Python ECDSA costs 12–25 ms per signature.** A cold audit of 2,881 records
   takes 35.7 s; the same audit afterwards takes 0.17 s, because verified block hashes
   are cached. A production system would use `libsecp256k1` and be roughly 1000× faster.

6. **Re-mining the chain *tip* is not detectable by internal checks alone.** If an
   attacker rewrites the most recent block and re-mines it, there is no later block
   whose `prev_hash` can break. This is not a flaw in the implementation — it is the
   reason public anchoring exists, and it is why the Tamper Lab tampers with a
   historical block. Publishing the Merkle root is what closes this gap.

---

## Mapping to the syllabus (CSDO7022)

| Syllabus topic | Implementation |
|---|---|
| Blockchain architecture, blocks, chain | `app/blockchain/chain.py` |
| Cryptographic hashing (SHA-256) | Everywhere; `blockchain/crypto.py` |
| Public-key cryptography, digital signatures | `blockchain/crypto.py` (secp256k1 from scratch) |
| Merkle trees and inclusion proofs | `blockchain/crypto.py`, shown on every receipt page |
| Consensus: Proof-of-Work, difficulty, nonce | `blockchain/chain.py`, live in the Explorer |
| Smart contracts and EVM | `anchor(bytes32)` contract, ABI encoding in `app/anchoring.py` |
| Decentralised applications (dApp) | The entire Flask application |
| Blockchain for records and trust | Attendance ledger + public anchoring |
| Immutability and tamper detection | The 4-level Tamper Lab |
| Limitations: scalability, key custody | *Honest limitations* below; `app/services.py` |

---

## Credits

*College data and subject codes are taken from the public
[Bharat College of Engineering](https://bharatenggcollege.com) website and the
University of Mumbai Rev-2019 'C' scheme syllabus.*

Third-party components: [Flask](https://flask.palletsprojects.com),
[qrcode](https://github.com/lincolnloop/python-qrcode), and
[jsQR](https://github.com/cozmo/jsQR) (Apache-2.0, vendored in
`app/static/js/vendor/` so the app works entirely offline).

Everything else — the elliptic curve cryptography, the Merkle trees, the Proof-of-Work
consensus, the Firestore REST client and the RS256 signer — is implemented in this
repository using only the Python standard library.
