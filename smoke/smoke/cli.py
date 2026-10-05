"""`smoke [--offers] [--json-out PATH]`: probes the running stack, prints PASS/FAIL per check, exits 1 on any FAIL."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from . import checks
from .checks import Result

BASE_URL = os.environ.get("BASE_URL", "http://nginx")
CONNECT_URL = os.environ.get("CONNECT_URL", "http://connect:8083")
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
PRODUCTS = ("P0042", "P0001", "P0100")
RENDER_TIMEOUT_MS = 20_000


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="smoke", description=__doc__)
    ap.add_argument("--offers", action="store_true", help="also post a cart ADD and ABANDON (offer path)")
    ap.add_argument("--json-out", help="write the results as JSON to this path")
    return ap


def guarded(name: str, fn) -> list[Result]:
    """Run one check group; an unexpected exception is a FAIL naming the check, never a crash that hides the rest."""
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001 - reported as FAIL with type and message
        return [Result(name, False, f"{type(exc).__name__}: {exc}")]
    return out if isinstance(out, list) else [out]


def check_connect(client: httpx.Client) -> Result:
    r = client.get(f"{CONNECT_URL}/connectors", params={"expand": "status"})
    r.raise_for_status()
    return checks.evaluate_connectors(r.json())


def check_api(client: httpx.Client) -> list[Result]:
    results = []
    for p in PRODUCTS:
        results += guarded(f"api: availability {p}", lambda p=p: _availability(client, p))
    return results


def _availability(client: httpx.Client, product: str) -> Result:
    r = client.get(f"{BASE_URL}/api/availability/{product}")
    try:
        body = r.json()
    except ValueError:
        body = None
    return checks.evaluate_availability(product, r.status_code, body)


def check_browser() -> list[Result]:
    from playwright.sync_api import sync_playwright

    results: list[Result] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for page_name, route, shot in (("home", "/", "smoke-home.png"), ("product", "/#/product/P0042", "smoke-product.png")):
                results += guarded(f"browser: {page_name}", lambda a=(browser, page_name, route, shot): _browse(*a))
        finally:
            browser.close()
    return results


def _browse(browser, page_name: str, route: str, shot: str) -> list[Result]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    console_errors: list[str] = []
    failed: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(f"uncaught: {e}"))
    page.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url} ({r.failure})"))
    page.on("response", lambda r: failed.append(f"{r.request.method} {r.url} HTTP {r.status}") if r.status >= 400 else None)
    results: list[Result] = []
    try:
        page.goto(BASE_URL + route, wait_until="load", timeout=RENDER_TIMEOUT_MS)
        if page_name == "home":
            page.get_by_test_id("product-card").first.wait_for(timeout=RENDER_TIMEOUT_MS)
            results.append(checks.evaluate_catalogue(page.get_by_test_id("product-card").count()))
        else:
            panel = page.get_by_test_id("stock-panel")
            panel.wait_for(timeout=RENDER_TIMEOUT_MS)
            deadline = time.monotonic() + RENDER_TIMEOUT_MS / 1000
            # The panel says "Checking availability…" until the first answer; the footer shows "–" until then too.
            while "Checking availability" in panel.inner_text() and time.monotonic() < deadline:
                page.wait_for_timeout(250)
            page.wait_for_timeout(1500)  # a second poll: a transient first answer should not pass for a healthy page
            results.append(checks.evaluate_product_page(panel.inner_text(), page.locator("footer.foot").inner_text()))
        # Let late failures (RUM, beacons, polling) surface before judging.
        page.wait_for_timeout(500)
        results.append(checks.evaluate_browser_noise(page_name, console_errors, failed))
    finally:
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUT_DIR / shot), full_page=True)
        except Exception as exc:  # noqa: BLE001 - a missing screenshot is itself a FAIL line
            results.append(Result(f"browser: {page_name} screenshot", False, f"{type(exc).__name__}: {exc}"))
        context.close()
    return results


def check_control(client: httpx.Client) -> Result:
    password = os.environ.get("CONTROL_PASSWORD")
    if not password:
        return Result("control: panel and parameters", False, "CONTROL_PASSWORD is not set in the container")
    auth = httpx.BasicAuth("demo", password)
    page = client.get(f"{BASE_URL}/control/", auth=auth)
    params = client.get(f"{BASE_URL}/control/api/params", auth=auth)
    try:
        body = params.json()
    except ValueError:
        body = None
    return checks.evaluate_control(page.status_code, page.text, params.status_code, body)


def check_offers(client: httpx.Client) -> list[Result]:
    """One ADD and one ABANDON cart event: the only state this test changes. Nothing is sold."""
    product = "P0001"
    base = {"store_id": "ONLINE", "product_id": product}
    r = client.post(f"{BASE_URL}/api/cart", json={**base, "event_type": "ADD"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
    add = checks.evaluate_cart("ADD", r.status_code, body)
    if not add.ok:
        return [add]
    r2 = client.post(f"{BASE_URL}/api/cart", json={**base, "cart_id": body["cart_id"], "event_type": "ABANDON"})
    body2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else None
    return [add, checks.evaluate_cart("ABANDON", r2.status_code, body2)]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)


    results: list[Result] = []
    with httpx.Client(timeout=15) as client:
        results += guarded("connect: connectors and tasks RUNNING", lambda: check_connect(client))
        results += guarded("api", lambda: check_api(client))
        results += guarded("browser", check_browser)
        results += guarded("control: panel and parameters", lambda: check_control(client))
        if args.offers:
            results += guarded("offers: cart", lambda: check_offers(client))
    for r in results:
        print(r.line(), flush=True)
    failed = [r for r in results if not r.ok]
    print(f"smoke: {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"ok": not failed, "results": [r.as_dict() for r in results]}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
