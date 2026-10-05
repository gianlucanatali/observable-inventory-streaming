import re
from pathlib import Path


COMPOSE_DIR = Path(__file__).parents[1]
HYBRID = (COMPOSE_DIR / "compose.hybrid.yaml").read_text()
MAKEFILE = (COMPOSE_DIR.parent / "Makefile").read_text()


def service_section(service):
    marker = HYBRID.index(f"  {service}:")
    match = re.search(r"\n  [^ ]", HYBRID[marker + 3 :])
    end = marker + 3 + match.start() if match else len(HYBRID)
    return HYBRID[marker:end]


def test_hybrid_stack_selects_hybrid_overlay():
    assert "compose.hybrid.yaml" in MAKEFILE
    assert "TOPOLOGY ?= $(if $(filter cloud,$(MODE)),hybrid,vm)" in MAKEFILE


def test_hybrid_overlay_keeps_only_on_prem_services_active():
    # These inherited services are intentionally not assigned cloud-online.
    assert '  connect:\n    depends_on:' in HYBRID
    assert '  watchdog:\n    depends_on:' in HYBRID
    for service in (
        "redis",
        "stock-projector",
        "inventory-api-100",
        "inventory-api-110",
        "inventory-api-120",
        "storefront",
        "nginx",
        "offer-worker",
        "demo-control",
        "cost-meter",
    ):
        assert f"  {service}:" in HYBRID
        marker = HYBRID.index(f"  {service}:")
        assert "profiles: !override [\"cloud-online\"]" in HYBRID[marker : marker + 200]
    agent = service_section("datadog-agent")
    assert "profiles: !reset []" in agent
    assert "configs: !override" in agent
    assert "source: dd_postgres" in agent
    assert "dd_redisdb" not in agent
    assert "dd_nginx" not in agent


def test_hybrid_connect_and_watchdog_use_elasticache_contract():
    assert "ELASTICACHE_REDIS_URL:?ELASTICACHE_REDIS_URL missing" in HYBRID
    assert "REDIS_URI: ${ELASTICACHE_REDIS_URL" in HYBRID
    assert "REDIS_URL: ${ELASTICACHE_REDIS_URL" in HYBRID



def test_hybrid_tools_target_alb_and_connect_on_vm():
    assert "HYBRID_ONLINE_URL:?HYBRID_ONLINE_URL missing" in HYBRID
    assert "BASE_URL: ${HYBRID_ONLINE_URL" in HYBRID
    assert "CONNECT_URL: http://connect:8083" in HYBRID


def test_hybrid_publishes_private_database_ports_for_ecs_demo_control():
    expected_ports = {
        "store-s01": "15431:5432",
        "store-s02": "15432:5432",
        "store-s03": "15433:5432",
        "store-s04": "15434:5432",
        "store-s05": "15435:5432",
        "procurement-db": "15436:5432",
    }
    for service, port in expected_ports.items():
        assert f'ports: ["{port}"]' in service_section(service)


def test_hybrid_keeps_cloud_online_services_disabled_on_the_vm():
    cloud_online = (
        "redis",
        "stock-projector",
        "inventory-api-100",
        "inventory-api-110",
        "inventory-api-120",
        "storefront",
        "nginx",
        "offer-worker",
        "demo-control",
        "cost-meter",
    )
    for service in cloud_online:
        assert 'profiles: !override ["cloud-online"]' in service_section(service)
