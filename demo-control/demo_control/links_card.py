"""Links card: every page of the running stack in one place (shop, Datadog, Confluent Cloud, AWS).

`make links-publish` (compose/scripts/publish-links.sh) writes the list to Redis `demo:links` from the laptop,
where Terraform state and the stack summary live; the panel only reads it. Thumbnails are example views taken
from the workshop guide, not live captures.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template_string, send_from_directory

log = logging.getLogger("demo_control")
LINKS_KEY = "demo:links"
STACK_KEY = "demo:stack"
STACK_FIELDS = ("stack", "env", "alb", "vm_public_ip", "confluent_env", "kafka_cluster")
THUMBS = Path(__file__).parent / "thumbs"
GROUPS = ("Shop", "Datadog", "Confluent", "AWS")
NOT_PUBLISHED = "No links published for this stack yet: run make links-publish on your computer."


class LinksError(RuntimeError):
    pass


def load(redis_client) -> list[dict]:
    """The published links, validated. Raises LinksError (with the reason) when the key is missing or malformed."""
    try:
        raw = redis_client.get(LINKS_KEY)
    except Exception as exc:  # noqa: BLE001 - shown on the card, not swallowed
        raise LinksError(f"Redis {LINKS_KEY} unreadable: {exc!r}") from exc
    if raw is None:
        raise LinksError(NOT_PUBLISHED)
    try:
        items = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError as exc:
        raise LinksError(f"Redis {LINKS_KEY} is not JSON: {exc}") from exc
    if not isinstance(items, list):
        raise LinksError(f"Redis {LINKS_KEY} must be a JSON list, got {type(items).__name__}")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not all(isinstance(it.get(k), str) and it.get(k) for k in ("id", "group", "name", "url")):
            raise LinksError(f"Redis {LINKS_KEY} item {i} needs non-empty id, group, name and url: {it!r}")
        if urlparse(it["url"]).scheme not in ("http", "https"):
            raise LinksError(f"Redis {LINKS_KEY} item {it['id']}: url must be http or https, got {it['url']!r}")
        thumb = it["id"] + ".jpg"
        out.append({"id": it["id"], "group": it["group"], "name": it["name"], "desc": it.get("desc", ""),
                    "url": it["url"], "host": urlparse(it["url"]).netloc,
                    "thumb": thumb if (THUMBS / thumb).is_file() else None})
    return out


def guide_json(redis_client) -> dict:
    """The JSON the workshop guide's 4.4 form takes: demo:stack plus every link url keyed by id, plus `control`.
    Raises LinksError (with the reason) when demo:stack or demo:links is missing or malformed."""
    try:
        raw = redis_client.get(STACK_KEY)
    except Exception as exc:  # noqa: BLE001 - shown on the card, not swallowed
        raise LinksError(f"Redis {STACK_KEY} unreadable: {exc!r}") from exc
    if raw is None:
        raise LinksError(f"No {STACK_KEY} published for this stack yet: run make links-publish on your computer.")
    try:
        stack = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError as exc:
        raise LinksError(f"Redis {STACK_KEY} is not JSON: {exc}") from exc
    if not isinstance(stack, dict):
        raise LinksError(f"Redis {STACK_KEY} must be a JSON object, got {type(stack).__name__}")
    bad = [k for k in STACK_FIELDS if not isinstance(stack.get(k), str) or not stack[k]]
    if bad:
        raise LinksError(f"Redis {STACK_KEY} needs non-empty string fields {bad}")
    if stack.get("version") != 1:
        raise LinksError(f"Redis {STACK_KEY} version must be 1, got {stack.get('version')!r}")
    if urlparse(stack["alb"]).scheme not in ("http", "https"):
        raise LinksError(f"Redis {STACK_KEY} alb must be an http(s) url, got {stack['alb']!r}")
    alb = stack["alb"].rstrip("/")
    links = {"control": alb + "/control/"}
    links.update((l["id"], l["url"]) for l in load(redis_client))
    return {"version": 1, **{k: stack[k] for k in STACK_FIELDS}, "alb": alb, "links": links}


def card_html(redis_client) -> str:
    try:
        links, error = load(redis_client), None
    except LinksError as exc:
        log.error("links card", extra={"fields": {"error": str(exc)}})
        links, error = [], str(exc)
    try:
        guide, guide_error = json.dumps(guide_json(redis_client), indent=2), None
    except LinksError as exc:
        guide, guide_error = "", str(exc)
    groups = [(g, [l for l in links if l["group"] == g]) for g in GROUPS]
    groups += [(g, [l for l in links if l["group"] == g]) for g in dict.fromkeys(l["group"] for l in links) if g not in GROUPS]
    return render_template_string(CARD, groups=[(g, ls) for g, ls in groups if ls], error=error,
                                  guide=guide, guide_error=guide_error)


def register(app: Flask, control) -> None:
    @app.get("/control/api/links")
    def api_links():
        try:
            return jsonify({"links": load(control.redis)})
        except LinksError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/control/api/guide-json")
    def api_guide_json():
        try:
            return jsonify(guide_json(control.redis))
        except LinksError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/control/thumbs/<name>")
    def thumb(name: str):
        if not name.endswith(".jpg") or "/" in name or not (THUMBS / name).is_file():
            abort(404)
        return send_from_directory(THUMBS, name, max_age=3600)


CARD = """<!-- links card (demo_control/links_card.py) -->
<style>
#links-card summary { cursor:pointer; list-style:none; display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
#links-card summary::-webkit-details-marker { display:none; }
#links-card summary h2 { margin:0; } #links-card summary h2::before { content:"\\25BE  "; font-size:18px; } #links-card:not([open]) summary h2::before { content:"\\25B8  "; }
.links-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; margin-top:14px; }
.link-tile { display:flex; flex-direction:column; text-decoration:none; color:var(--ink); background:#faf7fe; border:1px solid var(--line); border-radius:12px; overflow:hidden; transition:border-color .15s, box-shadow .15s; }
.link-tile:hover, .link-tile:focus-visible { border-color:var(--accent); box-shadow:0 4px 14px rgba(99,44,166,.18); }
.link-tile img, .link-tile .no-thumb { display:block; width:100%; aspect-ratio:16/9; object-fit:cover; object-position:top left; background:#eee9f4; border-bottom:1px solid var(--line); }
.link-body { padding:8px 10px 10px; } .link-group { font-size:11px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
.link-name { font-weight:800; font-size:15px; line-height:1.25; } .link-desc { color:var(--muted); font-size:13px; line-height:1.3; margin-top:2px; }
.guide-copy { margin-top:12px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
#guide-json { width:100%; height:9em; font-family:monospace; font-size:12px; margin-top:8px; }
</style>
<details class="control-card actions-card" id="links-card" open><summary><h2>Links</h2><span class="meta">Every page of this stack, each in a new tab. Pictures are example views from the workshop guide.</span></summary>
{% if error %}<p class="msg err">{{ error }}</p>{% endif %}
<div class="guide-copy"><button type="button" id="guide-copy"{% if not guide %} disabled{% endif %}>Copy for the workshop guide</button>
<span id="guide-msg" class="meta" role="status">{% if guide_error %}Copy unavailable: {{ guide_error }}{% else %}Copies this stack as JSON; paste it in the guide, section 4.4.{% endif %}</span></div>
<textarea id="guide-json" readonly hidden aria-label="Stack JSON for the workshop guide">{{ guide }}</textarea>
<div class="links-grid">{% for group, links in groups %}{% for l in links %}
<a class="link-tile" href="{{ l.url }}" target="_blank" rel="noopener noreferrer" data-link="{{ l.id }}" title="{{ l.url }}">
{% if l.thumb %}<img src="/control/thumbs/{{ l.thumb }}" alt="" loading="lazy">{% else %}<div class="no-thumb"></div>{% endif %}
<div class="link-body"><div class="link-group">{{ group }}</div><div class="link-name">{{ l.name }}</div>{% if l.desc %}<div class="link-desc">{{ l.desc }}</div>{% endif %}</div></a>
{% endfor %}{% endfor %}</div>
</details>
<script>
(function () {
  var btn = document.getElementById("guide-copy"), box = document.getElementById("guide-json"), msg = document.getElementById("guide-msg");
  if (!btn) return;
  function say(text, bad) { msg.textContent = text; msg.className = bad ? "msg err" : "meta"; }
  function viaTextarea() {  // plain http has no navigator.clipboard: select the text and use the legacy copy command
    box.hidden = false; box.focus(); box.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) say("Copied: paste it in the guide, section 4.4.");
    else say("Could not copy automatically. The JSON is selected below: press Ctrl+C (Cmd+C on Mac), then paste it in the guide, section 4.4.", true);
  }
  btn.addEventListener("click", function () {
    var text = box.value;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { say("Copied: paste it in the guide, section 4.4."); },
        function (err) { say("Clipboard refused (" + err + "), trying the fallback."); viaTextarea(); });
    } else viaTextarea();
  });
})();
</script>
"""
