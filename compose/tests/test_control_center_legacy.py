import re
import subprocess
from pathlib import Path


COMPOSE_DIR = Path(__file__).parents[1]
COMPOSE = (COMPOSE_DIR / "compose.yaml").read_text()
HYBRID = (COMPOSE_DIR / "compose.hybrid.yaml").read_text()
OVERLAY = COMPOSE_DIR.parent
CLOUD_MAIN = (OVERLAY / "terraform/cloud/main.tf").read_text()
CLOUD_OUTPUTS = (OVERLAY / "terraform/cloud/outputs.tf").read_text()
VM_MAIN = (OVERLAY / "terraform/vm/main.tf").read_text()
MAKEFILE = (OVERLAY / "Makefile").read_text()
STACK = (COMPOSE_DIR / "scripts/stack.sh").read_text()
LINKS = (COMPOSE_DIR / "scripts/links.sh").read_text()


def service_section(text, service):
    marker_text = f"  {service}:"
    marker = text.index(marker_text)
    match = re.search(r"\n  [^ ]", text[marker + len(marker_text) :])
    end = marker + len(marker_text) + match.start() if match else len(text)
    return text[marker:end]


def test_control_center_legacy_is_a_bounded_hybrid_profile_connected_to_cloud_and_connect():
    assert "  control-center:" not in COMPOSE
    service = service_section(HYBRID, "control-center")

    assert "confluentinc/cp-enterprise-control-center:7.9.0" in service
    assert 'profiles: ["control-center"]' in service
    assert 'ports: ["9021:9021"]' in service
    assert "KAFKA_HEAP_OPTS: -Xms1g -Xmx4g" in service
    assert "CONTROL_CENTER_MODE_ENABLE: management" in service
    assert "CONTROL_CENTER_MODE:" not in service
    assert "CONTROL_CENTER_BOOTSTRAP_SERVERS: ${KAFKA_BOOTSTRAP" in service
    assert "CONTROL_CENTER_REPLICATION_FACTOR: \"3\"" in service
    assert "CONTROL_CENTER_STREAMS_SECURITY_PROTOCOL: SASL_SSL" in service
    assert "CONTROL_CENTER_CONNECT_connect_CLUSTER: http://connect:8083" in service
    # cp-kafka-connect 8.x does not serve the default discovery path /v1/metadata/id (404): documented override.
    assert "CONTROL_CENTER_CONNECT_HEALTHCHECK_ENDPOINT: /connectors" in service
    assert "CONTROL_CENTER_SCHEMA_REGISTRY_URL: ${SR_URL:" in service
    assert "}:443" in service
    assert "CONTROL_CENTER_LICENSE: ${CONTROL_CENTER_LICENSE:-}" in service
    assert "CONTROL_CENTER_KAFKA_API_KEY" in service
    assert "CONTROL_CENTER_SR_API_KEY" in service
    assert "healthcheck:" in service
    assert "http://127.0.0.1:9021/2.0/feature/flags" in service

    assert "profiles: !reset []" not in service


def test_control_center_cloud_identity_topics_ingress_and_lifecycle_links_are_wired_without_local_kafka():
    assert 'control-center = "Control Center (Legacy) on the hybrid VM"' in CLOUD_MAIN
    assert '"_confluent-metrics"' in CLOUD_MAIN
    assert '"_confluent-monitoring"' in CLOUD_MAIN
    assert '"_confluent-command"' in CLOUD_MAIN
    assert 'control-center-read-topics' in CLOUD_MAIN
    assert 'control-center-internal-manage' in CLOUD_MAIN
    assert 'env_file_apps = ["connect", "projector", "storefront", "offers", "demo-control"]' in CLOUD_OUTPUTS
    assert 'concat(local.env_file_apps, var.enable_control_center ? ["control-center"] : [])' in CLOUD_OUTPUTS
    assert 'for app in concat(local.env_file_apps, var.enable_control_center ? ["control-center"] : [])' in CLOUD_OUTPUTS
    assert 'sort(keys(local.apps))' not in CLOUD_OUTPUTS

    assert 'resource "aws_vpc_security_group_ingress_rule" "control_center"' in VM_MAIN
    assert 'from_port         = 9021' in VM_MAIN
    assert 'cidr_ipv4         = var.presenter_cidr' in VM_MAIN

    assert 'ALL_LAYERS="releases restock offers dd-streams dd-synthetics dd-rum"' in STACK
    # Control Center is optional: valid when named explicitly, never part of LAYERS=all.
    assert '[ "$TOPOLOGY" = hybrid ] && VALID_LAYERS="$VALID_LAYERS control-center"' in STACK
    assert 'ALL_LAYERS="$ALL_LAYERS control-center"' not in STACK
    assert 'control-center:on) tf_apply cloud; tf_apply vm;;' in STACK
    assert 'for l in releases restock offers; do' in STACK
    assert 'if has control-center; then' in STACK
    assert 'if [ "$TOPOLOGY" = hybrid ] && has control-center && tf_has_state vm; then' in STACK
    assert 'if [ "$TOPOLOGY" = hybrid ] && grep -qx control-center' in LINKS
    assert '$(if $(filter hybrid,$(TOPOLOGY)),--profile control-center --profile cloud-online,)' in MAKEFILE

    assert "cp-kafka:" not in COMPOSE
    assert not re.search(r"^\s*image:.*replicator", COMPOSE, re.MULTILINE | re.IGNORECASE)
    assert not re.search(r"^\s*image:.*cluster-link", COMPOSE, re.MULTILINE | re.IGNORECASE)
    assert "control-center-next-gen" not in COMPOSE


def test_control_center_has_explicit_cluster_describe_acls_without_a_broad_cluster_role():
    block = re.search(r"explicit_control_center_cluster_acls\s*=\s*var\.enable_control_center\s*\?\s*\[(.*?)\]\s*:\s*\[\]", CLOUD_MAIN, re.DOTALL)
    assert block, "Control Center requires an explicit cluster ACL list"
    entries = re.findall(r"\{(.*?)\}", block.group(1), re.DOTALL)
    ops = set()
    for entry in entries:
        assert re.search(r'sa\s*=\s*"control-center"', entry) and re.search(r'type\s*=\s*"CLUSTER"', entry)
        assert re.search(r'name\s*=\s*"kafka-cluster"', entry) and re.search(r'pattern\s*=\s*"LITERAL"', entry)
        ops.add(re.search(r'op\s*=\s*"([A-Z_]+)"', entry).group(1))
    # DESCRIBE/DESCRIBE_CONFIGS per the Control Center on Confluent Cloud guide; IDEMPOTENT_WRITE for its idempotent producers.
    assert ops == {"DESCRIBE", "DESCRIBE_CONFIGS", "IDEMPOTENT_WRITE"}
    assert 'control-center-cluster-admin' not in CLOUD_MAIN


def test_control_center_uses_the_blank_license_trial_route_without_parent_plan_dependency():
    service = service_section(HYBRID, "control-center")
    assert "CONTROL_CENTER_LICENSE: ${CONTROL_CENTER_LICENSE:-}" in service
    assert "CONTROL_CENTER_LICENSE: ${CONTROL_CENTER_LICENSE:?" not in service


def test_hybrid_compose_renders_with_synthetic_placeholder_environment_only(tmp_path):
    env = tmp_path / "synthetic.env"
    values = {
        "STACK": "synthetic",
        "DD_API_KEY": "synthetic-dd-api-key",
        "PG_SUPERUSER_PASSWORD": "synthetic-password",
        "PG_DEBEZIUM_PASSWORD": "synthetic-password",
        "PG_WRITER_PASSWORD": "synthetic-password",
        "PG_DATADOG_PASSWORD": "synthetic-password",
        "PG_PROCUREMENT_PASSWORD": "synthetic-password",
        "CONTROL_PASSWORD": "synthetic-password",
        "KAFKA_BOOTSTRAP": "pkc-synthetic.example:9092",
        "SR_URL": "https://psrc-synthetic.example",
        "ELASTICACHE_REDIS_URL": "redis://cache-synthetic.example:6379/0",
        "HYBRID_ONLINE_URL": "https://online-synthetic.example",
    }
    for app in ("CONNECT", "PROJECTOR", "STOREFRONT", "OFFERS", "DEMO_CONTROL", "CONTROL_CENTER"):
        values[f"{app}_KAFKA_API_KEY"] = f"synthetic-{app.lower()}-kafka-key"
        values[f"{app}_KAFKA_API_SECRET"] = f"synthetic-{app.lower()}-kafka-secret"
        values[f"{app}_SR_API_KEY"] = f"synthetic-{app.lower()}-sr-key"
        values[f"{app}_SR_API_SECRET"] = f"synthetic-{app.lower()}-sr-secret"
    env.write_text("".join(f"{key}={value}\n" for key, value in values.items()))

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_DIR / "compose.yaml"),
            "-f",
            str(COMPOSE_DIR / "compose.cloud.yaml"),
            "-f",
            str(COMPOSE_DIR / "compose.hybrid.yaml"),
            "--env-file",
            str(env),
            "--profile",
            "control-center",
            "config",
            "--no-interpolate",
            "--quiet",
        ],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_control_center_can_write_its_internal_topics():
    # C3 stores its Kafka cluster registry in _confluent-command. Without WRITE the cluster list is empty
    # ("No clusters found") and the UI has no Kafka cluster to pair Schema Registry with (raw Avro bytes).
    binding = re.search(r'control-center-internal-write\s*=\s*\{(.*)\}\s*$', CLOUD_MAIN, re.MULTILINE)
    assert binding, "Control Center needs WRITE on its _confluent* internal topics"
    assert 'role = "DeveloperWrite"' in binding.group(1)
    assert 'crn = "${local.kafka_crn}/topic=_confluent*"' in binding.group(1)
    assert '"DeveloperWrite:topic"  = ["WRITE", "DESCRIBE", "DESCRIBE_CONFIGS"]' in CLOUD_MAIN
