#!/usr/bin/env python3
"""Start the web app.

    python run.py

Same as ``python manage.py serve``, kept as a second entry point because some
hosting guides expect a file called run.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app            # noqa: E402
from app.config import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    application = create_app(settings)
    print(f"\n  {settings.college_name} - Blockchain Attendance System")
    print(f"  http://localhost:{settings.port}\n")
    application.run(host=settings.host, port=settings.port, debug=settings.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
