from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

REQUIRED = ("STORE_HOSTS", "PG_PORT", "PG_DATABASE", "PG_WRITER_USER", "PG_WRITER_PASSWORD",
            "PROCUREMENT_HOST", "PROCUREMENT_DATABASE", "PROCUREMENT_USER", "PROCUREMENT_PASSWORD",
            "REDIS_URL", "DEFAULT_LEAD_TIME_S", "DD_AGENT_HOST", "DD_ENV", "DD_SERVICE", "DD_VERSION")


def parse_store_hosts(raw: str) -> dict[str, str]:
    """STORE_HOSTS format `S01=store-s01,S02=store-s02` -> {store_id: host}, order kept. Strict."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        sid, sep, host = part.partition("=")
        if not sep or not sid.strip() or not host.strip() or sid != sid.strip() or host != host.strip():
            raise SystemExit(f"supplier-sim: STORE_HOSTS entry {part!r} is not STORE_ID=host (value {raw!r})")
        if sid in out:
            raise SystemExit(f"supplier-sim: STORE_HOSTS lists store {sid!r} twice")
        out[sid] = host
    return out


def parse_lead_time(raw: str, where: str) -> int:
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{where}: lead time {raw!r} is not an integer number of seconds") from None
    if v < 0:
        raise ValueError(f"{where}: lead time {v} is negative")
    return v


@dataclass(frozen=True)
class Config:
    store_hosts: dict[str, str]
    pg_port: int
    pg_database: str
    pg_writer_user: str
    pg_writer_password: str
    procurement_host: str
    procurement_database: str
    procurement_user: str
    procurement_password: str
    redis_url: str
    default_lead_time_s: int
    dd_agent_host: str
    dd_env: str
    dd_service: str
    dd_version: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        missing = [n for n in REQUIRED if not env.get(n)]
        if missing:
            raise SystemExit(f"supplier-sim: missing required environment variable(s): {', '.join(missing)}")
        try:
            port = int(env["PG_PORT"])
            default_lead = parse_lead_time(env["DEFAULT_LEAD_TIME_S"], "DEFAULT_LEAD_TIME_S")
        except ValueError as exc:
            raise SystemExit(f"supplier-sim: invalid environment: {exc}") from None
        return cls(parse_store_hosts(env["STORE_HOSTS"]), port, env["PG_DATABASE"], env["PG_WRITER_USER"],
                   env["PG_WRITER_PASSWORD"], env["PROCUREMENT_HOST"], env["PROCUREMENT_DATABASE"],
                   env["PROCUREMENT_USER"], env["PROCUREMENT_PASSWORD"], env["REDIS_URL"], default_lead,
                   env["DD_AGENT_HOST"], env["DD_ENV"], env["DD_SERVICE"], env["DD_VERSION"])
