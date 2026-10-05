"""Kafka adapters for demo.config: Avro producer (blocking send) and whole-topic reader."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from confluent_kafka import KafkaError, OFFSET_BEGINNING, TopicPartition
from confluent_kafka import DeserializingConsumer, SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

from .config import Config

DELIVERY_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 15.0


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


def _schema(cfg: Config, name: str) -> str:
    return (Path(cfg.schema_dir) / name).read_text()


class ConfigProducer:
    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        self._topic = cfg.config_topic
        conf = {"auto.register.schemas": cfg.sr_auto_register}
        if not cfg.sr_auto_register:
            conf["use.latest.version"] = True
        self._p = SerializingProducer({
            **kafka_auth(cfg), "enable.idempotence": True, "acks": "all",
            "key.serializer": AvroSerializer(sr, _schema(cfg, "demo_config_key.avsc"), conf=dict(conf)),
            "value.serializer": AvroSerializer(sr, _schema(cfg, "demo_config.avsc"), conf=dict(conf)),
        })

    def send(self, key: dict, value: dict) -> None:
        errors: list = []
        self._p.produce(self._topic, key=key, value=value, on_delivery=lambda err, _m: err and errors.append(err))
        remaining = self._p.flush(DELIVERY_TIMEOUT_S)
        if errors:
            raise RuntimeError(f"delivery error: {errors[0]}")
        if remaining:
            raise RuntimeError(f"delivery not confirmed within {DELIVERY_TIMEOUT_S}s")


class ConfigReader:
    """Reads the whole compacted topic to its current end with a throwaway consumer (no group state kept)."""

    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        self._cfg, self._sr = cfg, sr

    def __call__(self) -> dict[str, float]:
        cfg = self._cfg
        c = DeserializingConsumer({
            **kafka_auth(cfg), "group.id": f"demo-control-read-{uuid.uuid4().hex[:8]}", "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "key.deserializer": AvroDeserializer(self._sr, _schema(cfg, "demo_config_key.avsc")),
            "value.deserializer": AvroDeserializer(self._sr, _schema(cfg, "demo_config.avsc")),
        })
        try:
            md = c.list_topics(cfg.config_topic, timeout=READ_TIMEOUT_S)
            tmd = md.topics.get(cfg.config_topic)
            if tmd is None or (tmd.error is not None and tmd.error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART):
                return {}  # not created yet: empty; the producer fails loudly if it cannot create it
            if tmd.error is not None:
                raise RuntimeError(f"topic {cfg.config_topic}: {tmd.error}")
            ends: dict[int, int] = {}
            for part in tmd.partitions:
                tp = TopicPartition(cfg.config_topic, part, OFFSET_BEGINNING)
                lo, hi = c.get_watermark_offsets(tp, timeout=READ_TIMEOUT_S)
                if hi > lo:
                    ends[part] = hi
            c.assign([TopicPartition(cfg.config_topic, part, OFFSET_BEGINNING) for part in ends])
            values: dict[str, float] = {}
            deadline = time.monotonic() + READ_TIMEOUT_S
            while ends:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"reading {cfg.config_topic} did not reach the end within {READ_TIMEOUT_S}s")
                msg = c.poll(0.5)
                if msg is None:
                    continue
                if msg.error():
                    raise RuntimeError(f"read error on {cfg.config_topic}: {msg.error()}")
                if msg.value() is None:
                    values.pop(msg.key()["key"], None)
                else:
                    values[msg.key()["key"]] = float(msg.value()["value"])
                if msg.offset() + 1 >= ends.get(msg.partition(), 0):
                    ends.pop(msg.partition(), None)
            return values
        finally:
            c.close()
