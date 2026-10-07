"""Regression test for the Connect box (site.js, stack.js, go.js) on the built site in overlay/docs.

A stack pasted from `make links-json` must survive reloads on every page and be used by go.html; a saved stack
that cannot be read must be kept and reported, never removed (only the Forget button removes it).
Serves overlay/docs over http on a free port; no network beyond localhost (go.html targets are intercepted).

    uv run --with playwright python overlay/workshop/site/tests/test_connect_stack.py
"""
import functools
import http.server
import json
import pathlib
import sys
import threading

from playwright.sync_api import sync_playwright

DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"
KEY = "workshop-stack"
DD = "https://app.datadoghq.example"  # placeholder hosts and ids only: this repo is public


def links_json(stack, alb, local):
    """The shape `make links-json` prints (compose/scripts/publish-links.sh --print), with placeholder values."""
    env = "dd-demo-" + stack
    links = {"control": alb + "/control/"}
    if not local:
        links["overview-dashboard"] = DD + "/dashboard/aaa-bbb-ccc/demo-home"
    links.update({"shop": alb + "/#/product/P0042", "shop-home": alb + "/#/"})
    if not local:
        links.update({"stock-dashboard": DD + "/dashboard/ddd-eee-fff/stock",
                      "online-dashboard": DD + "/dashboard/ggg-hhh-iii/online"})
    links.update({"apm": DD + "/apm/entity/service%3Ainventory-api?env=" + env + "&version=1.1.0",
                  "dsm": DD + "/data-streams/map?env=" + env})
    if not local:
        links.update({"cost-dashboard": DD + "/dashboard/jjj-kkk-lll/cost",
                      "confluent": "https://confluent.cloud.example/environments/env-abc123/clusters/lkc-xyz789/overview",
                      "stream-lineage": "https://confluent.cloud.example/environments/env-abc123/clusters/lkc-xyz789/stream-lineage",
                      "topic-inventory-cdc": "https://confluent.cloud.example/environments/env-abc123/clusters/lkc-xyz789/topics/inventory.cdc/message-viewer",
                      "topic-stock-sellable": "https://confluent.cloud.example/environments/env-abc123/clusters/lkc-xyz789/topics/stock.sellable/overview",
                      "control-center": "http://203.0.113.10:9021",
                      "ecs": "https://eu-west-1.console.aws.example/ecs/v2/clusters/" + env + "/services?region=eu-west-1"})
    return {"version": 1, "stack": stack, "env": env, "alb": alb,
            "vm_public_ip": "localhost" if local else "203.0.113.10",
            "confluent_env": "local" if local else "env-abc123", "kafka_cluster": "local" if local else "lkc-xyz789",
            "links": links}


PAYLOADS = {
    "hybrid": links_json("hybrid", "http://dd-demo-hybrid-alb.example", local=False),
    "local": links_json("local", "http://localhost:8080", local=True),
}
# extra keys from a newer guide are accepted and ignored
PAYLOADS["newer"] = {**PAYLOADS["hybrid"], "generated_at": "2026-10-07T10:00:00Z",
                     "links": {**PAYLOADS["hybrid"]["links"], "some-new-page": DD + "/new"}}
PAGES = ["setup.html", "workshop.html", "chapter-1.html", "chapter-3.html", "chapter-6.html", "reference.html"]

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(Quiet, directory=str(DOCS))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    if not (DOCS / "setup.html").is_file():
        sys.exit(f"test_connect_stack: {DOCS}/setup.html is missing; build the site first (workshop/site/build.py)")
    srv = serve()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, payload in PAYLOADS.items():
            ctx = browser.new_context()
            # go.html leaves the site: answer every off-site request locally so the test needs no network
            ctx.route(lambda url: not url.startswith(base), lambda route: route.fulfill(status=200, body="offsite"))
            page = ctx.new_page()
            page.goto(base + "/setup.html")
            page.evaluate("document.getElementById('connect').open = true")
            page.fill("#connect-json", json.dumps(payload, indent=2))
            page.click(".connect-go")
            check(page.text_content(".connect-status").startswith("Connected: stack " + payload["stack"]),
                  f"[{name}] Connect accepts the links-json payload")
            for p in PAGES:
                page.goto(base + "/" + p)
                stored = page.evaluate(f"localStorage.getItem('{KEY}')")
                badge = page.text_content("#stack-badge .stack-badge-name")
                check(stored is not None and badge == ": stack " + payload["stack"],
                      f"[{name}] {p}: stack still saved and shown after reload")
            if "topic-inventory-cdc" in payload["links"]:
                page.goto(base + "/chapter-1.html")
                for key in ("stream-lineage", "topic-inventory-cdc", "topic-stock-sellable"):
                    hrefs = page.eval_on_selector_all(f'a[data-stack-link="{key}"]', "els => els.map(e => e.href)")
                    check(bool(hrefs) and all(h == payload["links"][key] for h in hrefs),
                          f"[{name}] chapter-1: every '{key}' link points to the stack's console page")
            page.goto(base + "/go.html#shop")
            page.wait_for_url(payload["links"]["shop"].split("#")[0] + "**")
            check(True, f"[{name}] go.html#shop redirects to the stack's shop")
            if "overview-dashboard" in payload["links"]:
                page.goto(base + "/go.html#demo-home-2")
                page.wait_for_url("**tile_focus=7000000000000002**")
                check(True, f"[{name}] go.html#demo-home-2 redirects to the demo home")
            page.goto(base + "/setup.html")
            check(page.evaluate(f"localStorage.getItem('{KEY}')") is not None, f"[{name}] stack still saved after go.html")
            ctx.close()

        # a saved stack that cannot be read: kept, reported in the box and the badge, removed only by Forget
        for name, bad in (("not JSON", "{not json"), ("other version", json.dumps({**PAYLOADS["hybrid"], "version": 2})),
                          ("missing field", json.dumps({k: v for k, v in PAYLOADS["hybrid"].items() if k != "env"}))):
            ctx = browser.new_context()
            page = ctx.new_page()
            warnings = []
            page.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)
            page.goto(base + "/setup.html")
            page.evaluate(f"v => localStorage.setItem('{KEY}', v)", bad)
            for p in PAGES:
                page.goto(base + "/" + p)
                check(page.evaluate(f"localStorage.getItem('{KEY}')") == bad, f"[{name}] {p}: unreadable stack is kept")
                check(page.text_content("#stack-badge .stack-badge-name") == ": saved stack unreadable",
                      f"[{name}] {p}: badge reports the unreadable stack")
            page.goto(base + "/setup.html")
            status = page.text_content(".connect-status")
            check(status.startswith("The saved stack could not be read:") and "connect-bad" in page.get_attribute(".connect-status", "class"),
                  f"[{name}] Connect box says why: {status[:90]}")
            check(any("saved stack could not be read" in w for w in warnings), f"[{name}] console.warn names the reason")
            page.goto(base + "/go.html#shop")
            check(page.evaluate(f"localStorage.getItem('{KEY}')") == bad and "not valid" in page.text_content("#go-message"),
                  f"[{name}] go.html reports it and keeps it")
            page.goto(base + "/setup.html")
            page.evaluate("document.getElementById('connect').open = true")
            page.click(".connect-forget")
            check(page.evaluate(f"localStorage.getItem('{KEY}')") is None and page.is_hidden("#stack-badge"),
                  f"[{name}] Forget removes it")
            ctx.close()
        browser.close()
    srv.shutdown()
    if failures:
        sys.exit(f"test_connect_stack: {len(failures)} check(s) failed: {failures}")
    print("test_connect_stack: all checks passed")


if __name__ == "__main__":
    main()
