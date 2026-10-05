"""DogStatsD metrics: restock.orders.open, restock.orders.delivered{store}, restock.lead_time (contract section 10)."""
from __future__ import annotations

from datadog.dogstatsd import DogStatsd


class Metrics:
    def __init__(self, client: DogStatsd):
        self._c = client

    def orders_open(self, n: int) -> None:
        self._c.gauge("restock.orders.open", n)

    def delivered(self, store: str) -> None:
        self._c.increment("restock.orders.delivered", tags=[f"store:{store}"])

    def lead_time(self, seconds: int) -> None:
        self._c.gauge("restock.lead_time", seconds)


def dogstatsd_from_config(host: str, env: str, service: str, version: str) -> DogStatsd:
    return DogStatsd(host=host, port=8125, constant_tags=[f"env:{env}", f"service:{service}", f"version:{version}"])
