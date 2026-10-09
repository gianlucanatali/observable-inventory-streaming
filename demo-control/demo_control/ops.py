"""Release routing and store feed cards: action wiring, read-only JSON endpoints and the self-contained UI block.
The Checks and Background sales cards live in checks_card.py and are appended here.

Kept apart from app.py's page template so the panel styling can change independently of these two cards.
"""
from __future__ import annotations

from flask import Flask, jsonify

from . import checks_card
from .actions import ActionError, Operation
from .routing import PRESETS, ROLLBACK, RoutingError, WeightedRouting
from .store_feed import FEED_ACTIONS, STORES, ConnectFeeds, FeedError

ROUTING_OFF = ("Release routing is not configured in this deployment (neither ALB_INVENTORY_* nor NGINX_ROUTING_DIR "
               "is set). It is available on the hybrid stack (ALB rule) and in local mode (nginx upstream).")
FEEDS_OFF = "Store feed control is not configured in this deployment (CONNECT_URL absent)."


def make_operations(alb: WeightedRouting | None, feeds: ConnectFeeds | None, checks=None, sales=None,
                    reset=None, restock=None) -> dict[str, Operation]:
    def routing_op(name: str) -> Operation:
        def parse(body: dict) -> dict:
            if alb is None:
                raise ActionError(ROUTING_OFF)
            return {} if name == ROLLBACK else {"weights": "/".join(str(w) for w in PRESETS[name][1])}
        return Operation(parse, lambda params, progress: alb.run(name, progress))

    def feed_op(name: str) -> Operation:
        def parse(body: dict) -> dict:
            if feeds is None:
                raise ActionError(FEEDS_OFF)
            store = body.get("store")
            if not isinstance(store, str) or store not in STORES:
                raise ActionError(f"store must be one of {', '.join(STORES)}")
            return {"store": store}
        return Operation(parse, lambda params, progress: feeds.run(name, params["store"], progress))

    ops = {name: routing_op(name) for name in (*PRESETS, ROLLBACK)}
    ops.update({name: feed_op(name) for name in FEED_ACTIONS})
    ops.update(checks_card.make_check_operations(checks, sales, alb, reset, restock))
    return ops


def register(app: Flask, control) -> None:
    """Read-only endpoints. Authentication is the app-wide Basic Auth hook in app.create_app."""

    @app.get("/control/api/routing")
    def api_routing():
        alb = getattr(control, "alb", None)
        if alb is None:
            return jsonify({"error": ROUTING_OFF}), 503
        try:
            return jsonify(alb.view())
        except RoutingError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.get("/control/api/store-feeds")
    def api_store_feeds():
        feeds = getattr(control, "feeds", None)
        if feeds is None:
            return jsonify({"error": FEEDS_OFF}), 503
        return jsonify({"feeds": feeds.statuses()})

    checks_card.register(app, control)


def cards_html(control) -> str:
    backend = getattr(control, "alb", None)  # routing backend: AlbRouting (hybrid) or NginxRouting (local mode)
    routing_on, feeds_on = backend is not None, getattr(control, "feeds", None) is not None
    dis = "" if routing_on else " disabled"

    def btn(name: str, cls: str = "") -> str:
        if name == ROLLBACK:
            return f'<button class="ops-btn ops-route{cls}" data-action="{ROLLBACK}"{dis}>Rollback</button>'
        label, w, why = PRESETS[name]
        return (f'<button class="ops-btn ops-route{cls}" data-action="{name}" '
                f'data-weights="{"/".join(map(str, w))}" data-why="{why}"{dis}>{label}</button>')

    def group(title: str, body: str, cls: str = "") -> str:
        return f'<div class="ops-group{cls}"><span class="ops-group-label">{title}</span><div class="ops-buttons">{body}</div></div>'

    # Story order (talk flow): 1.1.0 canary, recover, 1.2.0 canary; the Incident preset is never pressed live.
    grouped = {"canary-110-10", "route-baseline", "canary-10", "canary-100", "canary-50", "incident"}
    rest = "".join(btn(n) for n in PRESETS if n not in grouped)  # any future preset still gets a button
    buttons = (group("1.1.0", btn("canary-110-10"))
               + group("Recover", btn(ROLLBACK) + btn("route-baseline", " secondary"))
               + group("1.2.0", btn("canary-10") + btn("canary-100") + btn("canary-50", " secondary ops-quiet") + rest)
               + group("Danger", btn("incident", " ops-danger"), " ops-group-danger"))
    stores = "".join(f'<option value="{s}"{" selected" if s == "S03" else ""}>{s}</option>' for s in STORES)
    dis = "" if feeds_on else " disabled"
    return (OPS_HTML.replace("{{ROUTE_BUTTONS}}", buttons).replace("{{STORE_OPTIONS}}", stores)
            .replace("{{FEED_DISABLED}}", dis)
            .replace("{{ROUTING_ON}}", "true" if routing_on else "false")
            .replace("{{FEEDS_ON}}", "true" if feeds_on else "false")
            .replace("{{ROUTING_OFF}}", ROUTING_OFF).replace("{{FEEDS_OFF}}", FEEDS_OFF)
            .replace("{{ROUTING_TEXT}}", backend.card_text if backend else "Changes the release traffic split, then reads it back.")
            .replace("{{ROUTING_LIVE}}", f"Live {backend.label}" if backend else "Live weights")) + checks_card.card_html(control)


OPS_HTML = """<!-- ops cards: release routing + store feed (demo_control/ops.py) -->
<style>
.ops-weights { display:flex; gap:6px; margin:6px 0; }
.ops-weight { flex:1; background:#faf7fe; border:1px solid var(--line); border-radius:12px; padding:8px 10px; text-align:center; }
.ops-weight b { display:block; font-size:22px; font-weight:800; line-height:1.2; } .ops-weight span { color:var(--muted); font-size:14px; font-weight:700; }
.ops-weight.hot { border-color:var(--accent); background:#eee9f4; box-shadow:inset 0 -4px 0 var(--accent); }
.ops-buttons { display:flex; flex-wrap:wrap; gap:6px; align-items:end; } .ops-buttons button { white-space:nowrap; }
.ops-feeds { width:100%; border-collapse:collapse; font-size:15px; margin:4px 0 0; } .ops-feeds td { padding:4px 6px; border-bottom:1px solid var(--line); }
.ops-feeds td:first-child { font-weight:800; width:3.5em; }
.ops-state-PAUSED { color:var(--err); font-weight:800; } .ops-state-RUNNING { color:var(--ok); font-weight:800; } .ops-state-ERROR, .ops-state-FAILED { color:var(--err); font-weight:800; }
select.ops-store { min-height:44px; font:inherit; padding:8px 10px; background:#fff; color:var(--ink); border:1px solid #cdb9e8; border-radius:10px; }
.ops-card .action-progress { display:grid; gap:4px; }
/* live state sits under the card title (left), the buttons that change it on the right */
.ops-live { margin-top:6px; } .ops-live-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.ops-live .ops-weights { margin:4px 0; }
.ops-groups { display:grid; gap:6px; }
.ops-group { display:grid; grid-template-columns:4.6em 1fr; gap:8px; align-items:center; }
.ops-group-label { font-size:13px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }
.ops-group-danger { margin-top:6px; padding-top:8px; border-top:1px dashed var(--line); }
.ops-group-danger .ops-group-label { color:var(--err); }
button.ops-danger { background:#fff; color:var(--err); border:1px dashed var(--err); box-shadow:none; font-weight:700; }
button.ops-danger:hover { background:#fbecef; border-color:var(--err); }
button.ops-quiet { border-style:dashed; font-weight:700; color:var(--muted); }
button.ops-refresh { min-height:26px; padding:2px 8px; font-size:13px; font-weight:700; border-color:var(--line); color:var(--muted); }
button.ops-refresh::before { content:"\\21BB\\00a0"; }
button.ops-refresh:hover { color:var(--accent-dark); border-color:var(--accent); background:#faf7fe; }
.ops-card .action-progress:not(:has(> :not(:empty))) { display:none; } .ops-card .action-progress > .msg:empty { display:none; }
.ops-feed-controls .field { flex:0 0 auto; } .ops-feed-controls select.ops-store { min-height:34px; padding:4px 8px; }
.ops-feeds td:nth-child(3) { color:var(--muted); font-size:13px; }
</style>
<section class="control-card actions-card ops-card" id="ops-routing"><div class="actions-layout"><div><h2>Release routing</h2><div class="label">Inventory API traffic split</div><p class="meta">{{ROUTING_TEXT}} Same weights as <code>make canary-110-10</code>, <code>incident</code>, <code>canary-10/50/100</code>, <code>rollback</code>, <code>route-baseline</code>. Rollback restores the routing in effect before the last change.</p>
<div class="ops-live"><div class="ops-live-head"><span class="meta">{{ROUTING_LIVE}}</span><button class="secondary ops-refresh" id="ops-route-refresh" title="Read the live weights again">Refresh weights</button></div><div class="ops-weights" id="ops-weights"><div class="ops-weight"><b>&mdash;</b><span>1.0.0</span></div><div class="ops-weight"><b>&mdash;</b><span>1.1.0</span></div><div class="ops-weight"><b>&mdash;</b><span>1.2.0</span></div></div>
<div class="meta" id="ops-route-prev">Previous: unknown</div></div></div>
<div class="ops-groups">{{ROUTE_BUTTONS}}</div></div>
<div class="action-progress"><div class="msg" id="ops-route-msg"></div></div></section>
<section class="control-card actions-card ops-card" id="ops-feeds"><div class="actions-layout"><div><h2>Store feed</h2><div class="label">Debezium store connectors</div><p class="meta">Pauses or resumes one store's change feed through the Kafka Connect REST API on the on-prem VM (same as <code>make store-pause|store-resume STORE=Sxx</code>). A paused store stops sending stock changes; its source keeps selling.</p>
<div class="ops-live"><table class="ops-feeds" id="ops-feed-table"><tbody><tr><td colspan="3">Loading connector state…</td></tr></tbody></table></div></div>
<div class="action-controls ops-feed-controls"><div class="field"><label for="ops-store">Store</label><select class="ops-store" id="ops-store"{{FEED_DISABLED}}>{{STORE_OPTIONS}}</select></div>
<button class="ops-btn ops-feed" data-action="store-pause"{{FEED_DISABLED}}>Pause feed</button>
<button class="ops-btn ops-feed secondary" data-action="store-resume"{{FEED_DISABLED}}>Resume feed</button>
<button class="secondary ops-refresh" id="ops-feed-refresh"{{FEED_DISABLED}}>Refresh state</button></div></div>
<div class="action-progress"><div class="msg" id="ops-feed-msg"></div></div></section>
<script>
(() => {
  const ROUTING_ON = {{ROUTING_ON}}, FEEDS_ON = {{FEEDS_ON}};
  const q = s => document.querySelector(s);
  const routeNames = [...document.querySelectorAll('.ops-route')].map(b => b.dataset.action);
  const feedNames = ['store-pause', 'store-resume'];
  let prev = null, seen = null;
  async function getJson(url, opts) {
    const r = await fetch(url, opts);
    let j; try { j = await r.json(); } catch (e) { j = {error: 'HTTP ' + r.status + ' (not JSON)'}; }
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  function msg(el, cls, text) { el.className = 'msg ' + cls; el.textContent = text; }
  async function loadRouting() {
    if (!ROUTING_ON) { msg(q('#ops-route-msg'), '', '{{ROUTING_OFF}}'); return; }
    try {
      const j = await getJson('/control/api/routing');
      const cells = q('#ops-weights').children;
      ['1.0.0', '1.1.0', '1.2.0'].forEach((r, i) => { cells[i].querySelector('b').textContent = j.weights[r] + '%'; cells[i].classList.toggle('hot', j.weights[r] > 0); });
      prev = j.previous;
      q('#ops-route-prev').textContent = 'Live ' + j.current.split(' ').join('/') + ' · previous (for Rollback): ' + (j.previous ? j.previous.split(' ').join('/') : 'none recorded');
    } catch (e) { msg(q('#ops-route-msg'), 'err', 'Could not read the live routing: ' + e.message); }
  }
  async function loadFeeds() {
    if (!FEEDS_ON) { msg(q('#ops-feed-msg'), '', '{{FEEDS_OFF}}'); q('#ops-feed-table tbody').innerHTML = ''; return; }
    try {
      const j = await getJson('/control/api/store-feeds');
      const body = q('#ops-feed-table tbody'); body.innerHTML = '';
      j.feeds.forEach(f => { const tr = document.createElement('tr');
        [f.store, f.state, f.tasks.length ? 'tasks ' + f.tasks.join(', ') : ''].forEach((t, i) => { const td = document.createElement('td'); td.textContent = t; td.title = f.error || f.connector; if (i === 1) td.className = 'ops-state-' + f.state; if (i === 2) td.className = 'meta'; tr.appendChild(td); });
        body.appendChild(tr); });
      const bad = j.feeds.filter(f => f.error);
      if (bad.length) msg(q('#ops-feed-msg'), 'err', bad[0].error);
    } catch (e) { msg(q('#ops-feed-msg'), 'err', 'Could not read connector state: ' + e.message); }
  }
  async function poll() {
    let j; try { j = await getJson('/control/api/actions'); } catch (e) { return; }
    const busy = ['queued', 'running'].includes(j.status);
    document.querySelectorAll('.ops-route').forEach(b => b.disabled = busy || !ROUTING_ON);
    document.querySelectorAll('.ops-feed').forEach(b => b.disabled = busy || !FEEDS_ON);
    const mine = routeNames.includes(j.name) ? q('#ops-route-msg') : feedNames.includes(j.name) ? q('#ops-feed-msg') : null;
    if (!mine) return;
    const where = j.params && j.params.store ? ' ' + j.params.store : '';
    msg(mine, j.status === 'failed' ? 'err' : j.status === 'succeeded' ? 'ok' : '', j.name + where + ' ' + j.status + ': ' + (j.error ? j.error + ' (last step: ' + j.progress + ')' : j.progress));
    const key = j.id + ':' + j.status;
    if (key !== seen && ['succeeded', 'failed'].includes(j.status)) { seen = key; routeNames.includes(j.name) ? loadRouting() : loadFeeds(); }
  }
  async function run(name, body, question, out) {
    if (!confirm(question)) return;
    try { await getJson('/control/api/actions/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
    catch (e) { msg(out, 'err', name + ' was not started: ' + e.message); return; }
    poll();
  }
  document.querySelectorAll('.ops-route').forEach(b => b.onclick = () => run(b.dataset.action, {},
    b.dataset.action === 'rollback' ? 'Restore the previous routing (' + (prev ? prev.split(' ').join('/') : 'none recorded') + ')?'
      : 'Route ' + b.dataset.weights + ' to 1.0.0/1.1.0/1.2.0 (' + b.dataset.why + ')? This changes the live routing.', q('#ops-route-msg')));
  document.querySelectorAll('.ops-feed').forEach(b => b.onclick = () => { const s = q('#ops-store').value;
    run(b.dataset.action, {store: s}, (b.dataset.action === 'store-pause' ? 'Pause' : 'Resume') + ' the Debezium feed of store ' + s + ' (inventory-' + s.toLowerCase() + ')?', q('#ops-feed-msg')); });
  q('#ops-route-refresh').onclick = loadRouting; q('#ops-feed-refresh').onclick = loadFeeds;
  loadRouting(); loadFeeds(); poll(); setInterval(poll, 1000);
})();
</script>
<!-- /ops cards -->
"""
