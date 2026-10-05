"""Local-mode stand-in for overlay/flink/sellable.sql."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from confluent_kafka import DeserializingConsumer, KafkaError, SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

from .core import Aggregator, CartAtRiskTracker, DemandWindow, ForecastTracker, Restocker

REQUIRED = ("KAFKA_BOOTSTRAP", "SR_URL")
REQUIRED_AUTH = ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET")


def kafka_auth(env) -> dict:
    protocol = env.get("KAFKA_SECURITY_PROTOCOL") or "SASL_SSL"
    if protocol not in ("SASL_SSL", "PLAINTEXT"):
        raise SystemExit(f"sellable-dev: KAFKA_SECURITY_PROTOCOL={protocol!r} is not SASL_SSL or PLAINTEXT")
    needed = REQUIRED if protocol == "PLAINTEXT" else REQUIRED + REQUIRED_AUTH
    missing = [n for n in needed if not env.get(n)]
    if missing:
        raise SystemExit(f"sellable-dev: missing required environment variable(s): {', '.join(missing)}")
    conf = {"bootstrap.servers": env["KAFKA_BOOTSTRAP"], "security.protocol": protocol}
    if protocol != "PLAINTEXT":
        conf.update({"sasl.mechanisms": "PLAIN", "sasl.username": env["KAFKA_API_KEY"], "sasl.password": env["KAFKA_API_SECRET"]})
    return conf


def offers_enabled(env) -> bool:
    raw = env.get("OFFERS_ENABLED")
    if raw is None or raw == "" or raw == "false":
        return False
    if raw == "true":
        return True
    raise SystemExit(f"sellable-dev: OFFERS_ENABLED={raw!r} is not 'true' or 'false'")


def main() -> int:
    env = os.environ
    auth = kafka_auth(env)
    sr_conf = {"url": env["SR_URL"]}
    if env.get("SR_API_KEY"):
        sr_conf["basic.auth.user.info"] = f"{env['SR_API_KEY']}:{env['SR_API_SECRET']}"
    sr = SchemaRegistryClient(sr_conf)
    schemas = Path(env.get("SCHEMA_DIR", "/app/contracts/avro"))
    in_topic = env.get("STATE_TOPIC", "inventory.state")
    out_topic = env.get("SELLABLE_TOPIC", "stock.sellable")

    consumer = DeserializingConsumer({
        **auth, "group.id": env.get("KAFKA_GROUP_ID", "sellable-dev"), "auto.offset.reset": "earliest",
        "key.deserializer": AvroDeserializer(sr), "value.deserializer": AvroDeserializer(sr),
    })
    producer = SerializingProducer({
        **auth, "enable.idempotence": True, "acks": "all",
        "key.serializer": AvroSerializer(sr, (schemas / "sellable_key.avsc").read_text()),
        "value.serializer": AvroSerializer(sr, (schemas / "sellable.avsc").read_text()),
    })
    # Restock layer: enabled only when RESTOCK_TOPIC is set, so the core layer needs none of these topics.
    # Stands in for overlay/flink/demand.sql, procurement.sql and restock.sql (same Avro schemas).
    restock_topic = env.get("RESTOCK_TOPIC") or None
    producers: dict[str, SerializingProducer] = {}
    topics = {}
    if restock_topic:
        topics = {
            "movements": env.get("MOVEMENTS_TOPIC", "stock.movements"),
            "demand": env.get("DEMAND_TOPIC", "stock.demand"),
            "orders": env.get("ORDERS_TOPIC", "procurement.orders"),
            "forecast": env.get("FORECAST_TOPIC", "restock.forecast"),
            "config": env.get("CONFIG_TOPIC", "demo.config"),
            "restock": restock_topic,
        }
        for name, (kf, vf) in {"demand": ("stock_demand_key", "stock_demand"),
                               "forecast": ("restock_forecast_key", "restock_forecast"),
                               "restock": ("restock_request_key", "restock_request")}.items():
            producers[name] = SerializingProducer({
                **auth, "enable.idempotence": True, "acks": "all",
                "key.serializer": AvroSerializer(sr, (schemas / f"{kf}.avsc").read_text()),
                "value.serializer": AvroSerializer(sr, (schemas / f"{vf}.avsc").read_text()),
            })
    # Offers layer: enabled only when OFFERS_ENABLED=true. Stands in for overlay/flink/cart_at_risk.sql.
    offers = offers_enabled(env)
    carts_topic = env.get("CARTS_TOPIC", "carts.events")
    at_risk_topic = env.get("AT_RISK_TOPIC", "carts.at-risk")
    risk_producer = None
    tracker = CartAtRiskTracker()
    if offers:
        risk_producer = SerializingProducer({
            **auth, "enable.idempotence": True, "acks": "all",
            "key.serializer": AvroSerializer(sr, (schemas / "cart_at_risk_key.avsc").read_text()),
            "value.serializer": AvroSerializer(sr, (schemas / "cart_at_risk.avsc").read_text()),
        })
    restocker = Restocker()
    window = DemandWindow()
    forecasts = ForecastTracker()
    consumer.subscribe([in_topic] + [topics[n] for n in ('movements', 'orders', 'config') if n in topics]
                       + ([carts_topic] if offers else []))
    stop = False

    def _stop(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    agg = Aggregator()

    def on_delivery(err, _msg):
        if err:
            raise SystemExit(f"sellable-dev: delivery to {_msg.topic() if _msg else out_topic} failed: {err}")

    def emit(name, kv):
        producers[name].produce(topics[name], key=kv[0], value=kv[1], on_delivery=on_delivery)

    def emit_risks(changes):
        for key, value in changes:
            risk_producer.produce(at_risk_topic, key=key, value=value, on_delivery=on_delivery)

    def now_ms() -> int:
        return int(time.time() * 1000)

    try:
        while not stop:
            msg = consumer.poll(1.0)
            producer.poll(0)
            if risk_producer is not None:
                risk_producer.poll(0)
                emit_risks(tracker.sweep(now_ms()))
            for p in producers.values():
                p.poll(0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise SystemExit(f"sellable-dev: consume error on {in_topic}: {msg.error()}")
            topic = msg.topic()
            if topic == in_topic:
                if msg.value() is None:
                    raise SystemExit(f"sellable-dev: unexpected tombstone on {in_topic} at offset {msg.offset()}")
                key, value = agg.apply(msg.value())
                producer.produce(out_topic, key=key, value=value, on_delivery=on_delivery)
                if offers:
                    emit_risks(tracker.on_sellable(value, now_ms()))
                requests = restocker.on_state(msg.value()) if restock_topic else []
            elif msg.value() is None:
                continue  # legitimate tombstone on a config or Debezium topic (deletes are never enabled)
            elif offers and topic == carts_topic:
                requests = []
                emit_risks(tracker.on_cart(msg.value(), now_ms()))
            elif topic == topics.get("movements"):
                out = window.apply(msg.value())
                requests = []
                if out is not None:
                    emit("demand", out)
                    requests = restocker.on_demand(out[1])
            elif topic == topics.get("orders"):
                out = forecasts.apply(msg.value())
                requests = []
                if out is not None:
                    emit("forecast", out)
                    requests = restocker.on_forecast(out[1])
            elif topic == topics.get("config"):
                requests = restocker.on_config(msg.value())
            else:
                raise SystemExit(f"sellable-dev: record from unexpected topic {topic!r}")
            for req in requests:
                emit("restock", req)
    finally:
        producer.flush(30)
        for p in producers.values():
            p.flush(30)
        if risk_producer is not None:
            risk_producer.flush(30)
        consumer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
