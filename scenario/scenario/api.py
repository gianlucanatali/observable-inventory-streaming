"""Token-protected REST API over the scenario load / verify / canary-check runs.

Started with `scenario api` in the `scenario-api` compose service (same image as the `scenario` tool). It lets the
control panel run what `make load`, `make verify` and `make canary-check` run, without a terminal:

    POST /runs        {"kind": "load"|"verify"|"canary-check", "params": {...}}  -> 202 run, 409 while one runs
    GET  /runs        recent runs, newest first, and the active run id
    GET  /runs/<id>   status, progress, output lines (the same lines the make target prints) and result
    GET  /healthz     no authentication, no data

Every other path needs `Authorization: Bearer <SCENARIO_API_TOKEN>`. The API runs the scenario package in process
(no shell, no docker socket) and writes the same files as the make targets: /out/last.json and /out/verify.json in the
shared `scenario-out` volume. Parameters are bounded; seed, window and file paths are fixed.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import ConfigError, require
from .runs import canary_check_run, load_run, verify_run

log = logging.getLogger("scenario.api")

RELEASES = ("1.0.0", "1.1.0", "1.2.0")
KINDS = ("load", "verify", "canary-check")
MIN_TOKEN_LEN = 32
MAX_BODY = 4096
MAX_LINES = 400
KEEP_RUNS = 20
LOAD_SEED, LOAD_WINDOW_S = 42, 10  # the CLI defaults the make target uses (ints, so window lines read the same)
RUN_ID = re.compile(r"[0-9a-f]{16}\Z")
# Bounds: generous around the make defaults (LOAD_DURATION=120, LOAD_RPS=5), small enough to keep a demo VM safe.
BOUNDS = {
    "load": {"duration_s": (120.0, 10.0, 600.0, float), "rps": (5.0, 0.5, 50.0, float)},
    "verify": {"settle_s": (30.0, 0.0, 120.0, float)},
    "canary-check": {"min_samples": (100, 1, 10000, int), "max_error_rate": (0.0, 0.0, 1.0, float),
                     # Default 200, not 200.0: the CLI's argparse default, so the gate line reads "budget 200 ms".
                     "p95_budget_ms": (200, 1.0, 10000.0, float)},
}
ENV_FOR = {"load": ["BASE_URL"], "verify": ["STORE_HOSTS", "PG_DATABASE", "PG_WRITER_USER", "PG_WRITER_PASSWORD",
                                            "REDIS_URL"]}


class ValidationError(ValueError):
    pass


class Busy(RuntimeError):
    def __init__(self, active: "Run"):
        super().__init__(f"run {active.id} ({active.kind}) is still running; one run at a time")
        self.active = active


def _number(params: dict, key: str, spec) -> float | int:
    default, lo, hi, kind = spec
    if key not in params:
        return default
    v = params[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (kind is int and not isinstance(v, int)):
        raise ValidationError(f"{key} must be {'an integer' if kind is int else 'a number'}, got {v!r}")
    if isinstance(v, float) and not math.isfinite(v) or not lo <= v <= hi:
        raise ValidationError(f"{key} must be between {lo} and {hi}, got {v!r}")
    return kind(v)


def validate(body: Any) -> tuple[str, dict]:
    """Request body -> (kind, normalised params). Unknown keys, kinds or releases are errors, never ignored."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object {\"kind\": ..., \"params\": {...}}")
    extra = set(body) - {"kind", "params"}
    if extra:
        raise ValidationError(f"unknown field(s): {', '.join(sorted(extra))}")
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ValidationError("params must be a JSON object")
    allowed = set(BOUNDS[kind]) | ({"release_a", "release_b"} if kind == "canary-check" else set())
    extra = set(params) - allowed
    if extra:
        raise ValidationError(f"unknown {kind} parameter(s): {', '.join(sorted(extra))}"
                              f"; allowed: {', '.join(sorted(allowed))}")
    out: dict = {k: _number(params, k, spec) for k, spec in BOUNDS[kind].items()}
    if kind == "canary-check":
        rb, ra = params.get("release_b"), params.get("release_a")
        if rb not in RELEASES:
            raise ValidationError(f"release_b is required and must be one of {', '.join(RELEASES)}")
        if ra is not None and (ra not in RELEASES or ra == rb):
            raise ValidationError(f"release_a must be null or one of {', '.join(RELEASES)} other than release_b")
        out.update(release_a=ra, release_b=rb)
    return kind, out


def _ms(v) -> str:
    return "n/a" if v is None else f"{v:.0f} ms"


def load_progress(line: str, duration_s: float) -> str | None:
    """Readable progress from one `scenario load` window line; None for other lines."""
    try:
        w = json.loads(line)
    except ValueError:
        return None
    if not isinstance(w, dict) or "window" not in w:
        return None
    done = min(duration_s, (w["window"] + 1) * w["window_s"])
    rels = "; ".join(f"{r} {d['count']} req p95 {_ms(d['p95_ms'])}" for r, d in w["releases"].items())
    errors = sum(d["errors"] for d in w["releases"].values())
    return f"Load {done:.0f}/{duration_s:.0f} s, last window: {rels or 'no responses'}; errors {errors}"


@dataclass
class Run:
    id: str
    kind: str
    params: dict
    status: str = "running"  # running | passed (exit 0) | failed (exit 1: a check failed) | error (exit 2)
    progress: str = "Starting"
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    dropped_lines: int = 0
    result: Any = None
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None

    def view(self, lines: bool = True) -> dict:
        d = {k: getattr(self, k) for k in ("id", "kind", "params", "status", "progress", "exit_code", "dropped_lines",
                                            "result", "error", "started_at", "finished_at")}
        if lines:
            d["lines"] = list(self.lines)
        return d


Executor = Callable[[dict, Callable[[str], None]], tuple[int, Any]]


def default_executors(out_dir: str) -> dict[str, Executor]:
    summary, verify_file = os.path.join(out_dir, "last.json"), os.path.join(out_dir, "verify.json")

    def load(p, emit):
        return load_run(p["rps"], p["duration_s"], LOAD_SEED, LOAD_WINDOW_S, summary, emit)

    def verify(p, emit):
        return verify_run(p["settle_s"], verify_file, emit)

    def canary(p, emit):
        code, data = canary_check_run(summary, p["release_a"], p["release_b"], p["min_samples"], p["max_error_rate"],
                                      p["p95_budget_ms"], verify_file, emit)
        data["summary_written_at"] = os.path.getmtime(summary)
        data["verify_written_at"] = os.path.getmtime(verify_file)
        return code, data

    return {"load": load, "verify": verify, "canary-check": canary}


class Runner:
    """One run at a time in a daemon thread; keeps the last KEEP_RUNS runs in memory."""

    def __init__(self, executors: dict[str, Executor], clock: Callable[[], float] = time.time):
        self._executors, self._clock = executors, clock
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._active: Run | None = None

    def start(self, kind: str, params: dict) -> dict:
        with self._lock:
            if self._active is not None:
                raise Busy(self._active)
            run = Run(secrets.token_hex(8), kind, params, started_at=self._clock())
            self._active = run
            self._runs[run.id] = run
            while len(self._runs) > KEEP_RUNS:
                self._runs.popitem(last=False)
            view = run.view()
        threading.Thread(target=self._execute, args=(run,), daemon=True, name=f"scenario-run-{run.id}").start()
        return view

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.view() if run else None

    def list(self) -> dict:
        with self._lock:
            return {"active": self._active.id if self._active else None,
                    "runs": [r.view(lines=False) for r in reversed(self._runs.values())]}

    def _emit(self, run: Run, line: str) -> None:
        with self._lock:
            if len(run.lines) < MAX_LINES:
                run.lines.append(line)
            else:
                run.dropped_lines += 1
            if run.kind == "load":
                run.progress = load_progress(line, run.params["duration_s"]) or run.progress

    @staticmethod
    def _start_message(run: Run) -> str:
        p = run.params
        if run.kind == "load":
            return f"Load starting: {p['duration_s']:.0f} s at {p['rps']:g} rps"
        if run.kind == "verify":
            return f"Comparing every source with Redis (sellable settles up to {p['settle_s']:.0f} s)"
        return "Gating the last load summary and verify result"

    def _execute(self, run: Run) -> None:
        log.info("run started", extra={"fields": {"event": "scenario_run", "run": run.id, "kind": run.kind,
                                                  "params": run.params, "status": "running"}})
        try:
            with self._lock:
                run.progress = self._start_message(run)
            code, result = self._executors[run.kind](run.params, lambda line: self._emit(run, line))
        except FileNotFoundError as exc:
            self._finish(run, "error", 2, None, f"{exc.filename} does not exist: run "
                         f"{'load' if exc.filename and str(exc.filename).endswith('last.json') else 'verify'} first")
        except Exception as exc:  # noqa: BLE001 - recorded on the run, logged with traceback, and returned to the caller
            log.exception("run failed", extra={"fields": {"event": "scenario_run", "run": run.id, "kind": run.kind}})
            self._finish(run, "error", 2, None, f"{type(exc).__name__}: {exc}")
        else:
            self._finish(run, "passed" if code == 0 else "failed", code, result, None)

    def _finish(self, run: Run, status: str, code: int, result, error: str | None) -> None:
        with self._lock:
            run.status, run.exit_code, run.result, run.error = status, code, result, error
            run.finished_at = self._clock()
            run.progress = {"passed": f"{run.kind} passed", "failed": f"{run.kind} failed (exit 1)",
                            "error": f"{run.kind} error: {error}"}[status]
            self._active = None
        log.info("run finished", extra={"fields": {"event": "scenario_run", "run": run.id, "kind": run.kind,
                                                   "status": status, "exit_code": code, "error": error}})


def make_handler(runner: Runner, token: str):
    expected = token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "scenario-api", ""
        timeout = 10  # socket read timeout: a slow client cannot hold a thread forever

        def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            scheme, _, supplied = (self.headers.get("Authorization") or "").partition(" ")
            if scheme == "Bearer" and hmac.compare_digest(supplied.strip().encode(), expected):
                return True
            self._send(401, {"error": "authentication required"}, {"WWW-Authenticate": 'Bearer realm="scenario-api"'})
            return False

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            if not self._authorized():
                return None
            if path == "/runs":
                return self._send(200, runner.list())
            run_id = path.removeprefix("/runs/")
            if path.startswith("/runs/") and RUN_ID.fullmatch(run_id):
                run = runner.get(run_id)
                return self._send(200, run) if run else self._send(404, {"error": f"no run {run_id} (only the last "
                                                                                  f"{KEEP_RUNS} are kept)"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return None
            if self.path != "/runs":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return self._send(400, {"error": "invalid Content-Length"})
            if not 0 < length <= MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._send(415, {"error": "Content-Type must be application/json"})
            try:
                kind, params = validate(json.loads(self.rfile.read(length)))
            except ValueError as exc:  # ValidationError and JSONDecodeError
                return self._send(400, {"error": str(exc)})
            try:
                return self._send(202, runner.start(kind, params))
            except Busy as exc:
                return self._send(409, {"error": str(exc), "active": exc.active.id})

        def log_message(self, fmt, *args):  # request line only; headers (the token) are never logged
            log.info("http", extra={"fields": {"event": "scenario_api_http", "client": self.client_address[0],
                                               "request": self.requestline.split(" HTTP/")[0], "detail": fmt % args}})

    return Handler


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage(), **getattr(record, "fields", {})}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def check_token(token: str | None) -> str:
    if not token or len(token) < MIN_TOKEN_LEN:
        raise ConfigError(f"SCENARIO_API_TOKEN must be set and at least {MIN_TOKEN_LEN} characters (run make secrets)")
    return token


def make_server(host: str, port: int, runner: Runner, token: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(runner, check_token(token)))
    server.daemon_threads = True
    return server


def serve(port: int) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    token = check_token(os.environ.get("SCENARIO_API_TOKEN"))
    require(sorted({n for names in ENV_FOR.values() for n in names}))  # fail at startup, not at the first click
    out_dir = os.environ.get("SCENARIO_OUT_DIR", "/out")
    if not os.path.isdir(out_dir) or not os.access(out_dir, os.W_OK):
        raise ConfigError(f"{out_dir} must be a writable directory (the scenario-out volume)")
    server = make_server("0.0.0.0", port, Runner(default_executors(out_dir)), token)  # noqa: S104 - SG-restricted
    log.info("scenario api listening", extra={"fields": {"port": port, "out_dir": out_dir}})
    server.serve_forever()
