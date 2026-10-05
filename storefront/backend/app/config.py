"""Environment configuration. Fails at startup naming the missing variable (contracts §8)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ConfigError(RuntimeError):
    pass


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"storefront config: required environment variable {name} is missing or empty")
    return value


def _bool(env: Mapping[str, str], name: str, default: str) -> bool:
    raw = env.get(name, default).strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigError(f"storefront config: {name}={raw!r} is not a boolean (use true/false)")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"storefront config: {name}={raw!r} is not an integer") from exc
    if value <= 0:
        raise ConfigError(f"storefront config: {name} must be positive, got {value}")
    return value


PROTOCOLS = ("SASL_SSL", "PLAINTEXT")
MAX_OFFER_POLL_MS = 2_000


def _protocol(env: Mapping[str, str]) -> str:
    raw = env.get("KAFKA_SECURITY_PROTOCOL") or "SASL_SSL"
    if raw not in PROTOCOLS:
        raise ConfigError(f"storefront config: KAFKA_SECURITY_PROTOCOL={raw!r} is not one of {', '.join(PROTOCOLS)}")
    return raw


def _auth(env: Mapping[str, str], name: str, protocol: str) -> str | None:
    """Credentials are required with SASL_SSL; with PLAINTEXT (local rehearsal, no auth) they are optional."""
    if protocol == "PLAINTEXT":
        return env.get(name) or None
    return _required(env, name)


def _rum_pair(env: Mapping[str, str]) -> None:
    """RUM is on only when both ids are set; exactly one set is a misconfiguration and fails loudly."""
    has_app, has_tok = bool(env.get("DD_RUM_APPLICATION_ID")), bool(env.get("DD_RUM_CLIENT_TOKEN"))
    if has_app != has_tok:
        missing = "DD_RUM_CLIENT_TOKEN" if has_app else "DD_RUM_APPLICATION_ID"
        raise ConfigError(f"storefront config: RUM needs both DD_RUM_APPLICATION_ID and DD_RUM_CLIENT_TOKEN; {missing} is missing or empty")


@dataclass(frozen=True)
class Config:
    kafka_bootstrap: str
    kafka_api_key: str | None
    kafka_api_secret: str | None
    sr_url: str
    sr_api_key: str | None
    sr_api_secret: str | None
    redis_url: str
    dd_env: str
    dd_version: str
    dd_service: str
    offers_enabled: bool
    poll_ms: int
    avro_dir: str
    static_dir: str
    cart_topic: str
    offers_topic: str
    offers_group_id: str
    sr_auto_register: bool
    max_display_delay_s: int
    security_protocol: str = "SASL_SSL"
    rum_application_id: str | None = None
    rum_client_token: str | None = None
    dd_site: str = "datadoghq.eu"
    stack: str = "dev"
    exposure_budget_ms: int = 800

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        protocol = _protocol(env)
        _rum_pair(env)
        poll_ms = _int(env, "POLL_MS", 1000)
        if poll_ms > MAX_OFFER_POLL_MS:
            raise ConfigError(f"storefront config: POLL_MS must be at most {MAX_OFFER_POLL_MS}, got {poll_ms}")
        return cls(
            kafka_bootstrap=_required(env, "KAFKA_BOOTSTRAP"),
            kafka_api_key=_auth(env, "KAFKA_API_KEY", protocol),
            kafka_api_secret=_auth(env, "KAFKA_API_SECRET", protocol),
            sr_url=_required(env, "SR_URL"),
            sr_api_key=_auth(env, "SR_API_KEY", protocol),
            sr_api_secret=_auth(env, "SR_API_SECRET", protocol),
            security_protocol=protocol,
            redis_url=_required(env, "REDIS_URL"),
            dd_env=_required(env, "DD_ENV"),
            dd_version=_required(env, "DD_VERSION"),
            dd_service=_required(env, "DD_SERVICE"),
            offers_enabled=_bool(env, "OFFERS_ENABLED", "true"),
            poll_ms=poll_ms,
            avro_dir=env.get("AVRO_DIR", "/app/contracts/avro"),
            static_dir=env.get("STATIC_DIR", "/app/static"),
            cart_topic=env.get("CART_TOPIC", "carts.events"),
            offers_topic=env.get("OFFERS_TOPIC", "offers"),
            offers_group_id=env.get("OFFERS_GROUP_ID", "storefront-offers"),
            sr_auto_register=_bool(env, "SR_AUTO_REGISTER", "true"),
            max_display_delay_s=_int(env, "MAX_DISPLAY_DELAY_S", 3600),
            rum_application_id=env.get("DD_RUM_APPLICATION_ID") or None,
            rum_client_token=env.get("DD_RUM_CLIENT_TOKEN") or None,
            dd_site=env.get("DD_SITE") or "datadoghq.eu",
            stack=env.get("STACK") or "dev",
            exposure_budget_ms=_int(env, "EXPOSURE_BUDGET_MS", 800),
        )
