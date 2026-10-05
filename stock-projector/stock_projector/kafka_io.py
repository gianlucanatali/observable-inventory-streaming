"""Thin Kafka adapters: Avro DeserializingConsumer source and Avro SerializingProducer publisher."""
from __future__ import annotations

from pathlib import Path

from confluent_kafka import DeserializingConsumer, KafkaError, KafkaException, SerializingProducer, TopicPartition
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

from .config import Config
from .core import ProjectorError
from .projector import Record

POLL_TIMEOUT_S = 1.0
DELIVERY_TIMEOUT_S = 30.0


def kafka_auth(cfg: Config) -> dict:
    conf = {"bootstrap.servers": cfg.kafka_bootstrap, "security.protocol": cfg.security_protocol}
    if cfg.security_protocol != "PLAINTEXT":
        conf.update({"sasl.mechanisms": "PLAIN", "sasl.username": cfg.kafka_api_key, "sasl.password": cfg.kafka_api_secret})
    return conf


def schema_registry_conf(cfg: Config) -> dict:
    conf = {"url": cfg.sr_url}
    if cfg.sr_api_key:
        conf["basic.auth.user.info"] = f"{cfg.sr_api_key}:{cfg.sr_api_secret}"
    return conf


def schema_registry(cfg: Config) -> SchemaRegistryClient:
    return SchemaRegistryClient(schema_registry_conf(cfg))


class KafkaSource:
    def __init__(self, cfg: Config, sr: SchemaRegistryClient):
        self._c = DeserializingConsumer({
            **kafka_auth(cfg), "group.id": cfg.group_id, "enable.auto.commit": False, "auto.offset.reset": "earliest",
            "key.deserializer": AvroDeserializer(sr), "value.deserializer": AvroDeserializer(sr),
        })
        self._c.subscribe([cfg.cdc_topic])

    def poll(self) -> Record | None:
        try:
            msg = self._c.poll(POLL_TIMEOUT_S)
        except KafkaException as e:  # includes ValueDeserializationError / KeyDeserializationError
            raise ProjectorError("deserialization", f"consume failed: {e}") from e
        if msg is None:
            return None
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None
            raise ProjectorError("consume_error", str(msg.error()))
        if msg.value() is None:
            raise ProjectorError("invalid_envelope", f"tombstone at offset {msg.offset()}")
        return Record(msg.topic(), msg.partition(), msg.offset(), msg.value(), raw=msg)

    def poll_batch(self, max_records: int) -> list[Record]:
        """First record waits up to POLL_TIMEOUT_S; the rest are only those already fetched (poll(0))."""
        out: list[Record] = []
        rec = self.poll()
        while rec is not None:
            out.append(rec)
            if len(out) >= max_records:
                break
            rec = self._poll_now()
        return out

    def _poll_now(self) -> Record | None:
        try:
            msg = self._c.poll(0)
        except KafkaException as e:
            raise ProjectorError("deserialization", f"consume failed: {e}") from e
        if msg is None:
            return None
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None
            raise ProjectorError("consume_error", str(msg.error()))
        if msg.value() is None:
            raise ProjectorError("invalid_envelope", f"tombstone at offset {msg.offset()}")
        return Record(msg.topic(), msg.partition(), msg.offset(), msg.value(), raw=msg)

    def commit(self, record: Record) -> None:
        try:
            self._c.commit(message=record.raw, asynchronous=False)
        except KafkaException as e:
            raise ProjectorError("commit_failed", f"offset {record.offset}: {e}") from e

    def close(self) -> None:
        self._c.close()


class KafkaPublisher:
    def __init__(self, cfg: Config, sr: SchemaRegistryClient, topic: str, key_schema: str, value_schema: str):
        d = Path(cfg.schema_dir)
        self._topic = topic
        self._p = SerializingProducer({
            **kafka_auth(cfg), "enable.idempotence": True, "acks": "all",
            "key.serializer": AvroSerializer(sr, (d / key_schema).read_text()),
            "value.serializer": AvroSerializer(sr, (d / value_schema).read_text()),
        })
        self._errors: list = []

    def produce(self, key: dict, value: dict) -> None:
        try:
            self._p.produce(self._topic, key=key, value=value,
                            on_delivery=lambda err, _m: err and self._errors.append(err))
            self._p.poll(0)   # serve delivery callbacks, keep the local queue moving
        except Exception as e:  # serialization, schema registry, queue full
            raise ProjectorError("publish_failed", f"{type(e).__name__}: {e}") from e

    def confirm(self) -> None:
        try:
            remaining = self._p.flush(DELIVERY_TIMEOUT_S)
        except Exception as e:
            raise ProjectorError("publish_failed", f"{type(e).__name__}: {e}") from e
        errors, self._errors = self._errors, []
        if errors:
            raise ProjectorError("publish_failed", f"delivery error: {errors[0]}")
        if remaining:
            raise ProjectorError("publish_failed", f"delivery not confirmed within {DELIVERY_TIMEOUT_S}s")
