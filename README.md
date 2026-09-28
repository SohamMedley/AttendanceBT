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

## Deploy on Render (Blueprint)

The repository includes `render.yaml`; no manual build or start command is needed.

1. Merge [PR #2](https://github.com/SohamMedley/AttendanceBT/pull/2) into `main` (including the Render configuration).
2. Sign in to [Render](https://dashboard.render.com/), then choose **New → Blueprint**.
3. Connect GitHub and select **SohamMedley/AttendanceBT**. Grant Render access to the repository if it is not listed.
4. Select branch **main**. Render should detect `render.yaml` at the repository root. To test before merging, select **arena/01a0ea3e-attendancebt** instead.
5. Give the Blueprint a name, review the free web service, and choose **Deploy Blueprint** (or **Apply**).
6. Wait for the build and health check to pass. Open the service's assigned **https://…onrender.com** URL.

Render generates `ATTENDANCE_SECRET` automatically. Do not put a real secret in `render.yaml` or commit it to Git. Keep the generated value stable; rotating it invalidates existing QR tokens. Render supplies `PORT` automatically.

### Verify the deployment

- Open `/healthz` on your service URL: it should return `{"status":"ok"}`.
- Open the dashboard and confirm the QR code loads and refreshes.
- Scan the QR code on your phone: it should open the same public **HTTPS** service URL with the check-in form.
- Submit a test student, seal the pending record, and try the same ID again: the duplicate should be rejected.
- Confirm the ledger says **Verified** and CSV export downloads.

### Important hosting limits

- **Demo only:** the free service has no persistent database. All records disappear on a restart, redeploy, or instance replacement. Free services can spin down while idle; the next request can take time to wake them.
- **Keep one worker and one instance.** Gunicorn runs one worker with four threads so every request shares the same in-memory ledger. Do not scale workers or instances until a shared database is added.
- **No login/access control:** anyone with the URL can view/export records, submit check-ins, and seal blocks. Use fictional student data on a public deployment.
- Gunicorn is the hosted HTTP server; Flask debug mode is off. The app trusts one proxy's forwarded protocol so Render-generated QR links use HTTPS. Only deploy behind a trusted reverse proxy with this configuration.

### Troubleshooting

| Symptom | What to check |
| --- | --- |
| Blueprint not found | Confirm the selected branch contains `render.yaml` at the repository root. |
| Build fails | Check Render's build logs; the build command must install `requirements.txt`. |
| Health check fails | Check runtime logs. The start command must bind to `0.0.0.0:$PORT`; the health path is `/healthz`. |
| First load is slow | The free instance may be waking up. Wait and retry. |
| QR expired | Scan the newly refreshed QR code and submit promptly. Tokens rotate every 15 seconds with one previous-window grace period. |
| Records disappeared | In-memory storage resets when the process restarts; this is expected for this demo. |

Future production deployment needs persistent storage, authentication, authorization, and abuse prevention before collecting real attendance data.
