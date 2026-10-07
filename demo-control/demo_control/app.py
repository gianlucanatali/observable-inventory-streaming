"""Flask app: /control/ page and JSON API. All state lives behind `Control`; this module is HTTP only."""
from __future__ import annotations

import hmac
import logging

from flask import Flask, Response, jsonify, render_template_string, request

from .backends import LayerOff, WriteFailed
from .registry import ValidationError, fmt
from .actions import ActionError
from . import links_card, ops
from .service import Control, Row

log = logging.getLogger("demo_control")
USER = "demo"


def row_json(r: Row) -> dict:
    p, g = r.param, r.reading
    return {"key": p.key, "label": p.label, "unit": p.unit, "default": p.default, "min": p.min, "max": p.max,
            "store": p.store, "consumers": list(p.consumers), "apply": p.apply, "layer": p.layer, "doc": p.doc,
            "read_only": p.read_only, "status": g.status, "value": g.value, "detail": g.detail}


def create_app(control: Control, password: str) -> Flask:
    app = Flask(__name__)

    @app.before_request
    def auth():
        if request.path == "/control/healthz":
            return None
        a = request.authorization
        if a is None or a.type != "basic" or not (
                hmac.compare_digest((a.username or "").encode(), USER.encode())
                and hmac.compare_digest((a.password or "").encode(), password.encode())):
            return Response("authentication required", 401, {"WWW-Authenticate": 'Basic realm="demo control"'})
        return None

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/control/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/control/api/params")
    def api_params():
        return jsonify({"stack": control.stack, "layers": control.layers(), "routing": control.routing(),
                        "params": [row_json(r) for r in control.rows()]})

    @app.get("/control/api/actions")
    def api_action_status():
        return jsonify(control.actions.status() if control.actions else {"status": "unavailable", "progress": "Actions are not configured"})

    @app.post("/control/api/actions/<name>")
    def api_action_start(name: str):
        if not control.actions:
            return jsonify({"error": "actions are not configured"}), 503
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"}), 400
        try:
            return jsonify(control.actions.start(name, body.get("product_id"), body)), 202
        except ActionError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.put("/control/api/params/<key>")
    def api_set(key: str):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "value" not in body:
            return jsonify({"error": 'body must be JSON like {"value": 12}'}), 400
        try:
            row = control.set(key, body["value"])
        except KeyError:
            return jsonify({"error": f"unknown parameter {key!r}"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except LayerOff as exc:
            return jsonify({"error": f"layer off: {exc}"}), 409
        except WriteFailed as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(row_json(row))

    @app.get("/control/")
    def page():
        rows = [row_json(r) for r in control.rows()]
        layers: dict[str, list[dict]] = {}
        for r in rows:
            layers.setdefault(r["layer"], []).append(r)
        return render_template_string(PAGE, layers=layers, stack=control.stack, running=control.layers(),
                                      routing=control.routing(), fmt=fmt, ops_cards=ops.cards_html(control),
                                      links_card=links_card.card_html(control.redis))

    ops.register(app, control)
    links_card.register(app, control)
    return app


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo control ({{ stack }})</title>
<style>
:root { --bg:#f4effb; --ink:#2a1250; --muted:#635a72; --card:#fff; --line:#e7dcf6; --accent:#632ca6; --accent-dark:#4b1f86; --accent-ink:#fff; --shadow:0 3px 8px rgba(42,18,80,.08),0 12px 28px rgba(42,18,80,.10); --radius:16px; --off:#6b7380; --err:#a0273d; --ok:#187542; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font:18px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
.control-header { background:linear-gradient(125deg,#2a1250,#632ca6); color:#fff; box-shadow:var(--shadow); }
.header-inner,.control-page { max-width:1180px; margin:0 auto; padding-left:24px; padding-right:24px; }.header-inner { padding-top:24px; padding-bottom:24px; }
h1 { font-size:36px; line-height:1.1; margin:0 0 8px; } h2 { font-size:24px; margin:32px 0 12px; text-transform:capitalize; color:var(--accent-dark); }
.ctx { color:var(--muted); font-size:16px; margin:0; } .control-header .ctx,.control-header .ctx code { color:rgba(255,255,255,.86); }
.control-page { padding-bottom:56px; }.control-card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow); }
.actions-card { margin-top:24px; padding:24px; border-color:#d9c6f1; }.actions-layout { display:grid; grid-template-columns:minmax(260px,1.25fr) minmax(360px,1fr); gap:24px; align-items:start; }
.actions-card h2 { margin:0 0 6px; }.label { font-weight:800; font-size:18px; }.meta { color:var(--muted); font-size:15px; }.actions-card .meta { margin:0; }
.action-controls { display:flex; flex-wrap:wrap; gap:10px; align-items:end; }.field { display:grid; gap:5px; flex:1 1 160px; font-size:15px; font-weight:700; color:var(--muted); }
input { min-height:44px; font:inherit; padding:8px 10px; background:#fff; color:var(--ink); border:1px solid #cdb9e8; border-radius:10px; } input[type=number] { width:8em; }
button { min-height:44px; font:inherit; font-size:16px; font-weight:800; padding:9px 14px; border-radius:10px; border:1px solid var(--accent); background:var(--accent); color:var(--accent-ink); cursor:pointer; box-shadow:0 4px 10px rgba(99,44,166,.18); }
button:hover { background:var(--accent-dark); border-color:var(--accent-dark); } button.secondary { background:#fff; color:var(--accent-dark); box-shadow:none; } button:disabled,input:disabled { opacity:.5; cursor:not-allowed; }
.action-progress { margin-top:18px; padding:12px 14px; border-radius:12px; background:#eee9f4; }.msg { min-height:1.5em; margin:0; font-size:15px; } .msg.err { color:var(--err); } .msg.ok { color:var(--ok); }
.row { display:grid; grid-template-columns:minmax(250px,1.2fr) minmax(230px,1fr) minmax(280px,1fr); gap:16px 24px; padding:20px 22px; margin:12px 0; }.row.off { opacity:.6; }.value { font-size:26px; font-weight:800; }.unit { font-size:16px; color:var(--muted); font-weight:400; }
.badge { display:inline-block; font-size:14px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); margin-right:6px; }.badge.off { color:var(--off); }.badge.err { color:var(--err); border-color:var(--err); }
@media (max-width:800px) { .header-inner,.control-page { padding-left:16px; padding-right:16px; }.actions-layout,.row { grid-template-columns:1fr; } h1 { font-size:30px; } }
</style></head><body>
<header class="control-header"><div class="header-inner"><h1>Demo control <span class="ctx">stack <code>{{ stack }}</code></span></h1>
<p class="ctx">Layers running: <code>{{ running|join(', ') if running is not none else 'unknown' }}</code>
 &nbsp;|&nbsp; Canary routing: <code>{{ routing if routing is not none else 'unknown' }}</code></p></div></header>
<main class="control-page">
{{ links_card|safe }}
<section class="control-card actions-card" id="actions"><div class="actions-layout"><div><h2>Actions</h2><div class="label">Source data actions</div><p class="meta">These actions update PostgreSQL only, then verify the Redis serving view. Reset demo data refuses to start while background sales are on (they keep changing stock, so the reset could never converge): press <b>Sales off</b> first, or use <b>Full demo reset</b> (Background sales card) or <code>make reset</code>, which also stop sales and restore 100/0/0 routing. Reset never changes ALB routing.</p></div><div class="action-controls"><div class="field"><label for="product-id">Product ID</label><input id="product-id" value="P0042" aria-label="Product ID"><div class="meta">Lab 5.1: <button type="button" class="secondary pick-product" data-product="P0042" title="Trailrunner GTX EU 42: AI">P0042</button> <button type="button" class="secondary pick-product" data-product="P0048" title="Dolomia Evo Charcoal EU 42: AI">P0048</button> <button type="button" class="secondary pick-product" data-product="P0092" title="Pathfinder Lite Navy EU 42: nothing comparable">P0092</button></div></div><button id="sell-out">Sell out product</button><button id="reset-data" class="secondary">Reset demo data</button></div></div><div class="action-progress"><div class="msg action-status">Loading action status…</div></div></section>
{{ ops_cards|safe }}
{% for layer, rows in layers.items() %}
<h2>{{ layer }}</h2>
{% for r in rows %}
<div class="row control-card parameter-card {{ 'off' if r.status == 'layer_off' }}" data-key="{{ r.key }}" data-default="{{ r.default }}">
  <div><div class="label">{{ r.label }}</div><div class="meta"><code>{{ r.key }}</code></div>
    <div class="meta">{{ r.doc }}</div></div>
  <div>
    <div class="value"><span class="cur">{% if r.status in ('ok','readonly') %}{{ fmt(r.value) }}{% elif r.status == 'unset' %}{{ fmt(r.default) }}{% else %}&mdash;{% endif %}</span>
      <span class="unit">{{ r.unit }}</span></div>
    <div class="meta st">
      {% if r.status == 'unset' %}<span class="badge">default in use</span>
      {% elif r.status == 'layer_off' %}<span class="badge off">layer off</span>
      {% elif r.status == 'error' %}<span class="badge err">error</span>
      {% elif r.status == 'readonly' %}<span class="badge">read-only</span>{% endif %}{{ r.detail }}</div>
    <div class="meta">default {{ fmt(r.default) }} &middot; range {{ fmt(r.min) }}..{{ fmt(r.max) }} &middot; apply: {{ r.apply }}</div>
    <div class="meta">read by {{ r.consumers|join(', ') }}</div>
  </div>
  <div>
    {% set locked = r.read_only or r.status == 'layer_off' %}
    <input type="number" step="any" min="{{ r.min }}" max="{{ r.max }}" value="{{ fmt(r.value if r.value is not none else r.default) }}" {{ 'disabled' if locked }}>
    <button class="save" {{ 'disabled' if locked }}>Save</button>
    <button class="secondary reset" {{ 'disabled' if locked }}>Reset to default</button>
    <div class="msg"></div>
  </div>
</div>
{% endfor %}{% endfor %}
</main>
<script>
async function put(row, value) {
  const msg = row.querySelector('.msg'); msg.className = 'msg'; msg.textContent = 'saving...';
  try {
    const r = await fetch('/control/api/params/' + row.dataset.key, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({value})});
    const j = await r.json();
    if (!r.ok) { msg.className = 'msg err'; msg.textContent = j.error || ('HTTP ' + r.status); return; }
    row.querySelector('.cur').textContent = j.value === null ? row.dataset.default : j.value;
    row.querySelector('input').value = j.value === null ? row.dataset.default : j.value;
    msg.className = 'msg ok'; msg.textContent = 'saved ' + new Date().toLocaleTimeString();
  } catch (e) { msg.className = 'msg err'; msg.textContent = 'request failed: ' + e; }
}
document.querySelectorAll('.row').forEach(row => {
  if (!row.querySelector('.save')) return;
  row.querySelector('.save').onclick = () => { const v = row.querySelector('input').value; if (v === '') { row.querySelector('.msg').className='msg err'; row.querySelector('.msg').textContent='enter a number'; return; } put(row, Number(v)); };
  row.querySelector('.reset').onclick = () => put(row, Number(row.dataset.default));
});
const actionStatus = document.querySelector('.action-status');
async function actionState() { const r = await fetch('/control/api/actions'); const j = await r.json(); actionStatus.className='msg ' + (j.status === 'failed' ? 'err' : j.status === 'succeeded' ? 'ok' : ''); actionStatus.textContent = (j.status || 'unknown') + ': ' + (j.error || j.progress || ''); const busy = ['queued','running'].includes(j.status); document.querySelector('#sell-out').disabled=busy; document.querySelector('#reset-data').disabled=busy; }
async function runAction(name) { if (!confirm(name === 'reset' ? 'Reset source data to the deterministic baseline? (Background sales must be off.)' : 'Sell out this product in every store?')) return; const body=name === 'sell-out' ? {product_id:document.querySelector('#product-id').value} : {}; const r=await fetch('/control/api/actions/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); if(!r.ok){ actionStatus.className='msg err'; actionStatus.textContent=j.error || 'Action request failed'; return; } actionState(); }
document.querySelectorAll('.pick-product').forEach((b)=>{ b.onclick=()=>{ document.querySelector('#product-id').value=b.dataset.product; }; }); document.querySelector('#sell-out').onclick=()=>runAction('sell-out'); document.querySelector('#reset-data').onclick=()=>runAction('reset'); actionState(); setInterval(actionState, 1000);
</script></body></html>
"""
