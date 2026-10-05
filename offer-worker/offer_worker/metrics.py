"""DogStatsD metrics, names and tags exactly as contract section 7. Ids never become tags."""
from __future__ import annotations

from datadog.dogstatsd import DogStatsd


class Metrics:
    def __init__(self, client: DogStatsd):
        self._c = client

    def decision(self, route: str, reason: str) -> None:
        self._c.increment("offer.decision", tags=[f"route:{route}", f"reason:{reason}"])

    def text(self, route: str, reason: str) -> None:
        self._c.increment("offer.text", tags=[f"route:{route}", f"reason:{reason}"])

    def completed(self, offer_type: str) -> None:
        self._c.increment("offer.completed", tags=[f"offer_type:{offer_type}"])


def dogstatsd_from_config(host: str, env: str, service: str, version: str) -> DogStatsd:
    return DogStatsd(host=host, port=8125, constant_tags=[f"env:{env}", f"service:{service}", f"version:{version}"])
