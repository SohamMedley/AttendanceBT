"""Settings for the attendance app.

Everything is read from the environment, so the same code runs on a laptop and
on Render. A small ``.env`` file is also read when present, which saves typing
the export lines on Windows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from ``.env`` into os.environ.

    Existing environment variables always win, and a value may be quoted (the
    Firebase service account JSON contains spaces and newlines, so it is usually
    written as a quoted single line).
    """
    path = path or BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# The class register. A mini project does not need a database of departments --
# one subject list is enough, and it is easy to read and change.
SUBJECTS = [
    {"code": "CSDO7022", "name": "Blockchain Technology", "faculty": "Prof. V. Tadkal", "division": "A"},
    {"code": "CSC701", "name": "Artificial Intelligence", "faculty": "Prof. S. Pawar", "division": "A"},
    {"code": "CSC702", "name": "Machine Learning", "faculty": "Prof. A. Shaikh", "division": "A"},
    {"code": "CSC703", "name": "Cloud Computing", "faculty": "Prof. R. More", "division": "A"},
    {"code": "CSC704", "name": "Software Engineering", "faculty": "Prof. K. Joshi", "division": "A"},
]


@dataclass
class Settings:
    """Runtime configuration."""

    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 5000))
    debug: bool = field(default_factory=lambda: _env_bool("APP_DEBUG", False))

    #: Used to sign the QR token and the Flask session cookie.
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-secret-change-me"))

    #: "auto" uses Firestore when credentials are present, otherwise a JSON file.
    storage_backend: str = field(default_factory=lambda: _env("STORAGE_BACKEND", "auto").lower())
    data_file: str = field(default_factory=lambda: _env("DATA_FILE", str(BASE_DIR / "data" / "ledger.json")))

    firebase_project_id: str = field(default_factory=lambda: _env("FIREBASE_PROJECT_ID", ""))
    #: Either the JSON itself, or a path to the JSON file.
    firebase_service_account: str = field(
        default_factory=lambda: _env("FIREBASE_SERVICE_ACCOUNT", "")
    )

    #: How many leading zeros a block hash must have (proof of work).
    difficulty: int = field(default_factory=lambda: _env_int("CHAIN_DIFFICULTY", 4))
    #: Seconds a QR code stays valid.
    qr_ttl: int = field(default_factory=lambda: _env_int("QR_TOKEN_TTL", 30))

    academic_year: str = field(default_factory=lambda: _env("ACADEMIC_YEAR", "2026-27"))

    college_name: str = "Bharat College of Engineering"
    college_short: str = "BCOE"
    college_department: str = "Computer Science & Engineering (AI & ML)"
    college_affiliation: str = "University of Mumbai"

    subjects: list = field(default_factory=lambda: [dict(s) for s in SUBJECTS])

    @property
    def storage_path(self) -> Path:
        return Path(self.data_file)

    def subject(self, code: str) -> dict | None:
        """Look up a subject by course code."""
        for subject in self.subjects:
            if subject["code"].upper() == str(code).upper():
                return subject
        return None


def load_settings() -> Settings:
    load_env_file()
    return Settings()
