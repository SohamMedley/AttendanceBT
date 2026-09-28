#!/usr/bin/env python3
"""
Convenience entry point.

    python run.py            # start the web application
    python manage.py <cmd>   # everything else (seed, demo, verify, tamper...)

Kept tiny on purpose: ``manage.py serve`` does the same thing with more
logging, and any WSGI server can host ``app:create_app()`` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app  # noqa: E402
from app.config import load_config  # noqa: E402


def main() -> int:
    config = load_config()
    application = create_app(config)

    print(
        f"\n  {config.college.name} - Blockchain Attendance System\n"
        f"  {config.college.affiliation}\n\n"
        f"  Listening on http://{config.host}:{config.port}\n"
        f"  Storage backend : {application.config['STORAGE_BACKEND_NAME']}\n"
        f"  Chain           : {len(application.config['SERVICES'].ledger.chain.chain)} blocks, "
        f"difficulty {config.chain.difficulty}\n"
    )

    application.run(
        host=config.host, port=config.port, debug=config.debug, threaded=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
