"""Checks card: `make load`, `make verify` and `make canary-check` through the scenario API on the VM.

The API (overlay/scenario/scenario/api.py) runs the scenario package next to the load summary (/out/last.json) and the
verify result (/out/verify.json). This client starts one run, polls it, relays its progress and returns a compact result.
The canary gate arguments are chosen from the live routing, exactly as the workshop and talk docs set CHECK_ARGS.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from .actions import OperationFailed

log = logging.getLogger("demo_control")

# Same as the Makefile defaults LOAD_DURATION ?= 120 and LOAD_RPS ?= 5 (tests parse the Makefile).
LOAD_DURATION_S, LOAD_RPS = 120, 5
VERIFY_FILE = "/out/verify.json"
LAST_LOAD_KEY = "demo:checks-last-load"  # {"run": id, "weights": "0 90 10"}: routing while the panel's last load ran
RELEASES = ("1.0.0", "1.1.0", "1.2.0")  # oldest first, the order of the routing weights
LOW_SHARE_MIN_SAMPLES = 30  # a canary below 50% gets ~60 samples in two minutes at 5 rps; the default 100 never passes
# Live routing (1.0.0/1.1.0/1.2.0) -> gate parameters, the same rule as `scenario canary-check --routing` (tests compare
# both): the newer release with traffic is the canary (release_b), the older one the baseline (release_a). 90/10/0 gates
# 1.1.0 against 1.0.0 (the canary-first incident), 90/0/10 and 50/0/50 (fix after rollback) 1.2.0 against 1.0.0, 0/90/10
# and 0/50/50 (explicit make weights) 1.2.0 against 1.1.0, a release alone other
# than 1.0.0 is gated without a baseline (0/0/100). Below 50% the sample minimum is 30. See workshop Labs 3-4.
CHECK_KINDS = ("load", "verify", "canary-check")


class CheckError(RuntimeError):
    pass


def canary_gate_spec(weights) -> dict:
    """{label, release_a, release_b, min_samples} for a live routing, or CheckError when it is not a canary split."""
    weights = tuple(weights)
    live = [(r, w) for r, w in zip(RELEASES, weights) if w > 0]
    if len(live) == 2:
        (base, _), (canary, share) = live
    elif len(live) == 1 and live[0][0] != RELEASES[0]:
        base, (canary, share) = None, live[0]
    else:
        raise CheckError(f"live routing is {'/'.join(map(str, weights))} (1.0.0/1.1.0/1.2.0); Check canary needs a "
                         "canary split: a newer release next to an older one (e.g. 90/10/0, 90/0/10, 50/0/50) "
                         "or a newer release alone (0/0/100)")
    return {"label": f"{share}%", "release_a": base, "release_b": canary,
            "min_samples": LOW_SHARE_MIN_SAMPLES if share < 50 else None}


def check_args(gate: dict) -> str:
    """The equivalent `make canary-check CHECK_ARGS=...` value (shown in the panel, compared with the docs in tests)."""
    parts = []
    if gate["release_a"]:
        parts += ["--release-a", gate["release_a"]]
    parts += ["--release-b", gate["release_b"]]
    if gate["min_samples"] is not None:
        parts += ["--min-samples", str(gate["min_samples"])]
    return " ".join(parts + ["--verify-file", VERIFY_FILE])


def gate_for(weights: tuple[int, int, int]) -> dict:
    gate = canary_gate_spec(weights)
    params = {"release_b": gate["release_b"]}
    if gate["release_a"]:
        params["release_a"] = gate["release_a"]
    if gate["min_samples"] is not None:
        params["min_samples"] = gate["min_samples"]
    return {"label": gate["label"], "weights": "/".join(map(str, weights)), "params": params,
            "release_a": gate["release_a"], "release_b": gate["release_b"], "check_args": check_args(gate)}


def parse_routing_value(raw: str | None) -> tuple[int, int, int]:
    """Redis `demo:routing` ("1.0.0=0 1.1.0=90 1.2.0=10", published by both routing paths) -> weights."""
    if not raw:
        raise CheckError("live routing is unknown (Redis demo:routing is empty): set a routing step first")
    try:
        found = dict(part.split("=", 1) for part in raw.split())
        return tuple(int(found[r]) for r in ("1.0.0", "1.1.0", "1.2.0"))  # type: ignore[return-value]
    except (KeyError, ValueError) as exc:
        raise CheckError(f"Redis demo:routing = {raw!r} is not '1.0.0=<w> 1.1.0=<w> 1.2.0=<w>'") from exc


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed http base URL from config


class ScenarioApi:
    """Tiny client; the token goes only into the Authorization header, never into messages or logs."""

    def __init__(self, base_url: str, token: str, opener=_urlopen, sleep=time.sleep, clock=time.monotonic,
                 timeout: float = 5.0, poll_s: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self._token, self._open, self._sleep, self._clock = token, opener, sleep, clock
        self._timeout, self._poll = timeout, poll_s

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")[:300]
            try:
                detail = json.loads(raw).get("error", raw)
            except (ValueError, AttributeError):
                detail = raw
            hint = " (token mismatch: SCENARIO_API_TOKEN differs between demo-control and the VM)" if exc.code == 401 else ""
            raise CheckError(f"scenario API {method} {url} answered HTTP {exc.code}: {detail}{hint}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CheckError(f"scenario API {method} {url} unreachable: {exc}") from exc
        except ValueError as exc:
            raise CheckError(f"scenario API {method} {url} did not answer JSON") from exc

    def runs(self) -> dict:
        return self._request("GET", "/runs")

    def run(self, kind: str, params: dict, progress: Callable[[str], None], limit_s: float) -> dict:
        run = self._request("POST", "/runs", {"kind": kind, "params": params})
        run_id, last = run["id"], None
        deadline = self._clock() + limit_s
        while run["status"] == "running":
            if run["progress"] != last:
                last = run["progress"]
                progress(last)
            if self._clock() >= deadline:
                raise CheckError(f"{kind} run {run_id} still running on the VM after {limit_s:.0f} s; "
                                 "the panel stopped waiting (the run continues; its result appears under Last VM run)")
            self._sleep(self._poll)
            run = self._request("GET", f"/runs/{run_id}")
        return run


def _ms(v) -> str:
    return "n/a" if v is None else f"{v:.0f} ms"


def compact(run: dict) -> dict:
    """The part of a run the panel shows: per-release numbers, verify counts or gate lines, plus the raw lines."""
    out = {"run": run["id"], "kind": run["kind"], "status": run["status"], "params": run["params"],
           "error": run.get("error"), "finished_at": run.get("finished_at")}
    res = run.get("result") or {}
    if run["kind"] == "load" and res:
        out["releases"] = {r: {k: d.get(k) for k in ("count", "errors", "p50_ms", "p95_ms")}
                           for r, d in res["total"]["releases"].items()}
        out["count"] = res["total"]["count"]
    elif run["kind"] == "verify" and res:
        out.update(ok=res["ok"], mismatches=res["mismatches"], sellable_mismatches=res["sellable_mismatches"])
    elif run["kind"] == "canary-check" and res:
        out.update(ok=res["ok"], gates=res["gates"], releases={
            r: {k: d.get(k) for k in ("count", "errors", "p95_ms")} for r, d in res["releases"].items()},
            summary_written_at=res.get("summary_written_at"))
    if "lines" in run:
        out["lines"] = run["lines"][-20:]
    return out


def describe(c: dict) -> str:
    if c["kind"] == "load" and "releases" in c:
        rels = "; ".join(f"{r} {d['count']} req, p95 {_ms(d['p95_ms'])}, {d['errors']} errors"
                         for r, d in c["releases"].items())
        return f"Load done: {c['count']} requests ({rels or 'no responses'}); summary in /out/last.json"
    if c["kind"] == "verify" and "ok" in c:
        return (f"Verify {'passed' if c['ok'] else 'FAILED'}: {c['mismatches']} position mismatch(es), "
                f"{c['sellable_mismatches']} sellable mismatch(es)")
    if c["kind"] == "canary-check" and "gates" in c:
        # Same wording as the scenario verdict line: each failed gate's reason (older VMs: its name), then the action.
        failed = [g.get("reason") or g["name"] for g in c["gates"] if not g["passed"]]
        return "CANARY GATES PASSED" if c["ok"] else f"CANARY GATES FAILED: {'; '.join(failed)}; roll back"
    return f"{c['kind']} {c['status']}"


class Checks:
    def __init__(self, api: ScenarioApi, redis_client, live_weights: Callable[[], tuple[int, int, int]]):
        self.api, self._r, self._weights = api, redis_client, live_weights

    def canary_gate(self) -> dict:
        return gate_for(self._weights())

    def _last_load(self) -> dict | None:
        raw = self._r.get(LAST_LOAD_KEY)
        return None if raw is None else json.loads(raw)

    def view(self) -> dict:
        out: dict = {}
        try:
            out["canary"] = self.canary_gate()
        except Exception as exc:  # noqa: BLE001 - shown on the card; the Check canary action re-checks and fails loudly
            out["canary"] = {"error": str(exc)}
        listing = self.api.runs()  # CheckError propagates: the endpoint answers 502 with the reason
        out["active"] = listing["active"]
        out["last"] = {}  # newest finished run per kind (the API lists newest first)
        for r in listing["runs"]:
            if r["status"] != "running" and r["kind"] not in out["last"]:
                out["last"][r["kind"]] = compact(r)
        out["last_load"] = self._last_load()
        return out

    def _finish(self, kind: str, run: dict, extra: dict | None = None) -> dict:
        c = compact(run)
        if extra:
            c.update(extra)
        if run["status"] == "error":
            raise CheckError(f"{kind} error on the VM: {run['error']}")
        if run["status"] == "failed":
            raise OperationFailed(describe(c), c)
        return c

    def load(self, progress: Callable[[str], None]) -> dict:
        weights = "/".join(map(str, self._weights()))
        run = self.api.run("load", {"duration_s": LOAD_DURATION_S, "rps": LOAD_RPS}, progress,
                           limit_s=LOAD_DURATION_S + 90)
        if run["status"] == "passed":
            self._r.set(LAST_LOAD_KEY, json.dumps({"run": run["id"], "weights": weights}))
        c = self._finish("load", run, {"weights": weights})
        progress(describe(c) + f" (routing {weights})")
        return c

    def verify(self, progress: Callable[[str], None]) -> dict:
        c = self._finish("verify", self.api.run("verify", {}, progress, limit_s=300))
        progress(describe(c))
        return c

    def canary_check(self, gate: dict, progress: Callable[[str], None]) -> dict:
        last = self._last_load()
        if last and last["weights"] != gate["weights"]:
            raise CheckError(f"routing changed since the panel's last load (load ran at {last['weights']}, live is "
                             f"{gate['weights']}): run load again before gating")
        base = f" vs baseline {gate['release_a']}" if gate["release_a"] else ""
        progress(f"Gating canary {gate['release_b']}{base} at {gate['label']} ({gate['weights']}): "
                 f"CHECK_ARGS=\"{gate['check_args']}\"")
        run = self.api.run("canary-check", gate["params"], progress, limit_s=60)
        c = self._finish("canary-check", run, {"weights": gate["weights"], "check_args": gate["check_args"],
                                               "label": gate["label"], "release_a": gate["release_a"],
                                               "release_b": gate["release_b"]})
        progress(describe(c) + f" at {gate['label']} ({gate['check_args']})")
        return c
