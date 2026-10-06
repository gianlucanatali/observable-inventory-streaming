"""Checks and Background sales cards: operations, read-only endpoints and the self-contained UI block.

Checks: load / verify / canary-check through the scenario API on the VM (checks.py).
Background sales: rate-based off / on (sales.py) and Full reset = sales off + baseline routing + reset demo data, plus
the restock part of `make reset` (cancel open purchase orders, clear restock:eta keys) when that layer runs.
The Actions card's "Reset demo data" is guarded here too: it refuses while background sales are on, because the jr
sales keep changing the stock and the reset would never converge.
"""
from __future__ import annotations

from typing import Callable

from flask import Flask, jsonify

from .actions import ActionError, Operation
from .checks import LOAD_DURATION_S, LOAD_RPS, CheckError, Checks, gate_for
from .routing import PRESETS
from .sales import BackgroundSales, SalesError

CHECKS_OFF = ("Checks are not configured in this deployment (SCENARIO_API_URL / SCENARIO_API_TOKEN absent). "
              "Use make load, make verify and make canary-check.")
SALES_OFF = "Background sales control needs the store sources (STORE_HOSTS) in this deployment."
FULL_RESET_OFF = ("Full reset needs ALB release routing (hybrid stack); in this deployment use make reset "
                  "(or Sales off, make route-baseline, Reset demo data).")
BASELINE = "route-baseline"
SALES_ON_RESET = "Background sales are on ({rate:g}/min per store): press Sales off first, or use Full reset"


def make_check_operations(checks: Checks | None, sales: BackgroundSales | None, alb,
                          reset: Callable[[Callable[[str], None]], None] | None,
                          restock=None) -> dict[str, Operation]:
    """`restock` is a scenario.presenter_actions.RestockReset (None: Full reset never touches procurement)."""
    def need_checks() -> Checks:
        if checks is None:
            raise ActionError(CHECKS_OFF)
        return checks

    def need_sales() -> BackgroundSales:
        if sales is None:
            raise ActionError(SALES_OFF)
        return sales

    def parse_canary(body: dict) -> dict:
        try:
            gate = need_checks().canary_gate()
        except CheckError as exc:
            raise ActionError(str(exc)) from exc
        return {"weights": gate["weights"], **gate["params"]}

    def run_canary(params: dict, progress):
        return need_checks().canary_check(gate_for(tuple(int(w) for w in params["weights"].split("/"))), progress)

    def parse_full_reset(body: dict) -> dict:
        need_sales()
        if alb is None or reset is None:
            raise ActionError(FULL_RESET_OFF)
        return {"weights": "/".join(map(str, PRESETS[BASELINE][1]))}

    def run_full_reset(params: dict, progress):
        out = {"sales": need_sales().off(progress)}
        out["routing"] = alb.run(BASELINE, progress)
        skip = "not configured in this deployment" if restock is None else restock.skip_reason()
        if skip is None:
            cancelled = restock.cancel_orders(progress)  # before the data reset: nothing delivered onto the baseline
        else:
            progress(f"Purchase orders and restock:eta keys not touched: {skip}")
        reset(progress)  # type: ignore[misc]
        if skip is None:
            cleared = restock.clear_etas(progress)
            out["restock"] = {"cancelled_orders": cancelled, "eta_keys_cleared": cleared}
            restock_note = f"{cancelled} open purchase order(s) cancelled, {cleared} restock:eta key(s) cleared"
        else:
            out["restock"] = {"skipped": skip}
            restock_note = f"purchase orders not touched: {skip}"
        progress("Full reset done: background sales off (rate 0), routing 100/0/0 verified, demo data at the "
                 f"seeded baseline and Redis converged; {restock_note}. Sales on restarts background sales.")
        return out

    def parse_reset(body: dict) -> dict:
        try:
            rate = need_sales().rate()
        except SalesError as exc:
            raise ActionError(f"Reset refused: cannot read the background sales rate ({exc}). Press Sales off "
                              "first, or use Full reset") from exc
        if rate > 0:
            raise ActionError(SALES_ON_RESET.format(rate=rate))
        return {}

    def fixed(need: Callable[[], object], params: dict) -> Callable[[dict], dict]:
        def parse(body: dict) -> dict:
            need()  # raises ActionError when the card is not configured here
            return dict(params)
        return parse

    ops = {
        "load": Operation(fixed(need_checks, {"duration_s": LOAD_DURATION_S, "rps": LOAD_RPS}),
                          lambda p, progress: need_checks().load(progress)),
        "verify": Operation(fixed(need_checks, {}), lambda p, progress: need_checks().verify(progress)),
        "canary-check": Operation(parse_canary, run_canary),
        "sales-off": Operation(fixed(need_sales, {}), lambda p, progress: need_sales().off(progress)),
        "sales-on": Operation(fixed(need_sales, {}), lambda p, progress: need_sales().on(progress)),
        "full-reset": Operation(parse_full_reset, run_full_reset),
    }
    if sales is not None and reset is not None:  # overrides the unguarded built-in source action of the same name
        ops["reset"] = Operation(parse_reset, lambda p, progress: reset(progress))
    return ops


def register(app: Flask, control) -> None:
    """Read-only endpoints. Authentication is the app-wide Basic Auth hook in app.create_app."""

    @app.get("/control/api/checks")
    def api_checks():
        checks = getattr(control, "checks", None)
        if checks is None:
            return jsonify({"error": CHECKS_OFF}), 503
        try:
            return jsonify(checks.view())
        except CheckError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.get("/control/api/sales")
    def api_sales():
        sales = getattr(control, "sales", None)
        if sales is None:
            return jsonify({"error": SALES_OFF}), 503
        try:
            return jsonify(sales.view())
        except SalesError as exc:
            return jsonify({"error": str(exc)}), 502


def card_html(control) -> str:
    checks_on = getattr(control, "checks", None) is not None
    sales_on = getattr(control, "sales", None) is not None
    full_on = sales_on and getattr(control, "alb", None) is not None
    return (CARDS.replace("{{CHECKS_DIS}}", "" if checks_on else " disabled")
            .replace("{{SALES_DIS}}", "" if sales_on else " disabled")
            .replace("{{FULL_DIS}}", "" if full_on else " disabled")
            .replace("{{CHECKS_ON}}", "true" if checks_on else "false")
            .replace("{{SALES_ON}}", "true" if sales_on else "false")
            .replace("{{FULL_ON}}", "true" if full_on else "false")
            .replace("{{CHECKS_OFF}}", CHECKS_OFF).replace("{{SALES_OFF}}", SALES_OFF)
            .replace("{{FULL_RESET_OFF}}", FULL_RESET_OFF)
            .replace("{{LOAD_LABEL}}", f"Run load ({LOAD_DURATION_S} s, {LOAD_RPS} rps)")
            .replace("{{LOAD_S}}", str(LOAD_DURATION_S)).replace("{{LOAD_RPS}}", str(LOAD_RPS)))


CARDS = """<!-- checks + background sales cards (demo_control/checks_card.py) -->
<style>
.chk-result { margin-top:10px; display:grid; gap:8px; }
.chk-table { width:100%; border-collapse:collapse; font-size:15px; } .chk-table th { text-align:left; color:var(--muted); font-weight:700; padding:4px 6px; border-bottom:1px solid var(--line); }
.chk-table td { padding:4px 6px; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; } .chk-table td:first-child { font-weight:800; }
.chk-gates { list-style:none; margin:0; padding:0; font-size:15px; display:grid; gap:4px; } .chk-gates li { display:flex; gap:8px; align-items:baseline; }
.chk-tag { display:inline-block; min-width:3.6em; text-align:center; font-weight:800; font-size:13px; padding:1px 6px; border-radius:999px; border:1px solid currentColor; }
.chk-pass { color:var(--ok); } .chk-fail { color:var(--err); }
.chk-verdict { font-size:20px; font-weight:800; } .chk-gate-for { font-size:15px; } .chk-gate-for code { font-size:14px; }
.chk-rate { display:flex; gap:10px; align-items:baseline; } .chk-rate b { font-size:28px; font-weight:800; }
</style>
<section class="control-card actions-card ops-card" id="chk-card"><div class="actions-layout"><div><h2>Checks</h2><div class="label">Load, verify and canary gate on the VM</div><p class="meta">Runs on the on-prem VM through the scenario API, exactly like <code>make load</code>, <code>make verify</code> and <code>make canary-check</code>: load writes <code>/out/last.json</code>, verify writes <code>/out/verify.json</code>, and the canary gate reads both. Check canary gates the newer release with traffic (the canary) against the older one: 1.1.0 vs 1.0.0 at 90/10/0, 1.2.0 vs 1.0.0 at 90/0/10 or 50/0/50, 1.2.0 alone at 0/0/100.</p></div>
<div><div class="ops-buttons"><button class="chk-btn" data-action="load"{{CHECKS_DIS}}>{{LOAD_LABEL}}</button><button class="chk-btn secondary" data-action="verify"{{CHECKS_DIS}}>Verify</button><button class="chk-btn" data-action="canary-check"{{CHECKS_DIS}}>Check canary</button><button class="secondary ops-refresh" id="chk-refresh"{{CHECKS_DIS}}>Refresh</button></div>
<p class="meta chk-gate-for" id="chk-gate-for">Gate for live routing: loading…</p></div></div>
<div class="action-progress"><div class="msg" id="chk-msg"></div><div class="chk-result" id="chk-result"></div></div></section>
<section class="control-card actions-card ops-card" id="sales-card"><div class="actions-layout"><div><h2>Background sales</h2><div class="label">jr sales rate and full reset</div><p class="meta">Sales off sets <code>sales_per_min_per_store</code> to 0 in every store (remembering the rate); Sales on restores it, then waits for a real sale. The jr containers must be running: <code>make sales-on</code> starts them, <code>make sales-off</code> and <code>make reset</code> stop them. Full reset = Sales off, Baseline (all to 1.0.0), Reset demo data; with the restock layer it also cancels open purchase orders and clears the <code>restock:eta</code> keys, like <code>make reset</code>.</p></div>
<div><div class="chk-rate"><b id="sales-rate">&mdash;</b><span class="meta" id="sales-meta">sales/min per store</span></div>
<div class="ops-buttons"><button class="sales-btn secondary" data-action="sales-off"{{SALES_DIS}}>Sales off</button><button class="sales-btn" data-action="sales-on"{{SALES_DIS}}>Sales on</button><button class="sales-btn secondary" data-action="full-reset"{{FULL_DIS}}>Full reset</button></div></div></div>
<div class="action-progress"><div class="msg" id="sales-msg"></div></div></section>
<script>
(() => {
  const CHECKS_ON = {{CHECKS_ON}}, SALES_ON = {{SALES_ON}}, FULL_ON = {{FULL_ON}};
  const q = s => document.querySelector(s);
  const checkNames = ['load', 'verify', 'canary-check'], salesNames = ['sales-off', 'sales-on', 'full-reset'];
  let gate = null, seen = null;
  async function getJson(url, opts) {
    const r = await fetch(url, opts);
    let j; try { j = await r.json(); } catch (e) { j = {error: 'HTTP ' + r.status + ' (not JSON)'}; }
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  function msg(el, cls, text) { el.className = 'msg ' + cls; el.textContent = text; }
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  const ms = v => v === null || v === undefined ? 'n/a' : Math.round(v) + ' ms';
  function table(releases, cols) {
    const t = el('table', 'chk-table'), head = el('tr');
    ['Release', ...cols.map(c => c[0])].forEach(h => head.appendChild(el('th', '', h))); t.appendChild(head);
    Object.entries(releases).forEach(([rel, d]) => { const tr = el('tr'); tr.appendChild(el('td', '', rel)); cols.forEach(c => tr.appendChild(el('td', '', c[1](d)))); t.appendChild(tr); });
    return t;
  }
  function show(c) {
    const box = q('#chk-result'); box.innerHTML = '';
    if (!c) return;
    const when = c.finished_at ? ' · ' + new Date(c.finished_at * 1000).toLocaleTimeString() : '';
    if (c.kind === 'load' && c.releases) {
      box.appendChild(el('div', 'meta', 'Last load: ' + c.count + ' requests at ' + c.params.rps + ' rps for ' + c.params.duration_s + ' s' + (c.weights ? ' · routing ' + c.weights : '') + when));
      box.appendChild(table(c.releases, [['Requests', d => d.count], ['Errors', d => d.errors], ['p50', d => ms(d.p50_ms)], ['p95', d => ms(d.p95_ms)]]));
    } else if (c.kind === 'verify' && 'ok' in c) {
      box.appendChild(el('div', 'chk-verdict ' + (c.ok ? 'chk-pass' : 'chk-fail'), c.ok ? 'VERIFY PASSED' : 'VERIFY FAILED'));
      box.appendChild(el('div', 'meta', c.mismatches + ' position mismatch(es), ' + c.sellable_mismatches + ' sellable mismatch(es)' + when));
      if (!c.ok && c.lines) box.appendChild(el('pre', 'meta', c.lines.slice(0, 8).join('\\n')));
    } else if (c.kind === 'canary-check' && c.gates) {
      box.appendChild(el('div', 'chk-verdict ' + (c.ok ? 'chk-pass' : 'chk-fail'), c.ok ? 'CANARY GATES PASSED' : 'CANARY GATES FAILED'));
      if (!c.ok) box.appendChild(el('div', 'meta chk-fail', c.gates.filter(g => !g.passed).map(g => g.reason || g.name).join('; ') + '; roll back'));
      if (c.label) box.appendChild(el('div', 'meta', 'Gate ' + (c.release_b ? c.release_b + (c.release_a ? ' vs ' + c.release_a : ' alone') + ' ' : '') + 'at ' + c.label + ' (' + c.weights + ')' + when));
      const ul = el('ul', 'chk-gates');
      c.gates.forEach(g => { const li = el('li'); li.appendChild(el('span', 'chk-tag ' + (g.passed ? 'chk-pass' : 'chk-fail'), g.passed ? 'PASS' : 'FAIL')); li.appendChild(el('span', '', g.name + ': ' + g.detail)); ul.appendChild(li); });
      box.appendChild(ul);
      if (c.releases) box.appendChild(table(c.releases, [['Samples', d => d.count], ['Errors', d => d.errors], ['p95', d => ms(d.p95_ms)]]));
    } else if (c.error) {
      box.appendChild(el('div', 'meta chk-fail', c.kind + ' ' + c.status + ': ' + c.error));
    }
  }
  async function loadChecks() {
    if (!CHECKS_ON) { msg(q('#chk-msg'), '', '{{CHECKS_OFF}}'); q('#chk-gate-for').textContent = ''; return; }
    try {
      const j = await getJson('/control/api/checks');
      gate = j.canary && !j.canary.error ? j.canary : null;
      const g = q('#chk-gate-for'); g.textContent = '';
      if (gate) { g.appendChild(document.createTextNode('Gate for live routing ' + gate.weights + ': canary ' + gate.release_b + ' (' + gate.label + ')' + (gate.release_a ? ' vs baseline ' + gate.release_a : ', no baseline') + ': ')); g.appendChild(el('code', '', 'CHECK_ARGS="' + gate.check_args + '"')); }
      else g.textContent = 'Check canary: ' + (j.canary ? j.canary.error : 'routing unknown');
      const last = j.last || {}, newest = Object.values(last).sort((a, b) => (b.finished_at || 0) - (a.finished_at || 0))[0];
      if (!seen && newest) show(newest);
      if (j.active) msg(q('#chk-msg'), '', 'A run is in progress on the VM (' + j.active + ').');
    } catch (e) { msg(q('#chk-msg'), 'err', 'Could not reach the scenario API: ' + e.message); }
  }
  async function loadSales() {
    if (!SALES_ON) { msg(q('#sales-msg'), '', '{{SALES_OFF}}'); return; }
    try {
      const j = await getJson('/control/api/sales');
      q('#sales-rate').textContent = j.rate === 0 ? 'OFF' : j.rate;
      q('#sales-meta').textContent = j.rate === 0 ? 'rate 0 · Sales on restores ' + (j.saved !== null ? j.saved : j.default + ' (default)') + '/min' : 'sales/min per store';
    } catch (e) { msg(q('#sales-msg'), 'err', 'Could not read the sales rate: ' + e.message); }
  }
  async function poll() {
    let j; try { j = await getJson('/control/api/actions'); } catch (e) { return; }
    const busy = ['queued', 'running'].includes(j.status);
    document.querySelectorAll('.chk-btn').forEach(b => b.disabled = busy || !CHECKS_ON);
    document.querySelectorAll('.sales-btn').forEach(b => b.disabled = busy || !SALES_ON || (b.dataset.action === 'full-reset' && !FULL_ON));
    const mine = checkNames.includes(j.name) ? q('#chk-msg') : salesNames.includes(j.name) ? q('#sales-msg') : null;
    if (!mine) return;
    msg(mine, j.status === 'failed' ? 'err' : j.status === 'succeeded' ? 'ok' : '', j.name + ' ' + j.status + ': ' + (j.error ? j.error + ' (last step: ' + j.progress + ')' : j.progress));
    const key = j.id + ':' + j.status;
    if (key !== seen && ['succeeded', 'failed'].includes(j.status)) {
      seen = key;
      if (checkNames.includes(j.name)) { show(j.result); loadChecks(); } else { loadSales(); loadChecks(); }
      if (j.name === 'full-reset' && q('#ops-route-refresh')) q('#ops-route-refresh').click();
    }
  }
  async function run(name, question, out) {
    if (!confirm(question)) return;
    try { await getJson('/control/api/actions/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); }
    catch (e) { msg(out, 'err', name + ' was not started: ' + e.message); return; }
    if (checkNames.includes(name)) q('#chk-result').innerHTML = '';
    poll();
  }
  const questions = {
    'load': 'Generate {{LOAD_S}} s of lookup load at {{LOAD_RPS}} rps from the VM (make load)? It overwrites /out/last.json; other actions wait until it ends.',
    'verify': 'Compare every store source with Redis and write /out/verify.json (make verify)?',
    'sales-off': 'Set background sales to 0 per minute in every store (the current rate is remembered)?',
    'sales-on': 'Restore the background sales rate and wait for a sale?',
    'full-reset': 'Full reset: background sales off, ALL traffic to 1.0.0, open purchase orders cancelled (restock layer), demo data back to the seeded baseline?'};
  document.querySelectorAll('.chk-btn').forEach(b => b.onclick = () => {
    const a = b.dataset.action;
    const question = a === 'canary-check' ? (gate ? 'Gate the last load: canary ' + gate.release_b + (gate.release_a ? ' vs baseline ' + gate.release_a : '') + ' at ' + gate.label + ' (' + gate.weights + '): CHECK_ARGS="' + gate.check_args + '"?' : 'Live routing is not a canary split; try anyway?') : questions[a];
    run(a, question, q('#chk-msg'));
  });
  document.querySelectorAll('.sales-btn').forEach(b => b.onclick = () => run(b.dataset.action, questions[b.dataset.action], q('#sales-msg')));
  q('#chk-refresh').onclick = () => { loadChecks(); loadSales(); };
  if (!FULL_ON && SALES_ON) q('[data-action="full-reset"]').title = '{{FULL_RESET_OFF}}';
  loadChecks(); loadSales(); poll(); setInterval(poll, 1000);
})();
</script>
<!-- /checks cards -->
"""
