"""Control service: registry + backends + audit (Datadog event and JSON log line)."""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass

from .backends import LayerOff, Reading, WriteFailed
from .registry import Param, fmt

log = logging.getLogger("demo_control")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                   "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@dataclass(frozen=True)
class Row:
    param: Param
    reading: Reading


class Control:
    LAYERS_KEY = "demo:layers"
    ROUTING_KEY = "demo:routing"

    def __init__(self, registry: dict[str, Param], backends: dict[str, object], redis_client, statsd, stack: str,
                 routing_file: str | None = None, actions=None, alb=None, feeds=None, checks=None, sales=None):
        self.registry, self._backends, self._redis, self._statsd = registry, backends, redis_client, statsd
        self.stack, self._routing_file = stack, routing_file
        self.actions = actions
        self.alb, self.feeds = alb, feeds  # routing backend (AlbRouting | NginxRouting) / ConnectFeeds, None when not configured
        self.checks, self.sales = checks, sales  # checks.Checks / sales.BackgroundSales, None when not configured

    # --- reading ---------------------------------------------------------------------------------------------------
    def rows(self) -> list[Row]:
        # Read every store once for all the params that use it (as primary or as an extra mirror).
        per_store: dict[str, dict[str, Reading]] = {}
        for store in dict.fromkeys(st for p in self.registry.values() for st in p.stores):
            params = [p for p in self.registry.values() if store in p.stores]
            per_store[store] = self._backends[store].read(params)
        readings: dict[str, Reading] = {}
        for p in self.registry.values():
            r = self._merge(p, {st: per_store[st][p.key] for st in p.stores})
            if r.status == "error":
                log.error("parameter unreadable", extra={"fields": {"param": p.key, "store": p.store, "detail": r.detail}})
            readings[p.key] = r
        return [Row(p, readings[p.key]) for p in self.registry.values()]

    def read(self, key: str) -> Reading:
        """One parameter, read from every store that holds it (same merge rule as rows())."""
        p = self.registry[key]
        return self._merge(p, {st: self._backends[st].read([p])[key] for st in p.stores})

    @staticmethod
    def _merge(p: Param, by_store: dict[str, Reading]) -> Reading:
        """Single store: as is. Several: the first store is the truth; if another holds a different value, error row."""
        first = by_store[p.stores[0]]
        if len(by_store) == 1 or first.status in ("error", "layer_off", "readonly"):
            return first
        diffs = [f"{st}={fmt(r.value) if r.value is not None else r.status}" for st, r in by_store.items()
                 if (r.status, r.value) != (first.status, first.value)]
        if diffs:
            shown = ", ".join(f"{st}={fmt(r.value) if r.value is not None else r.status}" for st, r in by_store.items())
            return Reading("error", detail=f"stores disagree: {shown}")
        return first

    # --- writing ---------------------------------------------------------------------------------------------------
    def set(self, key: str, raw_value) -> Row:
        """Validate, write, read back from the same store, audit. Raises KeyError, ValidationError, LayerOff, WriteFailed."""
        p = self.registry[key]
        value = p.validate(raw_value)
        written: list[str] = []
        for store in p.stores:
            try:
                self._backends[store].write(p, value)
            except (LayerOff, WriteFailed) as exc:
                done = f" (already written to: {', '.join(written)})" if written else ""
                msg = f"{exc}{done}" if written else str(exc)
                log.error("parameter change failed", extra={"fields": {"param": key, "requested": value, "error": msg}})
                if written:
                    raise WriteFailed(f"{p.key}: write to {store} failed: {msg}") from exc
                raise
            written.append(store)
        reading = self._merge(p, {st: self._backends[st].read([p])[key] for st in p.stores})
        self._audit(p, value, reading)
        return Row(p, reading)

    def _audit(self, p: Param, value: float, reading: Reading) -> None:
        title = f"demo config: {p.key} = {fmt(value)}"
        tags = ["project:dd-demo", f"stack:{self.stack}", f"layer:{p.layer}", f"param:{p.key}", "demo_event:config"]
        log.info(title, extra={"fields": {"event": "demo_config_changed", "param": p.key, "value": value, "unit": p.unit,
                                          "layer": p.layer, "stack": self.stack, "store": p.store,
                                          "read_back": reading.status if reading.value is None else reading.value}})
        try:
            self._statsd.event(title, f"{p.label}: set to {fmt(value)} {p.unit} (default {fmt(p.default)}), "
                                      f"apply: {p.apply}, store: {p.store}", alert_type="info", tags=tags)
        except Exception:  # noqa: BLE001 - audit must not undo the change; the JSON log line above already exists
            log.exception("datadog event failed", extra={"fields": {"param": p.key}})

    # --- read-only context -----------------------------------------------------------------------------------------
    @property
    def redis(self):
        """The Redis client, for read-only cards (links_card)."""
        return self._redis

    def layers(self) -> list[str] | None:
        """Redis `demo:layers`: JSON list (`["core","restock"]`) or comma-separated names. None = unknown."""
        try:
            raw = self._redis.get(self.LAYERS_KEY)
        except Exception as exc:  # noqa: BLE001
            log.error("demo:layers unreadable", extra={"fields": {"error": repr(exc)}})
            return None
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else raw
        try:
            val = json.loads(text)
            if isinstance(val, list):
                return [str(x) for x in val]
        except ValueError:
            pass
        return [x.strip() for x in text.split(",") if x.strip()]

    def routing(self) -> str | None:
        """Canary routing: JSON file at ROUTING_FILE if mounted, else Redis `demo:routing`, else None (unknown)."""
        if self._routing_file:
            try:
                with open(self._routing_file) as f:
                    return json.dumps(json.load(f), sort_keys=True)
            except (OSError, ValueError) as exc:
                log.warning("routing file unreadable", extra={"fields": {"file": self._routing_file, "error": repr(exc)}})
        try:
            raw = self._redis.get(self.ROUTING_KEY)
        except Exception as exc:  # noqa: BLE001
            log.error("demo:routing unreadable", extra={"fields": {"error": repr(exc)}})
            return None
        return None if raw is None else (raw.decode() if isinstance(raw, bytes) else raw)
