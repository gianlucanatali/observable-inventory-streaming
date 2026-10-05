"""DogStatsD metrics, names and tags exactly as contract section 7. Keys and revisions never become tags."""
from __future__ import annotations

from datadog.dogstatsd import DogStatsd


class Metrics:
    def __init__(self, client: DogStatsd):
        self._c = client

    def apply_delay(self, seconds: float, is_probe: bool) -> None:
        self._c.distribution("stock.freshness.apply_delay", seconds, tags=[f"is_probe:{str(is_probe).lower()}"])

    def record(self, op: str, outcome: str) -> None:
        self._c.increment("stock.projector.records", tags=[f"op:{op}", f"outcome:{outcome}"])

    def error(self, reason: str) -> None:
        self._c.increment("stock.projector.errors", tags=[f"reason:{reason}"])

    def ready(self, ready: bool) -> None:
        self._c.gauge("stock.serving.ready", 1 if ready else 0)


def dogstatsd_from_config(host: str, env: str, service: str, version: str) -> DogStatsd:
    return DogStatsd(host=host, port=8125, constant_tags=[f"env:{env}", f"service:{service}", f"version:{version}"])
