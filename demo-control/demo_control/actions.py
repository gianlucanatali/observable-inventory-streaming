"""In-process, single-flight presenter actions; never invokes a shell.

Source actions (sell-out, reset) touch PostgreSQL only. Extra operations (ALB routing, store feed pause/resume, the VM
checks and background sales) are injected by wsgi.py and use the narrow clients in routing.py, store_feed.py,
checks.py and sales.py.
"""
from __future__ import annotations

import re
import threading
import time
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable

PRODUCT_ID = re.compile(r"P\d{4}\Z")
log = logging.getLogger("demo_control")


class ActionError(ValueError):
    pass


class OperationFailed(RuntimeError):
    """An operation that ran to the end but failed its check (verify mismatch, canary gate): keeps its result."""

    def __init__(self, message: str, result: Any = None):
        super().__init__(message)
        self.result = result


@dataclass
class Action:
    id: int
    name: str
    product_id: str | None
    status: str = "queued"
    progress: str = "Queued"
    error: str | None = None
    params: dict | None = None
    result: Any = None

    def view(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Operation:
    """An extra action: `parse` validates the request body (raises ActionError), `run` does the work."""
    parse: Callable[[dict], dict]
    run: Callable[[dict, Callable[[str], None]], Any]


class Actions:
    """Run source-only scenario operations in a daemon thread, one at a time."""

    def __init__(self, sell_out: Callable[[str, Callable[[str], None]], None], reset: Callable[[Callable[[str], None]], None],
                 event_sink=None, stack: str | None = None, operations: dict[str, Operation] | None = None):
        self._sell_out, self._reset = sell_out, reset
        self._operations = dict(operations or {})
        self._event_sink, self._stack = event_sink, stack
        self._lock = threading.Lock()
        self._active: Action | None = None
        self._started_at = 0.0
        self._next_id = 1

    def status(self) -> dict:
        with self._lock:
            return self._active.view() if self._active else {"status": "idle", "progress": "No action is running"}

    def start(self, name: str, product_id: str | None = None, body: dict | None = None) -> dict:
        params = None
        if name in self._operations:
            params = self._operations[name].parse(body or {})
            product_id = None
        elif name not in {"sell-out", "reset"}:
            raise ActionError("unknown action")
        if name == "sell-out":
            product_id = product_id or "P0042"
            if not isinstance(product_id, str) or not PRODUCT_ID.fullmatch(product_id):
                raise ActionError("product_id must match Pdddd, for example P0042")
        with self._lock:
            if self._active and self._active.status in {"queued", "running"}:
                return self._active.view()
            if (self._active and self._active.name == name and self._active.product_id == product_id
                    and self._active.params == params and time.monotonic() - self._started_at < 1):
                return self._active.view()
            action = Action(self._next_id, name, product_id, params=params)
            self._next_id += 1
            self._active = action
            self._started_at = time.monotonic()
        threading.Thread(target=self._run, args=(action,), daemon=True, name=f"demo-action-{action.id}").start()
        return action.view()

    def _run(self, action: Action) -> None:
        def progress(message: str) -> None:
            with self._lock:
                action.progress = message
        with self._lock:
            action.status, action.progress = "running", "Starting " + (
                "source workflow" if action.name in {"sell-out", "reset"} else action.name)
        self._event(action, "started")
        try:
            if action.name in self._operations:
                result = self._operations[action.name].run(action.params or {}, progress)
                with self._lock:
                    action.result = result
            elif action.name == "sell-out":
                self._sell_out(action.product_id or "P0042", progress)
            else:
                self._reset(progress)
        except Exception as exc:  # action state is intentionally truthful after a partial source write
            with self._lock:
                action.status, action.error = "failed", f"Action failed: {exc}"
                action.result = getattr(exc, "result", None)
                action.progress = ("Check the source and Redis convergence, then retry the action."
                                   if action.name in {"sell-out", "reset"} else action.progress)
            log.exception("presenter action failed", extra={"fields": {"event": "demo_action", "action": action.name,
                                                               "product_id": action.product_id, "params": action.params,
                                                               "status": "failed"}})
            self._event(action, "failed", error=str(exc))
        else:
            with self._lock:
                action.status = "succeeded"
                if action.name in {"sell-out", "reset"}:
                    action.progress = "Source and Redis verification completed"
            log.info("presenter action completed", extra={"fields": {"event": "demo_action", "action": action.name,
                                                                     "product_id": action.product_id, "status": "succeeded"}})
            self._event(action, "succeeded")

    def _event(self, action: Action, status: str, error: str | None = None) -> None:
        if self._event_sink is None:
            return
        tags = ["project:dd-demo", "demo_event:action", f"action:{action.name}", f"product:{action.product_id or 'none'}"]
        tags += [f"{k}:{v}" for k, v in sorted((action.params or {}).items()) if v is not None]
        if self._stack:
            tags.append(f"stack:{self._stack}")
        message = f"Action {action.name} {status}."
        if action.params:
            message += " " + ", ".join(f"{k}={v}" for k, v in sorted(action.params.items()) if v is not None) + "."
        if status == "succeeded" and action.progress:
            message += f" {action.progress}"
        if error:
            message += f" Error: {error}"
        try:
            self._event_sink.event(f"demo action: {action.name} {status}", message,
                                   alert_type="error" if status == "failed" else "info" if status == "started" else "success",
                                   tags=tags)
        except Exception:  # noqa: BLE001 - telemetry cannot change the source action outcome
            log.exception("datadog action event failed", extra={"fields": {"action": action.name, "status": status}})
