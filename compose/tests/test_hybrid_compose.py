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


def test_hybrid_publishes_connect_rest_for_ecs_demo_control_only():
    # The port is published on the VM; terraform/aws admits it only from the demo-control security group.
    assert 'ports: ["8083:8083"]' in service_section("connect")
    assert HYBRID.count("8083:8083") == 1


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


BASE = (COMPOSE_DIR / "compose.yaml").read_text()
GEN_SECRETS = (COMPOSE_DIR / "scripts" / "gen-secrets.sh").read_text()


def base_section(service):
    marker = BASE.index(f"\n  {service}:\n") + 1
    match = re.search(r"\n  [^ #]", BASE[marker + 3 :])
    return BASE[marker : marker + 3 + match.start()]


def test_scenario_api_runs_the_scenario_image_without_docker_socket():
    # The same image and package as the scenario tool, long-running, sharing the scenario-out volume.
    api = base_section("scenario-api")
    assert "image: dd-scenario:${IMAGE_TAG:-dev}" in api and 'command: ["api", "--port", "8090"]' in api
    assert "<<: *scenario-env" in api and "environment: &scenario-env" in base_section("scenario")
    assert "- scenario-out:/out" in api
    assert "SCENARIO_API_TOKEN: ${SCENARIO_API_TOKEN:?SCENARIO_API_TOKEN missing (run make secrets)}" in api
    assert "profiles" not in api  # core: started by `make up-*`
    for text in (api, service_section("scenario-api")):
        assert "docker.sock" not in text and "privileged" not in text and "/var/run" not in text
    assert "ports" not in api  # vm/dev: reached by service name only


def test_hybrid_publishes_scenario_api_for_ecs_demo_control_only():
    section = service_section("scenario-api")
    assert 'ports: ["8090:8090"]' in section and HYBRID.count("8090:8090") == 1
    assert "BASE_URL: ${HYBRID_ONLINE_URL" in section and "REDIS_URL: ${ELASTICACHE_REDIS_URL" in section
    assert "depends_on: !reset []" in section
    assert BASE.count("8090:8090") == 0


def test_demo_control_reaches_scenario_api_by_service_name_in_vm_mode():
    dc = base_section("demo-control")
    assert "SCENARIO_API_URL: http://scenario-api:8090" in dc
    assert "SCENARIO_API_TOKEN: ${SCENARIO_API_TOKEN:?" in dc


def test_make_secrets_generates_the_scenario_api_token():
    names = re.search(r"^names=\((.*)\)$", GEN_SECRETS, re.M).group(1).split()
    assert "SCENARIO_API_TOKEN" in names and "openssl rand -hex 24" in GEN_SECRETS  # 48 hex chars >= 32
