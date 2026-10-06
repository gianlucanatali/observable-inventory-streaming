"""Pause / resume one store's Debezium connector through the Kafka Connect REST API.

Equivalent of `make store-pause|store-resume STORE=Sxx` (which runs the same PUT from inside the connect container),
plus a read-back of `GET /connectors/<name>/status` until the connector and its tasks reach the requested state.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger("demo_control")

STORES = ("S01", "S02", "S03", "S04", "S05")
FEED_ACTIONS = {"store-pause": ("pause", "PAUSED"), "store-resume": ("resume", "RUNNING")}


class FeedError(RuntimeError):
    pass


def connector_name(store: str) -> str:
    """Same name as the Makefile: 'inventory-' + STORE.lower()."""
    if store not in STORES:
        raise FeedError(f"store must be one of {', '.join(STORES)}")
    return "inventory-" + store.lower()


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed http base URL from config


class ConnectFeeds:
    def __init__(self, base_url: str, opener=_urlopen, sleep=time.sleep, timeout: float = 5.0,
                 settle_seconds: float = 20.0, poll_seconds: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self._open, self._sleep, self._timeout = opener, sleep, timeout
        self._settle, self._poll = settle_seconds, poll_seconds

    def _request(self, method: str, path: str) -> tuple[int, bytes]:
        url = f"{self.base_url}{path}"
        try:
            with self._open(urllib.request.Request(url, method=method), self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise FeedError(f"Connect REST {method} {url} answered HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FeedError(f"Connect REST {method} {url} unreachable: {exc}") from exc

    def status(self, store: str) -> dict:
        name = connector_name(store)
        code, body = self._request("GET", f"/connectors/{name}/status")
        try:
            data = json.loads(body)
            state = data["connector"]["state"]
            tasks = [t["state"] for t in data.get("tasks", [])]
        except (ValueError, KeyError, TypeError) as exc:
            raise FeedError(f"Connect REST status for {name} is not the expected JSON: {body[:200]!r}") from exc
        return {"store": store, "connector": name, "state": state, "tasks": tasks}

    def statuses(self) -> list[dict]:
        out = []
        for store in STORES:
            try:
                out.append(self.status(store))
            except FeedError as exc:
                out.append({"store": store, "connector": connector_name(store), "state": "ERROR", "tasks": [],
                            "error": str(exc)})
        return out

    def run(self, action: str, store: str, progress: Callable[[str], None]) -> dict:
        if action not in FEED_ACTIONS:
            raise FeedError(f"unknown store feed action {action!r}")
        verb, target = FEED_ACTIONS[action]
        name = connector_name(store)
        progress(f"Requesting {verb} of {name}")
        code, _ = self._request("PUT", f"/connectors/{name}/{verb}")
        if not 200 <= code < 300:
            raise FeedError(f"Connect REST PUT /connectors/{name}/{verb} answered HTTP {code}")
        progress(f"Waiting for {name} to report {target}")
        deadline = time.monotonic() + self._settle
        while True:
            status = self.status(store)
            if status["state"] == target and status["tasks"] and all(t == target for t in status["tasks"]):
                log.info("store feed changed", extra={"fields": {"event": "demo_store_feed", "store": store,
                                                                 "connector": name, "state": target}})
                progress(f"{name}: {target} (tasks {', '.join(status['tasks'])})")
                return status
            if "FAILED" in (status["state"], *status["tasks"]):
                raise FeedError(f"{name} reports connector {status['state']}, tasks {status['tasks']}; "
                                "check the connect container logs on the VM")
            if time.monotonic() >= deadline:
                raise FeedError(f"{name} still reports connector {status['state']}, tasks {status['tasks'] or 'none'} "
                                f"{self._settle:.0f}s after {verb}; expected {target}")
            self._sleep(self._poll)
