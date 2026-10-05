"""One backend per `store` in the registry. Each reads current values back from the place that holds them.

Reading.status: ok (value is stored), unset (nothing stored; consumers use the registry default), layer_off
(the store is not running or not configured; not an error), error (unexpected failure, detail says why),
readonly (shown only; defined elsewhere).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .registry import Param, fmt


class LayerOff(Exception):
    """The store behind a parameter is not running (or not configured in this stack)."""


class WriteFailed(Exception):
    """A write did not (fully) happen. Message says where, what and why."""


@dataclass(frozen=True)
class Reading:
    status: str
    value: float | None = None
    detail: str = ""


def _unset() -> Reading:
    return Reading("unset")


class RedisBackend:
    HASH = "demo:config"
    KILL_KEY = "offers:kill_switch"

    def __init__(self, redis_client):
        self._r = redis_client

    def read(self, params: list[Param]) -> dict[str, Reading]:
        try:
            raw = self._r.hgetall(self.HASH)
            kill = self._r.get(self.KILL_KEY)
        except Exception as exc:  # noqa: BLE001 - shown on the row, logged by the caller
            err = Reading("error", detail=f"redis unreadable: {type(exc).__name__}: {exc}")
            return {p.key: err for p in params}
        stored = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        out: dict[str, Reading] = {}
        for p in params:
            if p.key == "offers_kill_switch":
                # The worker reads offers:kill_switch, so that key (not the hash mirror) is the truth.
                text = kill.decode() if isinstance(kill, bytes) else kill
                out[p.key] = Reading("ok", 0.0 if text in (None, "", "0") else 1.0)
            elif p.key in stored:
                try:
                    out[p.key] = Reading("ok", float(stored[p.key]))
                except ValueError:
                    out[p.key] = Reading("error", detail=f"{self.HASH}.{p.key} holds {stored[p.key]!r}, not a number")
            else:
                out[p.key] = _unset()
        return out

    def seed_defaults(self, params: list[Param], current: dict[str, float] | None = None) -> list[str]:
        """HSETNX every redis-stored key that is absent: the value another store already holds (`current`, e.g. the
        demo.config topic, so multi-store params agree from the start), else the registry default. Never overwrites."""
        seeded = []
        for p in params:
            if "redis" in p.stores and p.key != "offers_kill_switch":
                value = (current or {}).get(p.key, p.default)
                if self._r.hsetnx(self.HASH, p.key, fmt(value)):
                    seeded.append(p.key)
        return seeded

    def write(self, p: Param, value: float) -> None:
        try:
            self._r.hset(self.HASH, p.key, fmt(value))
            if p.key == "offers_kill_switch":
                if value == 1.0:
                    self._r.set(self.KILL_KEY, "1")
                else:
                    self._r.delete(self.KILL_KEY)
        except Exception as exc:  # noqa: BLE001
            raise WriteFailed(f"redis write of {p.key} failed: {type(exc).__name__}: {exc}") from exc


class KafkaConfigBackend:
    """Compacted topic demo.config. `reader()` returns {key: value} (whole topic); `producer.send(key, value)` blocks
    until delivered. Both come from kafka_io (or fakes in tests)."""

    def __init__(self, reader: Callable[[], dict[str, float]], producer, clock: Callable[[], float] = time.time):
        self._reader, self._producer, self._clock = reader, producer, clock

    def read(self, params: list[Param]) -> dict[str, Reading]:
        try:
            current = self._reader()
        except Exception as exc:  # noqa: BLE001
            err = Reading("error", detail=f"demo.config unreadable: {type(exc).__name__}: {exc}")
            return {p.key: err for p in params}
        return {p.key: (Reading("ok", current[p.key]) if p.key in current else _unset()) for p in params}

    def write(self, p: Param, value: float) -> None:
        try:
            self._producer.send({"key": p.key}, {"key": p.key, "value": float(value), "updated_at_ms": int(self._clock() * 1000)})
        except Exception as exc:  # noqa: BLE001
            raise WriteFailed(f"produce to demo.config for {p.key} failed: {type(exc).__name__}: {exc}") from exc

    def seed_defaults(self, params: list[Param]) -> list[str]:
        """Produce the registry default for every kafka_config key missing from the topic; returns the keys seeded."""
        current = self._reader()
        seeded = []
        for p in params:
            if "kafka_config" in p.stores and p.key not in current:
                self.write(p, p.default)
                seeded.append(p.key)
        return seeded


class _PgBackend:
    """Shared connection handling. `connect(host)` returns a psycopg-like connection (autocommit=True)."""

    def _off(self, why: str) -> Exception:
        return LayerOff(why)


class ProcurementBackend(_PgBackend):
    KEYS = ("lead_time_s", "lead_time_jitter_pct")

    def __init__(self, connect: Callable[[], object] | None):
        self._connect = connect  # None: PROCUREMENT_HOST not configured in this stack

    def _conn(self):
        if self._connect is None:
            raise LayerOff("procurement-db is not configured (restock layer off)")
        try:
            return self._connect()
        except Exception as exc:  # noqa: BLE001 - unreachable database means the layer is not running
            raise LayerOff(f"procurement-db unreachable: {type(exc).__name__}: {exc}") from exc

    def read(self, params: list[Param]) -> dict[str, Reading]:
        try:
            conn = self._conn()
        except LayerOff as exc:
            off = Reading("layer_off", detail=str(exc))
            return {p.key: off for p in params}
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT key, value FROM procurement_config WHERE key = ANY(%s)", (list(self.KEYS),))
                    rows = dict(cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            err = Reading("error", detail=f"procurement_config unreadable: {type(exc).__name__}: {exc}")
            return {p.key: err for p in params}
        out = {}
        for p in params:
            try:
                out[p.key] = Reading("ok", float(rows[p.key])) if p.key in rows else _unset()
            except ValueError:
                out[p.key] = Reading("error", detail=f"procurement_config.{p.key} holds {rows[p.key]!r}, not a number")
        return out

    def write(self, p: Param, value: float) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO procurement_config (key, value) VALUES (%s, %s) "
                                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (p.key, fmt(value)))
        except Exception as exc:  # noqa: BLE001
            raise WriteFailed(f"procurement_config write of {p.key} failed: {type(exc).__name__}: {exc}") from exc


class StoreDbsBackend(_PgBackend):
    """demo_setting(key text PK, value double precision) in every store source."""

    def __init__(self, stores: tuple[tuple[str, str], ...], connect: Callable[[str], object] | None):
        self._stores, self._connect = stores, connect

    def _conn(self, host: str):
        return self._connect(host)  # type: ignore[misc]

    def read(self, params: list[Param]) -> dict[str, Reading]:
        if not self._stores or self._connect is None:
            off = Reading("layer_off", detail="no store sources configured (STORE_HOSTS empty)")
            return {p.key: off for p in params}
        per_store: dict[str, dict[str, float] | str] = {}
        for sid, host in self._stores:
            try:
                with self._conn(host) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT key, value FROM demo_setting WHERE key = ANY(%s)", ([p.key for p in params],))
                        per_store[sid] = {k: float(v) for k, v in cur.fetchall()}
            except Exception as exc:  # noqa: BLE001
                per_store[sid] = f"{type(exc).__name__}: {exc}"
        reachable = {s: v for s, v in per_store.items() if isinstance(v, dict)}
        if not reachable:
            off = Reading("layer_off", detail="no store source reachable: " + "; ".join(f"{s}: {v}" for s, v in per_store.items()))
            return {p.key: off for p in params}
        out = {}
        for p in params:
            vals = {s: v[p.key] for s, v in reachable.items() if p.key in v}
            if len(set(vals.values())) > 1:
                out[p.key] = Reading("error", detail="stores disagree: " + ", ".join(f"{s}={fmt(x)}" for s, x in sorted(vals.items())))
            elif vals:
                missing = [s for s in reachable if s not in vals] + [s for s, v in per_store.items() if not isinstance(v, dict)]
                out[p.key] = Reading("ok", next(iter(vals.values())), detail=(f"not set or unreachable in: {', '.join(missing)}" if missing else ""))
            else:
                out[p.key] = _unset()
        return out

    def write(self, p: Param, value: float) -> None:
        if not self._stores or self._connect is None:
            raise LayerOff("no store sources configured (STORE_HOSTS empty)")
        failed: dict[str, str] = {}
        done: list[str] = []
        for sid, host in self._stores:
            try:
                # autocommit=True connection + explicit transaction: without autocommit a bare execute holds the write.
                with self._conn(host) as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO demo_setting (key, value) VALUES (%s, %s) "
                                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (p.key, float(value)))
                done.append(sid)
            except Exception as exc:  # noqa: BLE001 - collected, reported once for all stores
                failed[sid] = f"{type(exc).__name__}: {exc}"
        if failed and not done:
            raise LayerOff("no store source reachable: " + "; ".join(f"{s}: {m}" for s, m in failed.items()))
        if failed:
            raise WriteFailed(f"demo_setting write of {p.key} reached {', '.join(done)} but failed on "
                              + "; ".join(f"{s}: {m}" for s, m in failed.items()))


class ReadOnlyBackend:
    """flink_statement: defined in Terraform, shown only."""

    def read(self, params: list[Param]) -> dict[str, Reading]:
        return {p.key: Reading("readonly", p.default, "set in the Flink statement (Terraform); default shown") for p in params}

    def write(self, p: Param, value: float) -> None:
        raise WriteFailed(f"{p.key} is read-only")
