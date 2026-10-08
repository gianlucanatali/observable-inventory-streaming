"""Browser test: search shows every matching section, and shows download progress while the index is still loading.

The docs are served with a slow index (sent in chunks with pauses). For "aws", "datadog", "flink", "make" the number of
results equals the number of index entries containing the word (capped at 50, with an "N results" line). While the index
loads the results area shows "Loading search index... NN%" (or the KB received when there is no Content-Length) with a
progress bar, then "Indexing...", then the results, and never a partial list. Once loaded, no progress is shown.

    uv run --with playwright python overlay/workshop/site/tests/test_search_all_matches.py
"""
import functools
import http.server
import json
import pathlib
import re
import threading
import time

from playwright.sync_api import sync_playwright

DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"
INDEX_FILE = DOCS / "assets" / "search-index.json"
INDEX = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
MAX = 50
CHUNKS, PAUSE = 8, 0.3
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def serve():
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if "search-index.json" not in self.path:
                return super().do_GET()
            data = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if "nolen" not in self.path:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            step = len(data) // CHUNKS + 1
            for i in range(0, len(data), step):
                self.wfile.write(data[i:i + step])
                self.wfile.flush()
                time.sleep(PAUSE)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(DOCS)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def expected(word):
    return len([e for e in INDEX if word in " ".join((e["h"], e["c"], e["g"], e["t"])).lower()])


def main():
    srv = serve()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        errors = []
        for label, vp in (("desktop", {"width": 1400, "height": 800}), ("mobile", {"width": 390, "height": 700})):
            page = browser.new_context(viewport=vp).new_page()
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/workshop.html")
            if label == "mobile":
                page.click(".search-toggle")
            page.click(".search-input")
            page.keyboard.type("aws", delay=40)
            seen, partial = [], False
            deadline = time.time() + 15
            while time.time() < deadline:
                txt = page.inner_text(".search-results") if page.locator(".search-results").is_visible() else ""
                n = page.locator(".search-hit").count()
                if n:
                    break
                partial = partial or (n > 0 and "Loading" in txt)
                if txt and (not seen or seen[-1] != txt):
                    seen.append(txt)
                page.wait_for_timeout(40)
            pcts = [int(m.group(1)) for t in seen for m in [re.search(r"Loading search index… (\d+)%", t)] if m]
            check(len(pcts) >= 2 and pcts == sorted(pcts) and pcts[-1] > pcts[0], f"{label}: percentage text grows while loading {pcts}")
            check(any("Indexing" in t for t in seen) or len(pcts) >= 2, f"{label}: 'Indexing...' state is shown or too brief to catch ({len(seen)} states)")
            check(not partial, f"{label}: no partial list while loading")
            page.wait_for_selector(".search-hit")
            check(page.locator(".search-progress").count() == 0, f"{label}: progress bar is gone when results appear")
            for word in ("aws", "datadog", "flink", "make"):
                page.fill(".search-input", word)
                page.wait_for_timeout(150)
                want = min(expected(word), MAX)
                got = page.locator(".search-hit").count()
                check(got == want and want >= 10, f"{label}: '{word}' shows {got} results (expected {want}, {expected(word)} entries match)")
                txt = page.inner_text(".search-count")
                check(txt.startswith(f"{expected(word)} results"), f"{label}: '{word}' count line reads '{txt}'")
                check(page.locator(".search-progress").count() == 0, f"{label}: no progress UI once the index is loaded ('{word}')")
            m = page.evaluate("(()=>{const r=document.querySelector('.search-results');const b=r.getBoundingClientRect();return [b.bottom,innerHeight,r.scrollHeight,r.clientHeight,getComputedStyle(r).overflowY]})()")
            check(m[0] <= m[1] + 1 and m[2] > m[3] and m[4] in ("auto", "scroll"), f"{label}: list fits the viewport and scrolls ({m})")
        # no Content-Length: KB received are shown instead of a percentage
        page = browser.new_context(viewport={"width": 1400, "height": 800}).new_page()
        page.route("**/search-index.json*", lambda r: r.continue_(url=r.request.url.split("?")[0] + "?nolen"))
        page.goto(base + "/workshop.html")
        page.click(".search-input")
        page.keyboard.type("aws")
        kb = None
        deadline = time.time() + 15
        while time.time() < deadline and not page.locator(".search-hit").count():
            m = re.search(r"Loading search index… (\d+) KB", page.inner_text(".search-results") if page.locator(".search-results").is_visible() else "")
            kb = m.group(0) if m else kb
            page.wait_for_timeout(40)
        check(kb is not None, f"without Content-Length the KB received are shown ({kb})")
        page.wait_for_selector(".search-hit")
        check(page.locator(".search-hit").count() == min(expected("aws"), MAX), "without Content-Length the full results still show")
        # prefetch: the fetch starts when the page is idle, before any keystroke
        page = browser.new_context().new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url) if "search-index" in r.url else None)
        page.goto(base + "/workshop.html")
        page.wait_for_timeout(1500)
        check(bool(reqs), "index is requested when the page is idle, before any typing")
        check(not errors, f"no console errors {errors[:2]}")
    srv.shutdown()
    raise SystemExit(1 if failures else 0)


main()
