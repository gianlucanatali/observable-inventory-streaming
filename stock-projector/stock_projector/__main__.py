"""Entrypoint: `ddtrace-run python -m stock_projector`. Set DD_DATA_STREAMS_ENABLED=true for Data Streams Monitoring."""
from __future__ import annotations

import logging
import os
import signal
import sys

import redis

from .config import Config
from .kafka_io import KafkaPublisher, KafkaSource, schema_registry
from .logs import setup_logging
from .metrics import Metrics, dogstatsd_from_config
from .projector import Projector
from .store import RedisStore


def main() -> int:
    cfg = Config.from_env(os.environ)
    setup_logging()
    stop = False

    def _stop(_sig, _frm):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sr = schema_registry(cfg)
    source = KafkaSource(cfg, sr)
    try:
        store = RedisStore(redis.Redis.from_url(cfg.redis_url))
        metrics = Metrics(dogstatsd_from_config(cfg.dd_agent_host, cfg.dd_env, cfg.dd_service, cfg.dd_version))
        state = KafkaPublisher(cfg, sr, cfg.state_topic, "stock_key.avsc", "stock_state.avsc")
        movements = KafkaPublisher(cfg, sr, cfg.movements_topic, "stock_movement_key.avsc", "stock_movement.avsc")
        code = Projector(store, state, source, metrics, cfg.store_ids, movements=movements).run(lambda: stop)
    finally:
        source.close()
    logging.getLogger("stock_projector").info("exit", extra={"ctx": {"code": code}})
    return code


if __name__ == "__main__":
    sys.exit(main())
