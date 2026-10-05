#!/usr/bin/env python3
"""Register the five Debezium connectors (inventory-s01..s05) and the Redis sink (sellable-redis), then wait for RUNNING.
When env LAYERS (comma list, e.g. "restock,offers") includes restock, also registers restock-procurement (JDBC sink into procurement-db)
and procurement-orders (Debezium on procurement-db.purchase_order -> topic procurement.orders).

Runs inside the connect container (python3 is there; curl, jq and envsubst are not):
  docker compose exec -T connect python3 /opt/dd/register_connector.py
Templates (connector-debezium.json once per store, connector-sellable-redis.json) are rendered from the environment:
${NAME} placeholders must be set and non-empty; per store, STORE_HOST (service name, e.g. store-s01) is added.
Stores come from STORE_HOSTS (S01=store-s01,...). PUT /connectors/<name>/config is idempotent (create or update).
Exits non-zero, naming the connector and printing its status body, on any failure.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DIR = os.environ.get("CONNECTOR_DIR", "/opt/dd")
URL = os.environ.get("REGISTER_CONNECT_URL", "http://localhost:8083")
TIMEOUT_S = int(os.environ.get("REGISTER_TIMEOUT_S", "120"))


def fail(msg):
    sys.exit(f"register_connector: {msg}")


def parse_store_hosts():
    raw = os.environ.get("STORE_HOSTS", "")
    if not raw:
        fail("STORE_HOSTS not set (expected S01=store-s01,S02=store-s02,...)")
    out = []
    for part in raw.split(","):
        sid, sep, host = part.strip().partition("=")
        if not sep or not sid or not host:
            fail(f"STORE_HOSTS entry '{part}' is not STORE=host")
        out.append((sid, host))
    return out


def render(path, extra):
    with open(path) as f:
        text = f.read()
    names = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", text)))
    env = {**os.environ, **extra}
    missing = [n for n in names if not env.get(n)]
    if missing:
        fail(f"template {path} needs environment variable(s) not set or empty: {', '.join(missing)}")
    return json.loads(re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: env[m.group(1)], text))


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=body,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def put(name, config):
    # A timeout (0) or a 500 is usually Connect failing to write its config topic while Confluent Cloud
    # drops broker connections for a minute: retry twice, loudly, then fail.
    for attempt in range(3):
        status, body = call("PUT", f"/connectors/{name}/config", json.dumps(config).encode())
        if status not in (0, 500) or attempt == 2:
            break
        print(f"register_connector: PUT {name} returned {status} ({body[:120]}), retry {attempt + 1}/2 in 20 s")
        time.sleep(20)
    if status not in (200, 201):
        fail(f"{name}: PUT {URL}/connectors/{name}/config returned {status}: {body}")
    print(f"register_connector: PUT {name} -> {status}")


def wait_running(names):
    deadline = time.time() + TIMEOUT_S
    pending = set(names)
    last = {}
    while pending and time.time() < deadline:
        for name in sorted(pending):
            status, body = call("GET", f"/connectors/{name}/status")
            last[name] = f"{status}: {body}"
            if status != 200:
                continue
            st = json.loads(body)
            states = [st["connector"]["state"]] + [t["state"] for t in st.get("tasks", [])]
            if "FAILED" in states:
                fail(f"{name}: connector or task FAILED, status body: {body}")
            if st.get("tasks") and all(s == "RUNNING" for s in states):
                print(f"register_connector: {name} RUNNING ({len(st['tasks'])} task(s))")
                pending.discard(name)
        if pending:
            time.sleep(2)
    if pending:
        detail = "; ".join(f"{n}: {last.get(n, 'no status')}" for n in sorted(pending))
        fail(f"not RUNNING after {TIMEOUT_S}s: {detail}")


def layers():
    return {x.strip() for x in os.environ.get("LAYERS", "").split(",") if x.strip()}


def wait_ready(timeout_s=180):
    # The worker may have just been (re)started by compose up: wait until its REST API answers.
    deadline = time.time() + timeout_s
    while True:
        status, body = call("GET", "/connectors")
        if status == 200:
            return
        if time.time() > deadline:
            fail(f"Connect REST {URL} not ready after {timeout_s} s: last status {status}: {body[:200]}")
        time.sleep(5)


def main():
    wait_ready()
    configs = {}
    for sid, host in parse_store_hosts():
        configs[f"inventory-{sid.lower()}"] = render(f"{DIR}/connector-debezium.json", {"STORE_HOST": host, "STORE_ID": sid})
    configs["sellable-redis"] = render(f"{DIR}/connector-sellable-redis.json", {})
    if "restock" in layers():
        configs["restock-procurement"] = render(f"{DIR}/connector-restock-procurement.json", {})
        configs["procurement-orders"] = render(f"{DIR}/connector-procurement-orders.json", {})
    for name, config in configs.items():
        put(name, config)
    wait_running(list(configs))
    print(f"register_connector: all {len(configs)} connectors RUNNING")


main()
