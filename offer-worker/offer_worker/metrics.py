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

    def stock_unconfirmed(self, reason: str) -> None:
        """An alternative's confirmed minimum was not > 0 for a reason other than live stock at 0."""
        self._c.increment("offer.stock.unconfirmed", tags=[f"reason:{reason}"])

    def completed(self, offer_type: str, restock_included: bool) -> None:
        """offer_type: ALTERNATIVE_PRODUCT (an alternative, maybe with the Restock notice), NOTIFY_ME (the Restock
        notice alone) or NONE (no Offer); restock_included: whether the Offer carries the Restock notice."""
        self._c.increment("offer.completed", tags=[f"offer_type:{offer_type}",
                                                   f"restock_included:{str(restock_included).lower()}"])


def dogstatsd_from_config(host: str, env: str, service: str, version: str) -> DogStatsd:
    return DogStatsd(host=host, port=8125, constant_tags=[f"env:{env}", f"service:{service}", f"version:{version}"])
