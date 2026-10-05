"""Kafka adapters: Avro consumer for carts.at-risk, Avro producer for offers."""
from __future__ import annotations

from pathlib import Path

from confluent_kafka import DeserializingConsumer, KafkaError, SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

from .config import Config

DELIVERY_TIMEOUT_MS = 1000


def kafka_auth(cfg: Config) -> dict:
    conf = {"bootstrap.servers": cfg.kafka_bootstrap, "security.protocol": cfg.security_protocol}
    if cfg.security_protocol != "PLAINTEXT":
        conf.update({"sasl.mechanisms": "PLAIN", "sasl.username": cfg.kafka_api_key, "sasl.password": cfg.kafka_api_secret})
    return conf


def schema_registry(cfg: Config) -> SchemaRegistryClient:
    conf = {"url": cfg.sr_url}
    if cfg.sr_api_key:
        conf["basic.auth.user.info"] = f"{cfg.sr_api_key}:{cfg.sr_api_secret}"
    return SchemaRegistryClient(conf)


class RiskSource:
    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        self._c = DeserializingConsumer({
            **kafka_auth(cfg), "group.id": cfg.group_id, "enable.auto.commit": False, "auto.offset.reset": "earliest",
            # The cart-risk Flink lane is deliberately at-least-once for the
            # demo's sub-15-second objective; risk_id deduplicates replays.
            "isolation.level": "read_uncommitted",
            "key.deserializer": AvroDeserializer(sr), "value.deserializer": AvroDeserializer(sr),
        })
        self._c.subscribe([cfg.risk_topic])

    def poll(self):
        """Returns (message, value) or None. A null value is a tombstone: (message, None). Errors raise."""
        msg = self._c.poll(1.0)
        if msg is None:
            return None
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None
            raise RuntimeError(f"carts.at-risk consume error: {msg.error()}")
        return msg, msg.value()

    def commit(self, msg) -> None:
        self._c.commit(message=msg, asynchronous=False)

    def close(self) -> None:
        self._c.close()


class OfferPublisher:
    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        d = Path(cfg.schema_dir)
        self._topic = cfg.offers_topic
        self._p = SerializingProducer({
            **kafka_auth(cfg), "enable.idempotence": True, "acks": "all",
            "key.serializer": AvroSerializer(sr, (d / "offer_key.avsc").read_text()),
            "value.serializer": AvroSerializer(sr, (d / "offer.avsc").read_text()),
        })

    def publish(self, key: dict, value: dict) -> None:
        errors: list = []
        self._p.produce(self._topic, key=key, value=value, on_delivery=lambda err, _m: err and errors.append(err))
        remaining = self._p.flush(DELIVERY_TIMEOUT_MS / 1000)
        if errors:
            raise RuntimeError(f"offers delivery error: {errors[0]}")
        if remaining:
            raise RuntimeError(f"offers delivery not confirmed within {DELIVERY_TIMEOUT_MS}ms")
