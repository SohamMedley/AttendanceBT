"""
Central configuration.

Precedence (highest first): **environment variables -> config.json -> defaults.**

Keeping the college-specific values in one place means the system can be shown
working for any institute simply by editing ``config.json`` -- nothing is
hard-coded in the logic. Every deployment-specific value below is read exactly
once, at import time, and validated.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
DATA_DIR = ROOT / "data"


def _env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class CollegeConfig:
    """Institute metadata, taken from the BCOE public website."""

    name: str = "Bharat College of Engineering"
    short_name: str = "BCOE"
    address: str = "Kanhor, Badlapur (W), Thane, Maharashtra 421503"
    affiliation: str = "University of Mumbai"
    approvals: str = "Approved by AICTE and DTE, Government of Maharashtra"
    website: str = "https://bharatenggcollege.com"
    department: str = "Computer Science & Engineering (AI & ML)"
    programme: str = "B.E. Computer Science & Engineering (Artificial Intelligence & Machine Learning)"
    campus_latitude: float = 19.1549
    campus_longitude: float = 73.2462

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "short_name": self.short_name,
            "address": self.address,
            "affiliation": self.affiliation,
            "approvals": self.approvals,
            "website": self.website,
            "department": self.department,
            "programme": self.programme,
            "campus_latitude": self.campus_latitude,
            "campus_longitude": self.campus_longitude,
        }


@dataclass
class ChainConfig:
    """Blockchain parameters."""

    difficulty: int = 4
    seal_threshold: int = 25
    node_name: str = "BCOE-NODE-01"
    genesis_difficulty: int = 2
    #: Turn on to demonstrate difficulty retargeting in the UI.
    auto_retarget: bool = False
    target_block_seconds: float = 10.0


@dataclass
class SessionConfig:
    """Lecture session / QR token parameters."""

    qr_token_ttl: int = 30          # seconds a rotating QR payload stays valid
    session_default_minutes: int = 60
    grace_seconds: int = 300        # marks after this are LATE, not PRESENT
    late_allowed: bool = True
    min_scan_interval_ms: int = 1500  # basic rate limit against scripted abuse
    require_geofence: bool = False
    geofence_radius_m: int = 250


@dataclass
class AnchorConfig:
    """Public-chain anchoring parameters."""

    #: "simulated" (default) | "opentimestamps" | "ethereum" | "firebase-only"
    provider: str = "auto"   # auto = try OpenTimestamps, fall back to simulated
    network: str = "opentimestamps-calendar"
    ethereum_rpc_url: str = ""
    ethereum_chain_id: int = 11155111       # Sepolia testnet
    ethereum_contract: str = ""
    ethereum_private_key: str = ""
    submit_enabled: bool = False            # real network submission is opt-in
    calendar_urls: tuple[str, ...] = (
        "https://a.pool.opentimestamps.org",
        "https://b.pool.opentimestamps.org",
        "https://alice.btc.calendar.opentimestamps.org",
    )


@dataclass
class StorageConfig:
    """Which persistence backend to use."""

    backend: str = "auto"           # auto | firestore | local
    local_path: str = "data/attendance_ledger.json"
    firebase_project_id: str = ""
    firebase_database: str = "(default)"
    firebase_prefix: str = ""
    firebase_service_account: str = ""
    auto_sync_local_to_cloud: bool = True


@dataclass
class SecurityConfig:
    """Attendance fraud controls."""

    max_marks_per_minute_per_ip: int = 40
    max_marks_per_minute_per_student: int = 3
    bind_device_on_first_use: bool = True
    allow_manual_override: bool = True
    manual_override_requires_reason: bool = True


@dataclass
class AppConfig:
    college: CollegeConfig = field(default_factory=CollegeConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    anchoring: AnchorConfig = field(default_factory=AnchorConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "bcoe-attendance-chain-dev-secret"
    academic_year: str = "2026-27"

    def as_dict(self) -> dict[str, Any]:
        return {
            "college": self.college.as_dict(),
            "chain": self.chain.__dict__,
            "session": self.session.__dict__,
            "anchoring": self.anchoring.__dict__,
            "storage": {
                k: v for k, v in self.storage.__dict__.items()
                # never expose credential paths in API responses
                if k not in {"firebase_service_account"}
            },
            "security": self.security.__dict__,
            "debug": self.debug,
            "academic_year": self.academic_year,
        }


def _apply_section(target: Any, values: dict[str, Any]) -> None:
    """Copy known keys from a config file section onto a dataclass instance."""
    for key, value in (values or {}).items():
        if hasattr(target, key):
            current = getattr(target, key)
            # Keep the declared type for simple scalars.
            if isinstance(current, bool):
                setattr(target, key, bool(value))
            elif isinstance(current, int) and not isinstance(value, bool):
                try:
                    setattr(target, key, int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(current, float):
                try:
                    setattr(target, key, float(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(current, tuple) and isinstance(value, list):
                setattr(target, key, tuple(value))
            else:
                setattr(target, key, value)


def load_env_file(path: str | os.PathLike[str] | None = None) -> int:
    """Read a ``.env`` file into ``os.environ`` without overriding anything.

    A twelve-line parser rather than a dependency: the format people actually
    use is ``KEY=value`` with optional quotes and ``#`` comments. Real
    environment variables always win, which is what makes the same file safe on
    a laptop and on a host that injects its own configuration (Render, Docker,
    systemd). Returns the number of variables that were newly set.
    """
    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.is_file():
        return 0

    applied = 0
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted: everything up to the closing quote is the value, so a
            # comment may follow it on the same line.
            quote = value[0]
            closing = value.find(quote, 1)
            value = value[1:closing] if closing != -1 else value[1:]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied

def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Build the configuration object from file + environment."""
    load_env_file()
    config = AppConfig()
    file_path = Path(path) if path else CONFIG_FILE

    if file_path.is_file():
        with file_path.open("r", encoding="utf-8") as handle:
            try:
                raw = json.load(handle)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{file_path} is not valid JSON: {exc}") from exc
        for section in ("college", "chain", "session", "anchoring", "storage", "security"):
            if section in raw:
                _apply_section(getattr(config, section), raw[section])
        for key in ("debug", "host", "port", "secret_key", "academic_year"):
            if key in raw:
                setattr(config, key, raw[key])

    # ---- environment overrides (12-factor style) -------------------------
    config.debug = _env_bool("APP_DEBUG", config.debug)
    config.host = _env("HOST", config.host)
    config.port = _env_int("PORT", config.port)
    config.secret_key = _env("SECRET_KEY", config.secret_key)
    config.academic_year = _env("ACADEMIC_YEAR", config.academic_year)

    config.storage.backend = _env("STORAGE_BACKEND", config.storage.backend)
    config.storage.local_path = _env("LOCAL_STORE_PATH", config.storage.local_path)
    config.storage.firebase_project_id = _env(
        "FIREBASE_PROJECT_ID", config.storage.firebase_project_id
    )
    config.storage.firebase_database = _env(
        "FIRESTORE_DATABASE", config.storage.firebase_database
    )
    config.storage.firebase_prefix = _env("FIRESTORE_PREFIX", config.storage.firebase_prefix)
    config.storage.firebase_service_account = _env(
        "FIREBASE_SERVICE_ACCOUNT_PATH", config.storage.firebase_service_account
    )
    config.storage.auto_sync_local_to_cloud = _env_bool(
        "AUTO_SYNC_LOCAL_TO_CLOUD", config.storage.auto_sync_local_to_cloud
    )

    config.chain.difficulty = _env_int("CHAIN_DIFFICULTY", config.chain.difficulty)
    config.chain.seal_threshold = _env_int("CHAIN_SEAL_THRESHOLD", config.chain.seal_threshold)
    config.chain.node_name = _env("CHAIN_NODE_NAME", config.chain.node_name)

    config.session.qr_token_ttl = _env_int("QR_TOKEN_TTL", config.session.qr_token_ttl)
    config.session.grace_seconds = _env_int("GRACE_SECONDS", config.session.grace_seconds)
    config.session.geofence_radius_m = _env_int(
        "GEOFENCE_RADIUS_M", config.session.geofence_radius_m
    )
    config.session.require_geofence = _env_bool(
        "REQUIRE_GEOFENCE", config.session.require_geofence
    )

    config.anchoring.provider = _env("ANCHOR_PROVIDER", config.anchoring.provider)
    config.anchoring.submit_enabled = _env_bool(
        "ANCHOR_SUBMIT_ENABLED", config.anchoring.submit_enabled
    )
    config.anchoring.ethereum_rpc_url = _env(
        "ETHEREUM_RPC_URL", config.anchoring.ethereum_rpc_url
    )
    config.anchoring.ethereum_contract = _env(
        "ETHEREUM_CONTRACT", config.anchoring.ethereum_contract
    )
    config.anchoring.ethereum_private_key = _env(
        "ETHEREUM_PRIVATE_KEY", config.anchoring.ethereum_private_key
    )

    return config


#: Single shared instance used by the app and the CLI.
settings = load_config()


__all__ = [
    "AppConfig",
    "CollegeConfig",
    "ChainConfig",
    "SessionConfig",
    "AnchorConfig",
    "StorageConfig",
    "SecurityConfig",
    "load_config",
    "settings",
    "ROOT",
    "DATA_DIR",
    "CONFIG_FILE",
]
