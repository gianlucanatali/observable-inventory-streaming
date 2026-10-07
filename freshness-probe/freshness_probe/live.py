"""Live parameters from Redis hash `demo:config` (contracts section 12). Read on every use; never cached."""
from __future__ import annotations

import logging

log = logging.getLogger("freshness_probe")
CONFIG_KEY = "demo:config"


class LiveConfig:
    """`get(name, default)` returns the float in demo:config, or `default` (logged once per name) when absent,
    unreadable or not a positive number."""

    def __init__(self, redis_client):
        self._r = redis_client
        self._warned: set[str] = set()

    def _warn_once(self, name: str, why: str, default: float) -> float:
        if name not in self._warned:
            self._warned.add(name)
            log.warning("live config unavailable; using default",
                        extra={"fields": {"param": name, "reason": why, "default": default}})
        return default

    def get(self, name: str, default: float) -> float:
        try:
            raw = self._r.hget(CONFIG_KEY, name)
        except Exception as exc:  # noqa: BLE001 - Redis trouble must not stop probing; logged once
            return self._warn_once(name, f"redis unreadable: {exc!r}", default)
        if raw is None:
            return self._warn_once(name, "field absent", default)
        try:
            value = float(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            return self._warn_once(name, f"not a number: {raw!r}", default)
        if value <= 0:
            return self._warn_once(name, f"must be > 0, got {value}", default)
        self._warned.discard(name)  # present again: warn again if it disappears later
        return value
