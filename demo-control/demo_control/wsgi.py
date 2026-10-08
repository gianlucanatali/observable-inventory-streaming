"""Production wiring: `ddtrace-run gunicorn demo_control.wsgi:app` (one worker; startup seeds demo.config defaults)."""
from __future__ import annotations

import logging
import os

import boto3  # credentials: SDK default chain (the ECS task role); never keys in env or files
import psycopg
import redis
from datadog import DogStatsd
from scenario.presenter_actions import RestockReset, make_presenter_operations

from .actions import Actions
from .checks import Checks, ScenarioApi, parse_routing_value
from .sales import RATE_KEY, BackgroundSales, SalesError, store_probe
from .ops import make_operations
from .routing import AlbRouting, region_from_arn
from .nginx_routing import NginxRouting
from .store_feed import ConnectFeeds
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
    statsd = DogStatsd(host=cfg.statsd_host, port=cfg.statsd_port)
    # scenario.redisview builds keys from str values (stock:<ns>:...), so it needs a decoding client;
    # the backends above keep the bytes client they were written for.
    r_text = redis.Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
    sell_out, reset_data = make_presenter_operations(store_conn, cfg.stores, r_text)
    alb = None  # the release-routing backend: the ALB rule (hybrid) or nginx's upstream (local mode), same interface
    if cfg.alb_rule_arn:
        elbv2 = boto3.client("elbv2", region_name=region_from_arn(cfg.alb_rule_arn))
        alb = AlbRouting(elbv2, cfg.alb_rule_arn, dict(cfg.alb_target_groups), r)
    elif cfg.nginx_routing_dir:
        alb = NginxRouting(cfg.nginx_routing_dir, r)
    feeds = ConnectFeeds(cfg.connect_url) if cfg.connect_url else None
    control: Control | None = None  # created below; the callbacks run only after startup

    def live_weights():
        return alb.live() if alb is not None else parse_routing_value(control.routing())

    def read_rate() -> float:
        reading = control.read(RATE_KEY)
        if reading.status != "ok" or reading.value is None:
            raise SalesError(f"{RATE_KEY} is not readable from the store sources: {reading.status} {reading.detail}")
        return reading.value

    checks = (Checks(ScenarioApi(cfg.scenario_api_url, cfg.scenario_api_token), r, live_weights)
              if cfg.scenario_api_url else None)
    sales = (BackgroundSales(read_rate, lambda v: control.set(RATE_KEY, v).reading.value, registry[RATE_KEY].default,
                             r, store_probe(cfg.stores, store_conn)) if cfg.stores else None)
    # Full demo reset's restock part (same as `make reset`): skipped when PROCUREMENT_HOST is absent or demo:layers lacks restock.
    # The lead time goes back through the panel's own setting path, so procurement_config (supplier-sim) and Kafka demo.config
    # (Flink restock.sql, sellable-dev) agree again.
    restock = RestockReset(procurement_conn if cfg.procurement_host else None, r_text, lambda: control.layers(),
                           set_lead_time_setting=lambda seconds: control.set("lead_time_s", seconds))
    actions = Actions(sell_out, reset_data, event_sink=statsd, stack=cfg.stack,
                      operations=make_operations(alb, feeds, checks, sales, reset_data, restock))
    control = Control(registry, backends, r, statsd, cfg.stack,
                      cfg.routing_file, actions, alb=alb, feeds=feeds, checks=checks, sales=sales)
    return create_app(control, cfg.control_password)


app = build()
