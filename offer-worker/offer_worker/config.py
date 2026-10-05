from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

NAME = "offer-worker"
REQUIRED = ("KAFKA_BOOTSTRAP", "SR_URL", "REDIS_URL", "DD_AGENT_HOST", "DD_ENV", "DD_SERVICE", "DD_VERSION")
# Required unless KAFKA_SECURITY_PROTOCOL=PLAINTEXT (local rehearsal broker and Schema Registry have no auth).
REQUIRED_AUTH = ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET")
PROTOCOLS = ("SASL_SSL", "PLAINTEXT")
TRUE = ("1", "true", "yes", "on")
FALSE = ("", "0", "false", "no", "off")
# Fixed policy terms. The worker can never offer more than this, whatever Jev or Bedrock say.
MAX_DISCOUNT_PCT = 15


def _bool(env: Mapping[str, str], name: str) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise SystemExit(f"{NAME}: {name}={raw!r} is not a boolean (use true/false)")


def _num(env: Mapping[str, str], name: str, default: str, cast, lo, hi):
    raw = env.get(name) or default
    try:
        v = cast(raw)
    except ValueError:
        raise SystemExit(f"{NAME}: {name}={raw!r} is not a valid {cast.__name__}") from None
    if not lo <= v <= hi:
        raise SystemExit(f"{NAME}: {name}={v} is outside {lo}..{hi}")
    return v


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
    security_protocol: str = "SASL_SSL"
    group_id: str = "offer-worker"
    risk_topic: str = "carts.at-risk"
    offers_topic: str = "offers"
    schema_dir: str = "/app/contracts/avro"
    catalogue_file: str = "/app/catalogue/products.json"
    jev_api_key: str | None = None
    jev_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"
    jev_timeout_ms: int = 800
    jev_min_confidence: float = 0.8
    kill_switch: bool = False
    kill_switch_key: str = "offers:kill_switch"
    bedrock_enabled: bool = False
    bedrock_model_id: str | None = None
    bedrock_timeout_ms: int = 3000
    aws_region: str | None = None
    discount_pct: int = 10
    max_risk_age_s: int = 300
    dedup_size: int = 10_000

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        protocol = env.get("KAFKA_SECURITY_PROTOCOL") or "SASL_SSL"
        if protocol not in PROTOCOLS:
            raise SystemExit(f"{NAME}: KAFKA_SECURITY_PROTOCOL={protocol!r} is not one of {', '.join(PROTOCOLS)}")
        needed = list(REQUIRED if protocol == "PLAINTEXT" else REQUIRED + REQUIRED_AUTH)
        bedrock = _bool(env, "BEDROCK_ENABLED")
        if bedrock:
            needed += ["AWS_REGION", "BEDROCK_MODEL_ID"]
        missing = [n for n in needed if not env.get(n)]
        if missing:
            raise SystemExit(f"{NAME}: missing required environment variable(s): {', '.join(missing)}")
        discount = _num(env, "OFFER_DISCOUNT_PCT", "10", int, 0, MAX_DISCOUNT_PCT)
        return cls(
            env["KAFKA_BOOTSTRAP"], env.get("KAFKA_API_KEY") or None, env.get("KAFKA_API_SECRET") or None,
            env["SR_URL"], env.get("SR_API_KEY") or None, env.get("SR_API_SECRET") or None, env["REDIS_URL"],
            env["DD_AGENT_HOST"], env["DD_ENV"], env["DD_SERVICE"], env["DD_VERSION"],
            security_protocol=protocol,
            group_id=env.get("KAFKA_GROUP_ID", "offer-worker"),
            risk_topic=env.get("RISK_TOPIC", "carts.at-risk"),
            offers_topic=env.get("OFFERS_TOPIC", "offers"),
            schema_dir=env.get("SCHEMA_DIR", "/app/contracts/avro"),
            catalogue_file=env.get("CATALOGUE_FILE", "/app/catalogue/products.json"),
            jev_api_key=env.get("JEV_API_KEY") or None,
            jev_url=env.get("JEV_URL") or "https://api.typesafe.ai/v1/systemone",
            jev_model=env.get("JEV_MODEL") or "jev-latest",
            jev_timeout_ms=_num(env, "JEV_TIMEOUT_MS", "800", int, 50, 10_000),
            jev_min_confidence=_num(env, "JEV_MIN_CONFIDENCE", "0.8", float, 0.0, 1.0),
            kill_switch=_bool(env, "OFFERS_KILL_SWITCH"),
            kill_switch_key=env.get("OFFERS_KILL_SWITCH_KEY", "offers:kill_switch"),
            bedrock_enabled=bedrock,
            bedrock_model_id=env.get("BEDROCK_MODEL_ID") or None,
            bedrock_timeout_ms=_num(env, "BEDROCK_TIMEOUT_MS", "3000", int, 100, 30_000),
            aws_region=env.get("AWS_REGION") or None,
            discount_pct=discount,
            max_risk_age_s=_num(env, "MAX_RISK_AGE_S", "300", int, 1, 86_400),
            dedup_size=_num(env, "DEDUP_SIZE", "10000", int, 1, 1_000_000),
        )
