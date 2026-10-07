from __future__ import annotations

import functools
import sys
import time

import redis

from .adapters import DogMetrics, PgProbeWriter, connect_http_get
from .config import ConfigError, load_config
from .live import LiveConfig
from .core import FreshnessProbe, check_connect, log, run_loop, setup_logging


def main() -> int:
    setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error(str(exc))
        return 2
    try:  # optional tracing; the freshness probe runs without ddtrace
        from ddtrace import patch  # type: ignore[import-not-found]
        patch(psycopg=True, redis=True, requests=True)
    except ImportError:
        log.info("ddtrace not installed, tracing disabled")
    metrics = DogMetrics(cfg.statsd_host, cfg.statsd_port)
    rc = redis.Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
    wd = FreshnessProbe(writers={sid: PgProbeWriter(cfg, sid, host) for sid, host in cfg.stores},
                  redis=rc, metrics=metrics, clock=time.time, stale_after_s=cfg.stale_after_s,
                  live=LiveConfig(rc))
    connect = functools.partial(check_connect, connect_http_get, cfg.connect_url,
                                cfg.connectors, metrics)
    log.info("freshness probe started")
    run_loop(wd, connect, cfg.probe_interval_s, cfg.check_interval_s, cfg.connect_interval_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
