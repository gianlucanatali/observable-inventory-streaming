import pytest

from app.config import Config, ConfigError
from app.kafka_io import _kafka_auth, schema_registry_conf
from tests.conftest import ENV

PLAIN = {k: v for k, v in ENV.items() if k not in ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET")}
PLAIN = {**PLAIN, "SR_URL": "http://sr:8081", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}


def test_default_is_sasl_ssl_with_sasl_settings():
    cfg = Config.from_env({**ENV, "SR_URL": "http://sr:8081"})
    assert cfg.security_protocol == "SASL_SSL"
    assert _kafka_auth(cfg) == {"bootstrap.servers": "b:9092", "security.protocol": "SASL_SSL",
                                "sasl.mechanisms": "PLAIN", "sasl.username": "k", "sasl.password": "s"}
    assert schema_registry_conf(cfg) == {"url": "http://sr:8081", "basic.auth.user.info": "k:s"}


@pytest.mark.parametrize("missing", ["KAFKA_API_KEY", "KAFKA_API_SECRET", "SR_API_KEY", "SR_API_SECRET"])
def test_sasl_ssl_requires_credentials(missing):
    env = dict(ENV)
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        Config.from_env(env)


def test_plaintext_needs_no_credentials_and_sets_no_sasl():
    cfg = Config.from_env(PLAIN)
    conf = _kafka_auth(cfg)
    assert conf == {"bootstrap.servers": "b:9092", "security.protocol": "PLAINTEXT"}
    assert schema_registry_conf(cfg) == {"url": "http://sr:8081"}


def test_plaintext_with_sr_key_uses_basic_auth_but_no_sasl():
    cfg = Config.from_env({**PLAIN, "SR_API_KEY": "dev", "SR_API_SECRET": "dev2"})
    assert "sasl.username" not in _kafka_auth(cfg)
    assert schema_registry_conf(cfg)["basic.auth.user.info"] == "dev:dev2"


def test_unknown_protocol_fails_loudly():
    with pytest.raises(ConfigError, match="KAFKA_SECURITY_PROTOCOL"):
        Config.from_env({**ENV, "KAFKA_SECURITY_PROTOCOL": "SSL"})


def test_poll_interval_above_offer_render_budget_fails_loudly():
    with pytest.raises(ConfigError, match="POLL_MS.*2000"):
        Config.from_env({**ENV, "POLL_MS": "2001"})
