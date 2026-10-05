"""Real I/O: PostgreSQL probe writer, Datadog metrics, Connect REST."""
from __future__ import annotations

import logging

import psycopg
import requests
from datadog import DogStatsd

from .config import Config

log = logging.getLogger("stock_watchdog")

PROBE_SQL = """
INSERT INTO stock_position (store_id, product_id, quantity, revision, changed_at, deleted)
VALUES (%s, '__probe__', %s, 1, now(), false)
ON CONFLICT (store_id, product_id)
DO UPDATE SET quantity = EXCLUDED.quantity, deleted = false
RETURNING revision, changed_at
"""  # revision/changed_at in VALUES are placeholders: the stamp trigger overwrites them


class PgProbeWriter:
    def __init__(self, cfg: Config, store_id: str, host: str):
        self._cfg, self._store_id, self._host = cfg, store_id, host
        self._conn: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        c = self._cfg
        return psycopg.connect(host=self._host, port=c.pg_port, dbname=c.pg_database,
                               user=c.pg_user, password=c.pg_password,
                               connect_timeout=3, autocommit=True)

    def write(self, quantity: int) -> tuple[int, int]:
        try:
            if self._conn is None or self._conn.closed:
                self._conn = self._connect()
            with self._conn.cursor() as cur:
                cur.execute(PROBE_SQL, (self._store_id, quantity))
                row = cur.fetchone()
        except Exception:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            raise
        if row is None:
            raise RuntimeError("probe upsert returned no row")
        revision, changed_at = row
        return int(revision), int(changed_at.timestamp() * 1000)


class DogMetrics:
    def __init__(self, host: str, port: int):
        self._statsd = DogStatsd(host=host, port=port)

    def gauge(self, name: str, value: float, tags: list[str] | None = None) -> None:
        self._statsd.gauge(name, value, tags=tags)

    def increment(self, name: str, tags: list[str] | None = None) -> None:
        self._statsd.increment(name, tags=tags)


def connect_http_get(url: str) -> dict:
    resp = requests.get(url, timeout=3)
    resp.raise_for_status()
    return resp.json()
