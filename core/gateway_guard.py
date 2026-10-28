"""Per-user cloud call rate limiting and automatic circuit breaking."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from core import config


@dataclass
class _UserGatewayState:
    calls: deque[float] = field(default_factory=deque)
    consecutive_failures: int = 0
    circuit_until: float = 0.0


class CloudGatewayGuard:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, _UserGatewayState] = {}

    def _state(self, user_id: str) -> _UserGatewayState:
        return self._users.setdefault(str(user_id or "anonymous"), _UserGatewayState())

    def before_call(self, user_id: str) -> tuple[bool, str]:
        now = time.monotonic()
        with self._lock:
            state = self._state(user_id)
            if state.circuit_until > now:
                return False, "circuit_open"
            cutoff = now - 60.0
            while state.calls and state.calls[0] <= cutoff:
                state.calls.popleft()
            if len(state.calls) >= max(1, int(config.CLOUD_RATE_LIMIT_PER_MINUTE)):
                return False, "rate_limited"
            state.calls.append(now)
            return True, "allowed"

    def record_success(self, user_id: str) -> None:
        with self._lock:
            state = self._state(user_id)
            state.consecutive_failures = 0
            state.circuit_until = 0.0

    def record_failure(self, user_id: str) -> str:
        now = time.monotonic()
        with self._lock:
            state = self._state(user_id)
            state.consecutive_failures += 1
            if state.consecutive_failures >= max(1, int(config.CLOUD_FAILURE_THRESHOLD)):
                state.circuit_until = now + max(1, int(config.CLOUD_CIRCUIT_SECONDS))
                return "circuit_open"
            return "cloud_error"

    def reset(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._users.clear()
            else:
                self._users.pop(str(user_id), None)

    def status(self, user_id: str) -> dict:
        now = time.monotonic()
        with self._lock:
            state = self._state(user_id)
            return {
                "circuit_open": state.circuit_until > now,
                "retry_after_seconds": max(0, round(state.circuit_until - now, 1)),
                "consecutive_failures": state.consecutive_failures,
                "calls_last_minute": len(state.calls),
            }


cloud_guard = CloudGatewayGuard()


__all__ = ["CloudGatewayGuard", "cloud_guard"]
