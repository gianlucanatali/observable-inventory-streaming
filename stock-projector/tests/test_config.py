import pytest

from stock_projector.config import Config, parse_store_hosts
from stock_projector.kafka_io import kafka_auth, schema_registry_conf

BASE = {
    "STORE_HOSTS": "S01=store-s01,S02=store-s02",
    "KAFKA_BOOTSTRAP": "b:9092", "SR_URL": "http://sr:8081", "REDIS_URL": "redis://r/0",
    "DD_AGENT_HOST": "agent", "DD_ENV": "dd-demo", "DD_SERVICE": "stock-projector", "DD_VERSION": "1.0.0",
}
AUTH = {"KAFKA_API_KEY": "k", "KAFKA_API_SECRET": "s", "SR_API_KEY": "sk", "SR_API_SECRET": "ss"}


def test_default_is_sasl_ssl_with_credentials():
    cfg = Config.from_env({**BASE, **AUTH})
    assert cfg.security_protocol == "SASL_SSL"
    assert kafka_auth(cfg) == {
        "bootstrap.servers": "b:9092", "security.protocol": "SASL_SSL", "sasl.mechanisms": "PLAIN",
        "sasl.username": "k", "sasl.password": "s",
    }
    assert schema_registry_conf(cfg) == {"url": "http://sr:8081", "basic.auth.user.info": "sk:ss"}


@pytest.mark.parametrize("missing", sorted(AUTH))
def test_sasl_ssl_requires_each_credential(missing):
    env = {**BASE, **AUTH}
    del env[missing]
    with pytest.raises(SystemExit, match=missing):
        Config.from_env(env)


def test_plaintext_needs_no_credentials_and_sets_no_sasl():
    cfg = Config.from_env({**BASE, "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"})
    conf = kafka_auth(cfg)
    assert conf == {"bootstrap.servers": "b:9092", "security.protocol": "PLAINTEXT"}
    assert not any(k.startswith("sasl.") for k in conf)
    assert schema_registry_conf(cfg) == {"url": "http://sr:8081"}


def test_plaintext_with_placeholder_sr_key_uses_basic_auth():
    cfg = Config.from_env({**BASE, **AUTH, "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"})
    assert "sasl.username" not in kafka_auth(cfg)
    assert schema_registry_conf(cfg)["basic.auth.user.info"] == "sk:ss"


def test_unknown_protocol_fails_loudly():
    with pytest.raises(SystemExit, match="KAFKA_SECURITY_PROTOCOL"):
        Config.from_env({**BASE, **AUTH, "KAFKA_SECURITY_PROTOCOL": "SSL"})


def test_missing_base_variable_named():
    env = {**BASE, "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}
    del env["REDIS_URL"]
    with pytest.raises(SystemExit, match="REDIS_URL"):
        Config.from_env(env)


def test_store_ids_in_order():
    assert Config.from_env({**BASE, **AUTH}).store_ids == ("S01", "S02")


@pytest.mark.parametrize("raw", ["", "S01", "S01=", "=h", "S01=a,S01=b", "S01=a,,S02=b", "S01 =a", "S01=a, S02=b"])
def test_store_hosts_parsed_strictly(raw):
    with pytest.raises(SystemExit, match="STORE_HOSTS"):
        parse_store_hosts(raw)


def test_store_hosts_required():
    env = {**BASE, **AUTH}
    del env["STORE_HOSTS"]
    with pytest.raises(SystemExit, match="STORE_HOSTS"):
        Config.from_env(env)
