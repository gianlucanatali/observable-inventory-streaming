from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

REQUIRED = ("STORE_HOSTS", "KAFKA_BOOTSTRAP", "SR_URL", "REDIS_URL", "DD_AGENT_HOST", "DD_ENV", "DD_SERVICE", "DD_VERSION")
# Required unless KAFKA_SECURITY_PROTOCOL=PLAINTEXT (local rehearsal broker and Schema Registry have no auth).
REQUIRED_AUTH = ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET")
PROTOCOLS = ("SASL_SSL", "PLAINTEXT")


def parse_store_hosts(raw: str) -> tuple[str, ...]:
    """STORE_HOSTS format `S01=store-s01,S02=store-s02`; returns the store ids in order. Strict."""
    ids: list[str] = []
    for part in raw.split(","):
        sid, sep, host = part.partition("=")
        if not sep or not sid.strip() or not host.strip() or sid != sid.strip() or host != host.strip():
            raise SystemExit(f"stock-projector: STORE_HOSTS entry {part!r} is not STORE_ID=host (value {raw!r})")
        if sid in ids:
            raise SystemExit(f"stock-projector: STORE_HOSTS lists store {sid!r} twice")
        ids.append(sid)
    return tuple(ids)


@dataclass(frozen=True)
class Config:
    kafka_bootstrap: str
    kafka_api_key: str | None
    kafka_api_secret: str | None
    sr_url: str
    sr_api_key: str | None
    sr_api_secret: str | None
    redis_url: str
    dd_agent_host: str
    dd_env: str
    dd_service: str
    dd_version: str
    group_id: str
    cdc_topic: str
    state_topic: str
    movements_topic: str
    schema_dir: str
    store_ids: tuple[str, ...] = ()
    security_protocol: str = "SASL_SSL"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        protocol = env.get("KAFKA_SECURITY_PROTOCOL") or "SASL_SSL"
        if protocol not in PROTOCOLS:
            raise SystemExit(f"stock-projector: KAFKA_SECURITY_PROTOCOL={protocol!r} is not one of {', '.join(PROTOCOLS)}")
        needed = REQUIRED if protocol == "PLAINTEXT" else REQUIRED + REQUIRED_AUTH
        missing = [n for n in needed if not env.get(n)]
        if missing:
            raise SystemExit(f"stock-projector: missing required environment variable(s): {', '.join(missing)}")
        return cls(
            env["KAFKA_BOOTSTRAP"], env.get("KAFKA_API_KEY") or None, env.get("KAFKA_API_SECRET") or None,
            env["SR_URL"], env.get("SR_API_KEY") or None, env.get("SR_API_SECRET") or None, env["REDIS_URL"],
            env["DD_AGENT_HOST"], env["DD_ENV"], env["DD_SERVICE"], env["DD_VERSION"],
            group_id=env.get("KAFKA_GROUP_ID", "stock-projector"),
            cdc_topic=env.get("CDC_TOPIC", "inventory.cdc"),
            state_topic=env.get("STATE_TOPIC", "inventory.state"),
            movements_topic=env.get("MOVEMENTS_TOPIC", "stock.movements"),
            schema_dir=env.get("SCHEMA_DIR", "/app/contracts/avro"),
            store_ids=parse_store_hosts(env["STORE_HOSTS"]),
            security_protocol=protocol,
        )
