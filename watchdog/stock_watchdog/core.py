"""Pure watchdog logic: probe tracking, feed state, Connect status. All I/O is injected."""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .live import LiveConfig

PROBE_PRODUCT = "__probe__"
STATE_OK, STATE_STALE, STATE_UNKNOWN = "ok", "stale", "unknown"
STATE_GAUGE = {STATE_OK: 1, STATE_STALE: 0, STATE_UNKNOWN: -1}
STATE_RANK = {STATE_OK: 0, STATE_STALE: 1, STATE_UNKNOWN: 2}

log = logging.getLogger("stock_watchdog")


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


class Metrics(Protocol):
    def gauge(self, name: str, value: float, tags: list[str] | None = None) -> None: ...
    def increment(self, name: str, tags: list[str] | None = None) -> None: ...


class ProbeWriter(Protocol):
    def write(self, quantity: int) -> tuple[int, int]:
        """Upsert the store's probe row; return (revision, changed_at_ms). Raise on failure."""


@dataclass
class CheckResult:
    state: str
    probe_age_ms: int  # -1 when it cannot be computed
    reason: str


@dataclass
class StoreProbe:
    writer: ProbeWriter
    pending: list[tuple[int, int]] = field(default_factory=list)  # (revision, changed_at_ms)
    wrote_ok_once: bool = False
    last_write_failed: bool = False
    last_changed_at_ms: int | None = None  # newest successful write


@dataclass
class Watchdog:
    writers: dict[str, ProbeWriter]  # store_id -> writer, in display order
    redis: object  # redis.Redis-like: get(), hget(), hset()
    metrics: Metrics
    clock: Callable[[], float]  # epoch seconds
    stale_after_s: float
    probes: dict[str, StoreProbe] = field(init=False)
    live: LiveConfig | None = None

    def __post_init__(self) -> None:
        self.probes = {sid: StoreProbe(w) for sid, w in self.writers.items()}

    def probe_write(self) -> None:
        quantity = int(self.clock()) % 1_000_000
        for sid, p in self.probes.items():  # one store failing never stops the others
            try:
                revision, changed_at_ms = p.writer.write(quantity)
            except Exception as exc:  # noqa: BLE001 - logged, counted, and turns that store to unknown
                p.last_write_failed = True
                self.metrics.increment("stock.probe.write_errors", [f"store:{sid}"])
                log.error("probe write failed", extra={"fields": {"store": sid, "error": repr(exc)}})
                continue
            p.last_write_failed = False
            p.wrote_ok_once = True
            p.pending.append((revision, changed_at_ms))
            p.last_changed_at_ms = changed_at_ms
            log.info("probe written", extra={"fields": {"store": sid, "revision": revision, "quantity": quantity}})

    def stale_after(self) -> float:
        """Live `stale_after_s` from demo:config, else the configured default."""
        return self.live.get("stale_after_s", self.stale_after_s) if self.live else self.stale_after_s

    def _active_ns(self) -> str:
        ns = self.redis.get("stock:active_ns")
        if ns is None:
            raise LookupError("stock:active_ns is not set")
        return ns.decode() if isinstance(ns, bytes) else ns

    def _evaluate_store(self, sid: str, p: StoreProbe, now_ms: int) -> CheckResult:
        try:
            raw = self.redis.hget(f"stock:{self._active_ns()}:{sid}:{PROBE_PRODUCT}", "revision")
            visible = None if raw is None else int(raw)
        except Exception as exc:  # noqa: BLE001 - any Redis problem means we cannot vouch for the feed
            log.error("redis read failed", extra={"fields": {"store": sid, "error": repr(exc)}})
            return CheckResult(STATE_UNKNOWN, -1, f"redis unreadable: {exc!r}")
        if visible is not None:
            p.pending = [x for x in p.pending if x[0] > visible]
        if not p.wrote_ok_once:
            return CheckResult(STATE_UNKNOWN, -1, "no successful probe write yet")
        age_ms = max(0, now_ms - p.pending[0][1]) if p.pending else 0
        if p.last_write_failed:
            return CheckResult(STATE_UNKNOWN, age_ms, "last probe write failed")
        state = STATE_OK if age_ms <= self.stale_after() * 1000 else STATE_STALE
        return CheckResult(state, age_ms, "probe pending" if p.pending else "caught up")

    def evaluate(self) -> dict[str, CheckResult]:
        now_ms = int(self.clock() * 1000)
        return {sid: self._evaluate_store(sid, p, now_ms) for sid, p in self.probes.items()}

    def _sellable_age_s(self, now_ms: int) -> float | None:
        """Seconds the aggregate (Flink + sink) path is behind the newest probe write; 0 when caught up."""
        newest = max((p.last_changed_at_ms for p in self.probes.values() if p.last_changed_at_ms is not None),
                     default=None)
        if newest is None:
            return None
        try:
            raw = self.redis.hget(f"sellable:{PROBE_PRODUCT}", "last_changed_at_ms")
        except Exception as exc:  # noqa: BLE001 - no metric rather than a wrong one
            log.error("sellable read failed", extra={"fields": {"error": repr(exc)}})
            return None
        if raw is not None and int(raw) >= newest:
            return 0.0
        return max(0, now_ms - newest) / 1000

    def _write_status(self, key: str, state: str, age_ms: int, now_ms: int) -> None:
        try:
            self.redis.hset(key, mapping={"state": state, "probe_age_ms": age_ms, "checked_at_ms": now_ms})
        except Exception as exc:  # noqa: BLE001 - the serving API treats absent/old status as unknown
            log.error("feed status write failed", extra={"fields": {"key": key, "error": repr(exc)}})

    def check(self) -> dict[str, CheckResult]:
        results = self.evaluate()
        now_ms = int(self.clock() * 1000)
        for sid, res in results.items():
            tags = [f"store:{sid}"]
            self.metrics.gauge("stock.feed.state", STATE_GAUGE[res.state], tags)
            if res.probe_age_ms >= 0:
                self.metrics.gauge("stock.probe.age", res.probe_age_ms / 1000, tags)
            self._write_status(f"feed:status:{sid}", res.state, res.probe_age_ms, now_ms)
        worst = max((r.state for r in results.values()), key=STATE_RANK.__getitem__, default=STATE_UNKNOWN)
        self._write_status("feed:status", worst, max((r.probe_age_ms for r in results.values()), default=-1), now_ms)
        sellable_age = self._sellable_age_s(now_ms)
        if sellable_age is not None:
            self.metrics.gauge("stock.sellable.age", sellable_age)
        log.info("feed checked", extra={"fields": {
            "state": worst, "stores": {s: [r.state, r.probe_age_ms, r.reason] for s, r in results.items()},
            "sellable_age_s": sellable_age}})
        return results


def check_connect(http_get: Callable[[str], dict], base_url: str, connectors: Iterable[str],
                  metrics: Metrics) -> dict[str, bool]:
    """Per connector: 1 only if the connector and every task report RUNNING; any error is 0 and logged."""
    out: dict[str, bool] = {}
    for name in connectors:
        url = f"{base_url}/connectors/{name}/status"
        try:
            body = http_get(url)
            tasks = body.get("tasks") or []
            running = (body["connector"]["state"] == "RUNNING" and len(tasks) > 0
                       and all(t.get("state") == "RUNNING" for t in tasks))
            if not running:
                log.error("connector not running", extra={"fields": {"connector": name, "status": body}})
        except Exception as exc:  # noqa: BLE001 - unreachable or malformed means not running
            running = False
            log.error("connect status check failed", extra={"fields": {"url": url, "error": repr(exc)}})
        metrics.gauge("stock.connect.task_running", 1 if running else 0, [f"connector:{name}"])
        out[name] = running
    return out


def run_loop(wd: Watchdog, connect_check: Callable[[], object], probe_interval_s: float,
             check_interval_s: float, connect_interval_s: float,
             sleep: Callable[[float], None] = time.sleep, stop: Callable[[], bool] = lambda: False) -> None:
    """The probe interval is re-read every cycle from demo:config when the watchdog has a LiveConfig."""
    next_probe = next_connect = wd.clock()
    while not stop():
        now = wd.clock()
        if now >= next_probe:
            wd.probe_write()
            next_probe = now + (wd.live.get("probe_interval_s", probe_interval_s) if wd.live else probe_interval_s)
        if now >= next_connect:
            connect_check()
            next_connect = now + connect_interval_s
        wd.check()
        sleep(check_interval_s)
