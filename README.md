# Provex · AttendanceBT

A Flask attendance dashboard with rotating QR check-ins and SHA-256 hash-linked blocks.

## Run

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open port 5000. Scan the session QR code on another device to open the check-in form, or use **Record attendance**. Seal pending records into a block, search attendance, and export CSV.

```sh
.venv/bin/python -m unittest discover -s tests
```

## Scope

This is an in-memory, single-process demo, not a decentralized or production attendance service. Records reset on restart. Duplicate student IDs are rejected for the same UTC day, including after sealing. QR tokens rotate every 15 seconds with one previous-window grace period. Set `ATTENDANCE_SECRET` to supply a stable signing secret; otherwise a random secret is generated on startup. `PORT` defaults to 5000.

The server verifies block hashes, indexes, and links for the integrity indicator. There is no authentication or authorization: do not expose real student data or use this as a trusted production attendance system without adding access control and persistent storage.
