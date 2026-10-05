"""Production wiring: `ddtrace-run gunicorn demo_control.wsgi:app` (one worker; startup seeds demo.config defaults)."""
from __future__ import annotations

import logging
import os

import psycopg
import redis
from datadog import DogStatsd

from .app import create_app
from .backends import (KafkaConfigBackend, ProcurementBackend, ReadOnlyBackend, RedisBackend, StoreDbsBackend)
from .config import Config, ConfigError, split_host_port
from .kafka_io import ConfigProducer, ConfigReader, schema_registry
from .registry import load_registry
from .service import Control, setup_logging

setup_logging()
log = logging.getLogger("demo_control")


def build() -> "object":
    try:
        cfg = Config.from_env(os.environ)
    except ConfigError as exc:
        raise SystemExit(f"demo-control: {exc}") from exc
    registry = load_registry(cfg.registry_file)
    r = redis.Redis.from_url(cfg.redis_url, socket_timeout=3, socket_connect_timeout=3)
    sr = schema_registry(cfg)
    kafka = KafkaConfigBackend(ConfigReader(cfg, sr), ConfigProducer(cfg, sr))

    def store_conn(host: str):
        host, port = split_host_port(host, cfg.pg_port)
        return psycopg.connect(host=host, port=port, dbname=cfg.pg_database, user=cfg.pg_user,
                               password=cfg.pg_password, connect_timeout=3, autocommit=True)

    def procurement_conn():
        return psycopg.connect(host=cfg.procurement_host, port=cfg.procurement_port, dbname=cfg.procurement_database,
                               user=cfg.procurement_user, password=cfg.procurement_password,
                               connect_timeout=3, autocommit=True)

    backends = {
        "redis": RedisBackend(r), "kafka_config": kafka,
        "procurement_db": ProcurementBackend(procurement_conn if cfg.procurement_host else None),
        "store_dbs": StoreDbsBackend(cfg.stores, store_conn if cfg.stores else None),
        "flink_statement": ReadOnlyBackend(),
    }
    seeded = kafka.seed_defaults(list(registry.values()))  # raises (fail loudly) if Kafka/SR cannot be reached
    topic_now = {k: rd.value for k, rd in kafka.read(list(registry.values())).items() if rd.status == "ok"}
    seeded += backends["redis"].seed_defaults(list(registry.values()), topic_now)  # raises if Redis is unreachable
    log.info("demo-control started", extra={"fields": {"stack": cfg.stack, "seeded_defaults": seeded}})
    control = Control(registry, backends, r, DogStatsd(host=cfg.statsd_host, port=cfg.statsd_port), cfg.stack, cfg.routing_file)
    return create_app(control, cfg.control_password)


app = build()
