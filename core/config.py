"""Runtime configuration loaded from optional .env plus the process environment."""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
_LOADED_ENV_FILE: Path | None = None


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load a simple KEY=VALUE .env file with the Python standard library only."""
    global _LOADED_ENV_FILE
    candidate = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not candidate.is_file():
        return None
    for raw_line in candidate.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
    _LOADED_ENV_FILE = candidate
    return candidate


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip() or default


def _env_allow_empty(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(_env(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float, minimum: float = 1.0) -> float:
    try:
        return max(minimum, float(_env(name, str(default))))
    except ValueError:
        return default


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "1").strip().lower() in {"1", "true", "yes", "on"}


load_env_file()


# Supply a real key through the environment; this fallback is intentionally a placeholder.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY", "YOUR_DEEPSEEK_API_KEY")
DEEPSEEK_FAST_MODEL = _env("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")
DEEPSEEK_PLANNING_MODEL = _env("DEEPSEEK_PLANNING_MODEL", "deepseek-v4-pro")

# Optional OpenAI-compatible relay.
RELAY_BASE_URL = _env("RELAY_BASE_URL", "")
RELAY_API_KEY = _env("RELAY_API_KEY", "")
RELAY_FAST_MODEL = _env("RELAY_FAST_MODEL", "gpt5.5sol")
RELAY_PLANNING_MODEL = _env("RELAY_PLANNING_MODEL", "terra")

# The Tencent Cloud domestic gateway keeps the real Zhipu credential server-side.
# Override the gateway address or app secret through environment variables when
# deploying a private gateway; the defaults keep the contest demo cloneable.
GATEWAY_BASE_URL = _env_allow_empty(
    "EASIER_BANKING_GATEWAY_URL",
    "https://adverse-turkey-d1gf25ss51ce3e932-1518539214.ap-shanghai.app.tcloudbase.com",
)
# 这是评委体验用的受限凭证，网关侧已限速，赛后会作废。
GATEWAY_APP_SECRET = _env_allow_empty(
    "EASIER_BANKING_GATEWAY_APP_SECRET",
    "bank2026test",
)
GATEWAY_MODEL = "glm-4-flash"
CLOUD_RATE_LIMIT_PER_MINUTE = _int_env("EASIER_BANKING_CLOUD_RATE_LIMIT", 20)
CLOUD_TIMEOUT_SECONDS = min(8.0, _float_env("EASIER_BANKING_CLOUD_TIMEOUT_SECONDS", 8.0, 1.0))
CLOUD_FAILURE_THRESHOLD = _int_env("EASIER_BANKING_CLOUD_FAILURE_THRESHOLD", 3)
CLOUD_CIRCUIT_SECONDS = _int_env("EASIER_BANKING_CLOUD_CIRCUIT_SECONDS", 60)

SESSION_TTL_SECONDS = _int_env("EASIER_BANKING_SESSION_TTL_SECONDS", 8 * 60 * 60, 60)
LOGIN_MAX_FAILURES = _int_env("EASIER_BANKING_LOGIN_MAX_FAILURES", 5)
LOGIN_FAILURE_WINDOW_SECONDS = _int_env("EASIER_BANKING_LOGIN_FAILURE_WINDOW_SECONDS", 5 * 60, 30)
LOGIN_LOCK_SECONDS = _int_env("EASIER_BANKING_LOGIN_LOCK_SECONDS", 5 * 60, 30)
OTP_PEPPER = _env_allow_empty("OTP_PEPPER", "").strip() or secrets.token_urlsafe(48)


def gateway_configured(base_url: str | None = None, app_secret: str | None = None) -> bool:
    base = str(GATEWAY_BASE_URL if base_url is None else base_url).strip()
    secret = str(GATEWAY_APP_SECRET if app_secret is None else app_secret).strip()
    return bool(base and secret and not base.startswith("YOUR_") and not secret.startswith("YOUR_"))


def gateway_mode() -> str:
    return "cloud" if gateway_configured() else "offline"


# Provider routing order: Tencent Cloud gateway -> DeepSeek -> relay -> local.
MODEL_ROUTE = ("gateway", "primary", "secondary", "local")
ENABLE_LOCAL_FALLBACK = True

# Red/yellow/green transaction authorization gateway thresholds.
DAILY_SMALL_TRANSFER_LIMIT = 1000.0
MAX_AUTH_FAILURES = 3
RISK_SCORE_THRESHOLD = 100
