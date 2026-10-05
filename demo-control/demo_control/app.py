"""Flask app: /control/ page and JSON API. All state lives behind `Control`; this module is HTTP only."""
from __future__ import annotations

import hmac
import logging

from flask import Flask, Response, jsonify, render_template_string, request

from .backends import LayerOff, WriteFailed
from .registry import ValidationError, fmt
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
                                      routing=control.routing(), fmt=fmt)

    return app


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo control ({{ stack }})</title>
<style>
:root { --bg:#fff; --fg:#14181f; --muted:#5b6472; --line:#d5dae1; --card:#f5f7fa; --accent:#632ca6; --off:#9aa3af; --err:#b3261e; --ok:#1b7f3b; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181f; --fg:#eef1f5; --muted:#a3acb9; --line:#323a47; --card:#1c222c; --accent:#b794f4; --off:#6b7380; --err:#ff8a80; --ok:#6fdc8c; } }
body { background:var(--bg); color:var(--fg); font:20px/1.4 system-ui,sans-serif; margin:0; padding:24px 32px 64px; }
h1 { font-size:32px; margin:0 0 4px; } h2 { font-size:26px; margin:36px 0 10px; text-transform:capitalize; border-bottom:2px solid var(--accent); }
.ctx { color:var(--muted); font-size:18px; } .ctx code { color:var(--fg); }
.row { display:grid; grid-template-columns: minmax(260px,1.2fr) minmax(300px,1fr) 2fr; gap:8px 24px; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin:10px 0; }
.row.off { opacity:.6; } .label { font-weight:700; font-size:22px; } .meta { color:var(--muted); font-size:16px; }
.value { font-size:30px; font-weight:700; } .unit { font-size:18px; color:var(--muted); font-weight:400; }
.badge { display:inline-block; font-size:15px; padding:1px 8px; border-radius:10px; border:1px solid var(--line); margin-right:6px; }
.badge.off { color:var(--off); } .badge.err { color:var(--err); border-color:var(--err); }
input[type=number] { font-size:24px; width:8em; padding:4px 8px; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; }
button { font-size:18px; padding:6px 14px; border-radius:6px; border:1px solid var(--accent); background:var(--accent); color:#fff; cursor:pointer; }
button.secondary { background:transparent; color:var(--accent); } button:disabled, input:disabled { opacity:.5; cursor:not-allowed; }
.msg { min-height:1.3em; font-size:16px; } .msg.err { color:var(--err); } .msg.ok { color:var(--ok); }
@media (max-width: 900px) { .row { grid-template-columns: 1fr; } }
</style></head><body>
<h1>Demo control <span class="ctx">stack <code>{{ stack }}</code></span></h1>
<p class="ctx">Layers running: <code>{{ running|join(', ') if running is not none else 'unknown' }}</code>
 &nbsp;|&nbsp; Canary routing: <code>{{ routing if routing is not none else 'unknown' }}</code></p>
{% for layer, rows in layers.items() %}
<h2>{{ layer }}</h2>
{% for r in rows %}
<div class="row {{ 'off' if r.status == 'layer_off' }}" data-key="{{ r.key }}" data-default="{{ r.default }}">
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
  row.querySelector('.save').onclick = () => { const v = row.querySelector('input').value; if (v === '') { row.querySelector('.msg').className='msg err'; row.querySelector('.msg').textContent='enter a number'; return; } put(row, Number(v)); };
  row.querySelector('.reset').onclick = () => put(row, Number(row.dataset.default));
});
</script></body></html>
"""
