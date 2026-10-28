"""Runtime configuration for model routing and transaction risk controls."""

import os


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


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
GATEWAY_BASE_URL = _env(
    "EASIER_BANKING_GATEWAY_URL",
    "https://adverse-turkey-d1gf25ss51ce3e932-1518539214.ap-shanghai.app.tcloudbase.com",
)
GATEWAY_APP_SECRET = _env(
    "EASIER_BANKING_GATEWAY_APP_SECRET",
    "bank2026test",
)
GATEWAY_MODEL = "glm-4-flash"

# Provider routing order: Tencent Cloud gateway -> DeepSeek -> relay -> local.
MODEL_ROUTE = ("gateway", "primary", "secondary", "local")
ENABLE_LOCAL_FALLBACK = True

# Red/yellow/green transaction authorization gateway thresholds.
DAILY_SMALL_TRANSFER_LIMIT = 1000.0
MAX_AUTH_FAILURES = 3
RISK_SCORE_THRESHOLD = 100
