"""Entrypoint: `ddtrace-run python -m offer_worker`. Set DD_DATA_STREAMS_ENABLED=true for Data Streams Monitoring.

Single-threaded: one carts.at-risk record at a time, offset committed after the offer is delivered (at least once;
risk_id dedup in memory plus the deterministic offer_id on the compacted topic absorb replays).
Any unexpected error stops the process with a traceback (fail loudly); compose restarts it.
"""
from __future__ import annotations

import logging
import os
import signal
import sys

import redis

from .bedrock import BedrockWriter
from .config import Config
from .jev import HttpJevClient
from .kafka_io import OfferPublisher, RiskSource, schema_registry
from .logs import setup_logging
from .metrics import Metrics, dogstatsd_from_config
from .policy import load_catalogue
from .worker import OfferWorker


def main() -> int:
    cfg = Config.from_env(os.environ)
    setup_logging()
    log = logging.getLogger("offer_worker")
    stop = False

    def _stop(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    catalogue = load_catalogue(cfg.catalogue_file)
    r = redis.Redis.from_url(cfg.redis_url)
    r.ping()  # fail at start-up, not on the first offer
    sr = schema_registry(cfg)
    jev = HttpJevClient(cfg.jev_url, cfg.jev_api_key, cfg.jev_model, cfg.jev_timeout_ms) if cfg.jev_api_key else None
    text = BedrockWriter(cfg.aws_region, cfg.bedrock_model_id, cfg.bedrock_timeout_ms) if cfg.bedrock_enabled else None
    publisher = OfferPublisher(cfg, sr)
    worker = OfferWorker(cfg, r, catalogue, publisher.publish,
                         Metrics(dogstatsd_from_config(cfg.dd_agent_host, cfg.dd_env, cfg.dd_service, cfg.dd_version)), jev, text)
    source = RiskSource(cfg, sr)
    log.info("offer-worker started", extra={"ctx": {"jev": jev is not None, "bedrock": text is not None,
                                                    "kill_switch_env": cfg.kill_switch, "products": len(catalogue)}})
    try:
        while not stop:
            got = source.poll()
            if got is None:
                continue
            msg, value = got
            worker.handle(value)
            source.commit(msg)
    finally:
        source.close()
    log.info("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
