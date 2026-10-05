"""Entrypoint: `ddtrace-run python -m supplier_sim`."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

import redis

from .adapters import PgProcurement, PgSources, RedisCompression, RedisEtas
from .config import Config
from .core import Supplier
from .logs import setup_logging
from .metrics import Metrics, dogstatsd_from_config

INTERVAL_S = 1.0


def main() -> int:
    cfg = Config.from_env(os.environ)
    setup_logging()
    log = logging.getLogger("supplier_sim")
    stop = False

    def _stop(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    redis_client = redis.Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    supplier = Supplier(
        PgProcurement(cfg.procurement_host, cfg.procurement_database, cfg.procurement_user,
                      cfg.procurement_password),
        PgSources(cfg.store_hosts, cfg.pg_database, cfg.pg_writer_user, cfg.pg_writer_password, cfg.pg_port),
        RedisEtas(redis_client),
        Metrics(dogstatsd_from_config(cfg.dd_agent_host, cfg.dd_env, cfg.dd_service, cfg.dd_version)),
        cfg.default_lead_time_s, lambda: int(time.time() * 1000), RedisCompression(redis_client))
    log.info("started", extra={"ctx": {"stores": list(cfg.store_hosts)}})
    while not stop:
        started = time.monotonic()
        try:
            supplier.cycle()
        except Exception as exc:  # procurement unreachable or bad lead time: loud, retried next second
            log.error(f"delivery cycle failed: {exc!r}", exc_info=True)
        time.sleep(max(0.0, INTERVAL_S - (time.monotonic() - started)))
    log.info("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
