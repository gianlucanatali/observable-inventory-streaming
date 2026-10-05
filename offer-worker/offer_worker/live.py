"""Live parameters from Redis hash `demo:config` (contracts section 12). Read on every use; never cached."""
from __future__ import annotations

import logging

log = logging.getLogger("offer_worker")
CONFIG_KEY = "demo:config"


class LiveConfig:
    """`get(name, default, lo, hi)`: the number in demo:config, or `default` (logged once per name) when the field
    is absent, unreadable, not a number or outside lo..hi."""

    def __init__(self, redis_client):
        self._r = redis_client
        self._warned: set[str] = set()

    def _fallback(self, name: str, why: str, default: float) -> float:
        if name not in self._warned:
            self._warned.add(name)
            log.warning("live config unavailable; using default",
                        extra={"ctx": {"param": name, "reason": why, "default": default}})
        return default

    def get(self, name: str, default: float, lo: float, hi: float) -> float:
        try:
            raw = self._r.hget(CONFIG_KEY, name)
        except Exception as exc:  # noqa: BLE001 - Redis trouble must not stop offers; logged once
            return self._fallback(name, f"redis unreadable: {exc!r}", default)
        if raw is None:
            return self._fallback(name, "field absent", default)
        try:
            value = float(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            return self._fallback(name, f"not a number: {raw!r}", default)
        if not lo <= value <= hi:
            return self._fallback(name, f"{value} outside {lo}..{hi}", default)
        self._warned.discard(name)
        return value
