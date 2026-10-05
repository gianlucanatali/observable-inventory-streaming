from __future__ import annotations

import contextlib
import os


class ConfigError(Exception):
    pass


def require(names: list[str], env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ) if env is None else env
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise ConfigError("missing required environment variable(s): " + ", ".join(missing))
    return {n: env[n] for n in names}


def parse_store_hosts(raw: str) -> list[tuple[str, str]]:
    """STORE_HOSTS `S01=store-s01,S02=store-s02` -> [(store_id, host), ...] in order. Strict."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        sid, sep, host = part.partition("=")
        if not sep or not sid or not host or sid != sid.strip() or host != host.strip():
            raise ConfigError(f"STORE_HOSTS entry {part!r} is not STORE_ID=host (value {raw!r})")
        if sid in [p[0] for p in pairs]:
            raise ConfigError(f"STORE_HOSTS lists store {sid!r} twice")
        pairs.append((sid, host))
    return pairs


def store_hosts(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    return parse_store_hosts(require(["STORE_HOSTS"], env)["STORE_HOSTS"])


def pg_connect(host: str):
    import psycopg
    e = require(["PG_DATABASE", "PG_WRITER_USER", "PG_WRITER_PASSWORD"])
    return psycopg.connect(host=host, port=int(os.environ.get("PG_PORT", "5432")),
                           dbname=e["PG_DATABASE"], user=e["PG_WRITER_USER"],
                           password=e["PG_WRITER_PASSWORD"], connect_timeout=5,
                           # Autocommit: every write is in its own `with conn.transaction()` and commits
                           # there. Without it a prior read opens an implicit transaction, the write becomes
                           # a savepoint, and nothing commits until close (sell-out would land all at once).
                           autocommit=True)


@contextlib.contextmanager
def open_sources():
    """Yield {store_id: connection} for every store in STORE_HOSTS (order kept); close all on exit."""
    with contextlib.ExitStack() as stack:
        conns = {}
        for sid, host in store_hosts():
            try:
                conns[sid] = stack.enter_context(pg_connect(host))
            except Exception as exc:
                raise RuntimeError(f"cannot connect to source of store {sid} at {host}: {exc!r}") from exc
        yield conns


def redis_connect():
    import redis
    e = require(["REDIS_URL"])
    return redis.Redis.from_url(e["REDIS_URL"], decode_responses=True,
                                socket_timeout=5, socket_connect_timeout=5)
