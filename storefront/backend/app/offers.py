"""In-memory offers map and the background consumer that fills it."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_OFFERS = 10_000


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def offer_to_json(offer: dict) -> dict:
    return {
        "offer_id": offer["offer_id"],
        "cart_id": offer["cart_id"],
        "scenario_id": offer["scenario_id"],
        "offer_type": offer["offer_type"],
        "original_store_id": offer["original_store_id"],
        "original_product_id": offer["original_product_id"],
        "store_id": offer.get("store_id"),
        "product_id": offer.get("product_id"),
        "discount_pct": offer["discount_pct"],
        "headline": offer["headline"],
        "body": offer["body"],
        "decision_route": offer["decision_route"],
        "decision_reason": offer.get("decision_reason"),
        "jev_choice": offer.get("jev_choice"),
        "jev_confidence": offer.get("jev_confidence"),
        "min_confidence": offer.get("min_confidence"),
        "rule_choice": offer.get("rule_choice"),
        "chosen_choice": offer.get("chosen_choice"),
        "text_route": offer["text_route"],
        "created_at": _iso(offer.get("created_at")),
    }


class OfferStore:
    """Latest offer per (scenario_id, cart_id); the most recently consumed record wins."""

    def __init__(self, max_items: int = MAX_OFFERS):
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._max = max_items

    def put(self, offer: dict) -> None:
        key = (offer["scenario_id"], offer["cart_id"])
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = offer
            while len(self._items) > self._max:
                evicted, _ = self._items.popitem(last=False)
                log.warning("offer store full, evicted oldest cart", extra={"cart_id": evicted[1]})

    def get(self, scenario_id: str, cart_id: str) -> dict | None:
        with self._lock:
            return self._items.get((scenario_id, cart_id))


class OfferConsumer:
    """Polls `consumer` (confluent-kafka-like: poll(timeout) -> msg with error()/value()).

    Any failure stops the thread, is logged with the traceback and is reported by status()
    so /readyz turns red. Offsets are never committed: a restart replays the compacted topic.
    """

    def __init__(self, consumer: Any, store: OfferStore, on_record: Callable[[], None] = lambda: None,
                 partition_eof: int | None = None):
        self._consumer = consumer
        self._store = store
        self._on_record = on_record
        self._partition_eof = partition_eof
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="offers-consumer", daemon=True)
        self._failure: str | None = None
        self._received = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._consumer.close()

    def status(self) -> dict:
        if self._failure:
            return {"state": "failed", "error": self._failure, "received": self._received}
        if not self._thread.is_alive():
            return {"state": "stopped", "error": "consumer thread is not running", "received": self._received}
        return {"state": "running", "error": None, "received": self._received}

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self._consumer.poll(1.0)
                if msg is None:
                    continue
                err = msg.error()
                if err is not None:
                    if self._partition_eof is not None and err.code() == self._partition_eof:
                        continue
                    raise RuntimeError(f"offers consumer received a Kafka error: {err}")
                offer = msg.value()
                if offer is None:
                    raise RuntimeError("offers consumer received a tombstone/null value; contract violation")
                self._store.put(offer)
                self._received += 1
                self._on_record()
        except Exception as exc:  # noqa: BLE001 - recorded, logged, surfaced by /readyz; thread ends
            self._failure = f"{type(exc).__name__}: {exc}"
            log.exception("offers consumer failed; offers will not update until restart")
