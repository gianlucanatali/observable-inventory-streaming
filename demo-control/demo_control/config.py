"""Environment configuration. Fails at startup naming every missing or invalid variable."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

PROTOCOLS = ("SASL_SSL", "PLAINTEXT")
REQUIRED = ("CONTROL_PASSWORD", "REDIS_URL", "KAFKA_BOOTSTRAP", "SR_URL", "STACK")
REQUIRED_AUTH = ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET")


class ConfigError(RuntimeError):
    pass


def parse_store_hosts(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse STORE_HOSTS entries whose endpoint may include a published TCP port."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        sid, sep, host = part.partition("=")
        if not sep or not sid or not host or sid != sid.strip() or host != host.strip():
            raise ConfigError(f"STORE_HOSTS entry {part!r} is not STORE_ID=host (value {raw!r})")
        if sid in [p[0] for p in pairs]:
            raise ConfigError(f"STORE_HOSTS lists store {sid!r} twice")
        pairs.append((sid, host))
    return tuple(pairs)


def split_host_port(endpoint: str, default_port: int) -> tuple[str, int]:
    """Split the IPv4/DNS host:port form used by the hybrid VM port mappings."""
    host, separator, raw_port = endpoint.rpartition(":")
    if not separator:
        return endpoint, default_port
    if not host or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ConfigError(f"STORE_HOSTS endpoint {endpoint!r} has an invalid TCP port")
    return host, int(raw_port)


@dataclass(frozen=True)
class Config:
    control_password: str
    redis_url: str
    kafka_bootstrap: str
    kafka_api_key: str | None
    kafka_api_secret: str | None
    sr_url: str
    sr_api_key: str | None
    sr_api_secret: str | None
    security_protocol: str
    sr_auto_register: bool
    config_topic: str
    stack: str
    registry_file: str
    schema_dir: str
    routing_file: str | None
    stores: tuple[tuple[str, str], ...]  # (store_id, host); empty = no store sources known
    pg_port: int
    pg_database: str | None
    pg_user: str | None
    pg_password: str | None
    procurement_host: str | None
    procurement_port: int
    procurement_database: str | None
    procurement_user: str | None
    procurement_password: str | None
    statsd_host: str
    statsd_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        errors: list[str] = []
        protocol = env.get("KAFKA_SECURITY_PROTOCOL") or "SASL_SSL"
        if protocol not in PROTOCOLS:
            errors.append(f"KAFKA_SECURITY_PROTOCOL={protocol!r} is not one of {', '.join(PROTOCOLS)}")
        needed = REQUIRED if protocol == "PLAINTEXT" else REQUIRED + REQUIRED_AUTH
        errors += [f"missing required environment variable {n}" for n in needed if not env.get(n)]

        def port(name: str, default: str) -> int:
            raw = env.get(name) or default
            try:
                return int(raw)
            except ValueError:
                errors.append(f"{name}={raw!r} is not an integer")
                return 0

        pg_port, proc_port, statsd_port = port("PG_PORT", "5432"), port("PROCUREMENT_PORT", "5432"), port("DD_DOGSTATSD_PORT", "8125")
        stores: tuple[tuple[str, str], ...] = ()
        if env.get("STORE_HOSTS"):
            try:
                stores = parse_store_hosts(env["STORE_HOSTS"])
            except ConfigError as exc:
                errors.append(str(exc))
            for n in ("PG_DATABASE", "PG_WRITER_USER", "PG_WRITER_PASSWORD"):
                if not env.get(n):
                    errors.append(f"STORE_HOSTS is set, so {n} is required")
        if env.get("PROCUREMENT_HOST"):
            for n in ("PROCUREMENT_DATABASE", "PROCUREMENT_USER", "PROCUREMENT_PASSWORD"):
                if not env.get(n):
                    errors.append(f"PROCUREMENT_HOST is set, so {n} is required")
        auto = (env.get("SR_AUTO_REGISTER") or "true").strip().lower()
        if auto not in ("true", "false", "1", "0"):
            errors.append(f"SR_AUTO_REGISTER={auto!r} is not true/false")
        if errors:
            raise ConfigError("demo-control configuration invalid: " + "; ".join(errors))
        return cls(
            control_password=env["CONTROL_PASSWORD"], redis_url=env["REDIS_URL"],
            kafka_bootstrap=env["KAFKA_BOOTSTRAP"], kafka_api_key=env.get("KAFKA_API_KEY") or None,
            kafka_api_secret=env.get("KAFKA_API_SECRET") or None, sr_url=env["SR_URL"],
            sr_api_key=env.get("SR_API_KEY") or None, sr_api_secret=env.get("SR_API_SECRET") or None,
            security_protocol=protocol, sr_auto_register=auto in ("true", "1"),
            config_topic=env.get("CONFIG_TOPIC", "demo.config"), stack=env["STACK"],
            registry_file=env.get("REGISTRY_FILE", "/app/contracts/demo-params.json"),
            schema_dir=env.get("SCHEMA_DIR", "/app/contracts/avro"),
            routing_file=env.get("ROUTING_FILE") or None,
            stores=stores, pg_port=pg_port, pg_database=env.get("PG_DATABASE") or None,
            pg_user=env.get("PG_WRITER_USER") or None, pg_password=env.get("PG_WRITER_PASSWORD") or None,
            procurement_host=env.get("PROCUREMENT_HOST") or None, procurement_port=proc_port,
            procurement_database=env.get("PROCUREMENT_DATABASE") or None,
            procurement_user=env.get("PROCUREMENT_USER") or None,
            procurement_password=env.get("PROCUREMENT_PASSWORD") or None,
            statsd_host=env.get("DD_AGENT_HOST", "localhost"), statsd_port=statsd_port,
        )
