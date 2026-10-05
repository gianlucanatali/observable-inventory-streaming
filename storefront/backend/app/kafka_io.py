"""Real Kafka / Schema Registry / Redis / DogStatsD adapters. Not exercised by unit tests
(they use fakes); the Avro round trip against the real schema files is tested with mock://."""
from __future__ import annotations

import os
from pathlib import Path

from confluent_kafka import DeserializingConsumer, KafkaError, SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

from .config import Config

PARTITION_EOF = KafkaError._PARTITION_EOF


def schema_registry_conf(cfg: Config) -> dict:
    conf = {"url": cfg.sr_url}
    if cfg.sr_api_key and not cfg.sr_url.startswith("mock://"):
        conf["basic.auth.user.info"] = f"{cfg.sr_api_key}:{cfg.sr_api_secret}"
    return conf


def schema_registry(cfg: Config) -> SchemaRegistryClient:
    return SchemaRegistryClient.new_client(schema_registry_conf(cfg))


def _schema(cfg: Config, name: str) -> str:
    path = Path(cfg.avro_dir) / name
    if not path.is_file():
        raise FileNotFoundError(f"Avro schema {path} not found (AVRO_DIR={cfg.avro_dir})")
    return path.read_text()


def _kafka_auth(cfg: Config) -> dict:
    conf = {"bootstrap.servers": cfg.kafka_bootstrap, "security.protocol": cfg.security_protocol}
    if cfg.security_protocol != "PLAINTEXT":
        conf.update({"sasl.mechanisms": "PLAIN", "sasl.username": cfg.kafka_api_key,
                     "sasl.password": cfg.kafka_api_secret})
    return conf


def cart_serializers(cfg: Config, sr: SchemaRegistryClient):
    conf = {"auto.register.schemas": cfg.sr_auto_register}
    if not cfg.sr_auto_register:
        conf["use.latest.version"] = True
    return (
        AvroSerializer(sr, _schema(cfg, "cart_key.avsc"), conf=dict(conf)),
        AvroSerializer(sr, _schema(cfg, "cart_event.avsc"), conf=dict(conf)),
    )


class CartPublisher:
    """Publishes one CartEvent and waits for the broker acknowledgement."""

    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        key_ser, value_ser = cart_serializers(cfg, sr)
        self._topic = cfg.cart_topic
        self._producer = SerializingProducer({
            **_kafka_auth(cfg),
            "client.id": "storefront-cart-producer",
            "enable.idempotence": True,
            "key.serializer": key_ser,
            "value.serializer": value_ser,
        })

    def publish(self, key: dict, value: dict) -> None:
        errors: list = []
        self._producer.produce(self._topic, key=key, value=value,
                               on_delivery=lambda err, _msg: errors.append(err) if err else None)
        remaining = self._producer.flush(10)
        if errors:
            raise RuntimeError(f"Kafka delivery to {self._topic} failed: {errors[0]}")
        if remaining:
            raise RuntimeError(f"Kafka delivery to {self._topic} timed out with {remaining} message(s) unsent")


def offers_consumer(cfg: Config, sr: SchemaRegistryClient) -> DeserializingConsumer:
    consumer = DeserializingConsumer({
        **_kafka_auth(cfg),
        "client.id": "storefront-offers-consumer",
        "group.id": cfg.offers_group_id,
        "auto.offset.reset": "earliest",
        # In-memory state: always replay the compacted topic on restart, so never commit.
        "enable.auto.commit": False,
        # The offer worker may publish before its Flink transaction commits;
        # offer_id is deterministic, so duplicate correct records are safe.
        "isolation.level": "read_uncommitted",
        "key.deserializer": AvroDeserializer(sr, _schema(cfg, "offer_key.avsc")),
        "value.deserializer": AvroDeserializer(sr, _schema(cfg, "offer.avsc")),
    })
    consumer.subscribe([cfg.offers_topic])
    return consumer


def make_redis(cfg: Config):
    import redis

    return redis.Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def make_statsd(cfg: Config):
    from datadog import DogStatsd

    return DogStatsd(
        host=os.environ.get("DD_AGENT_HOST", "localhost"),
        port=int(os.environ.get("DD_DOGSTATSD_PORT", "8125")),
        constant_tags=[f"env:{cfg.dd_env}", f"service:{cfg.dd_service}", f"version:{cfg.dd_version}"],
    )
