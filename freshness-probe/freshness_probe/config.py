"""Environment configuration. Fails at startup naming every missing or invalid variable."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def parse_store_hosts(raw: str) -> tuple[tuple[str, str], ...]:
    """STORE_HOSTS `S01=store-s01,S02=store-s02` -> ((store_id, host), ...) in order. Strict."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        sid, sep, host = part.partition("=")
        if not sep or not sid or not host or sid != sid.strip() or host != host.strip():
            raise ConfigError(f"STORE_HOSTS entry {part!r} is not STORE_ID=host (value {raw!r})")
        if sid in [p[0] for p in pairs]:
            raise ConfigError(f"STORE_HOSTS lists store {sid!r} twice")
        pairs.append((sid, host))
    return tuple(pairs)


@dataclass(frozen=True)
class Config:
    stores: tuple[tuple[str, str], ...]  # (store_id, host) in display order
    connectors: tuple[str, ...]
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str
    redis_url: str
    connect_url: str
    probe_interval_s: float
    check_interval_s: float
    stale_after_s: float
    connect_interval_s: float
    statsd_host: str
    statsd_port: int


def _float(env: dict[str, str], name: str, default: str, errors: list[str]) -> float:
    raw = env.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a number")
        return 0.0
    if value <= 0:
        errors.append(f"{name}={raw!r} must be > 0")
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    errors: list[str] = []
    for name in ("STORE_HOSTS", "PG_DATABASE", "PG_WRITER_USER", "PG_WRITER_PASSWORD",
                 "REDIS_URL", "CONNECT_URL"):
        if not env.get(name):
            errors.append(f"missing required environment variable {name}")
    port_raw = env.get("PG_PORT", "5432")
    statsd_port_raw = env.get("DD_DOGSTATSD_PORT", "8125")
    ports = {}
    for name, raw in (("PG_PORT", port_raw), ("DD_DOGSTATSD_PORT", statsd_port_raw)):
        try:
            ports[name] = int(raw)
        except ValueError:
            errors.append(f"{name}={raw!r} is not an integer")
            ports[name] = 0
    probe = _float(env, "PROBE_INTERVAL_S", "5", errors)
    check = _float(env, "CHECK_INTERVAL_S", "1", errors)
    stale = _float(env, "STALE_AFTER_S", "15", errors)
    connect = _float(env, "CONNECT_INTERVAL_S", "10", errors)
    stores: tuple[tuple[str, str], ...] = ()
    if env.get("STORE_HOSTS"):
        try:
            stores = parse_store_hosts(env["STORE_HOSTS"])
        except ConfigError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigError("freshness-probe configuration invalid: " + "; ".join(errors))
    return Config(
        stores=stores,
        connectors=tuple(f"inventory-{sid.lower()}" for sid, _ in stores) + (env.get("SELLABLE_CONNECTOR", "sellable-redis"),),
        pg_port=ports["PG_PORT"], pg_database=env["PG_DATABASE"],
        pg_user=env["PG_WRITER_USER"], pg_password=env["PG_WRITER_PASSWORD"],
        redis_url=env["REDIS_URL"], connect_url=env["CONNECT_URL"].rstrip("/"),
        probe_interval_s=probe, check_interval_s=check,
        stale_after_s=stale, connect_interval_s=connect,
        statsd_host=env.get("DD_AGENT_HOST", "localhost"), statsd_port=ports["DD_DOGSTATSD_PORT"],
    )
