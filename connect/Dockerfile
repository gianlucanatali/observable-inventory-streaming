# Kafka Connect with the Debezium PostgreSQL connector, the Redis sink (redis-kafka-connect) and the Confluent JDBC sink (restock layer). Build context: overlay/connect/
#   docker build -t dd-connect overlay/connect
# Pins: image and plugin digests are recorded directly below.

# --- stage 1: fetch and verify the plugin (python image already pinned in the BOM; no curl needed) ---
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256 AS fetch
ARG DEBEZIUM_VERSION=3.6.3.Final
ARG DEBEZIUM_SHA512=400495c485a1da532c5d7e2dcc3bbb1b7f7dcdbcdc74c82529309cb89a2c37298319ea45ea00414b3ce750a51ad01084e104725e810c55c1a7c1981baff201a0
ARG REDIS_SINK_VERSION=1.1.0
ARG REDIS_SINK_SHA256=7e4249ca356f702220cf09e9e150c8336e24824d7a72fce05d7999bf2a7e03df
WORKDIR /dl
RUN python3 - <<PY
import hashlib, sys, tarfile, urllib.request
v = "${DEBEZIUM_VERSION}"
url = f"https://repo1.maven.org/maven2/io/debezium/debezium-connector-postgres/{v}/debezium-connector-postgres-{v}-plugin.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha512(data).hexdigest()
if got != "${DEBEZIUM_SHA512}":
    sys.exit(f"Dockerfile: sha512 mismatch for {url}: expected ${DEBEZIUM_SHA512}, got {got}")
open("plugin.tar.gz", "wb").write(data)
tarfile.open("plugin.tar.gz").extractall("/dl/out", filter="data")
print("verified and extracted", url)
PY

# Redis sink for stock.sellable. The zip holds one plugin directory; see the public connector documentation linked in the workshop guide.
RUN python3 - <<PY
import hashlib, io, sys, urllib.request, zipfile
v = "${REDIS_SINK_VERSION}"
url = f"https://github.com/redis-field-engineering/redis-kafka-connect/releases/download/v{v}/redis-redis-kafka-connect-{v}.zip"
data = urllib.request.urlopen(url, timeout=300).read()
got = hashlib.sha256(data).hexdigest()
if got != "${REDIS_SINK_SHA256}":
    sys.exit(f"Dockerfile: sha256 mismatch for {url}: expected ${REDIS_SINK_SHA256}, got {got}")
zipfile.ZipFile(io.BytesIO(data)).extractall("/dl/out")
print("verified and extracted", url)
PY

# Confluent JDBC sink for restock.requests -> procurement-db (contract section 10), Confluent Community License.
# Confluent Hub 10.9.9 (released 2026-09-16, md5 matches api.hub.confluent.io); sha256 computed on the downloaded zip.
# The zip bundles lib/postgresql-42.7.12.jar, so no separate driver. The zip holds one plugin directory.
ARG JDBC_SINK_VERSION=10.9.9
ARG JDBC_SINK_SHA256=6b9b479b2c99f7dafa1053d4a3ffa19d7421d4322ead7071cabee1c55f923308
RUN python3 - <<PY
import hashlib, io, sys, urllib.request, zipfile
v = "${JDBC_SINK_VERSION}"
url = f"https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-jdbc/versions/{v}/confluentinc-kafka-connect-jdbc-{v}.zip"
data = urllib.request.urlopen(url, timeout=300).read()
got = hashlib.sha256(data).hexdigest()
if got != "${JDBC_SINK_SHA256}":
    sys.exit(f"Dockerfile: sha256 mismatch for {url}: expected ${JDBC_SINK_SHA256}, got {got}")
zipfile.ZipFile(io.BytesIO(data)).extractall("/dl/out")
print("verified and extracted", url)
PY

# --- stage 2: the Confluent Connect image (AvroConverter ships inside) ---
FROM confluentinc/cp-kafka-connect:8.3.2@sha256:1bc06f60d7d2958f0030c4f31df7ba66151f5d542455fb84fc785d5f670f81da
# /usr/share/confluent-hub-components/ is already on CONNECT_PLUGIN_PATH (default of the image).
COPY --from=fetch --chown=appuser:appuser /dl/out/ /usr/share/confluent-hub-components/
COPY --chown=appuser:appuser connector-debezium.json connector-sellable-redis.json connector-restock-procurement.json connector-procurement-orders.json register_connector.py /opt/dd/
