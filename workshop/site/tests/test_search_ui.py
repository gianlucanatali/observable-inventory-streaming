"""Browser test of the site search (workshop/site/search.js) on the built site in overlay/docs, headless Chromium.

Serves overlay/docs over http on a free port. Checks: "/" focuses the box; a code query ("flink-check") and a
phrase query ("Full demo reset") show results and clicking one opens the right page#anchor; heading hits rank above
body hits; several words are AND-ed; case and accents are ignored; no match shows a message; Esc closes; Enter opens
the selected result; the one-page guide scrolls instead of navigating; mobile width uses the icon button; no
console errors.

    uv run --with playwright python overlay/workshop/site/tests/test_search_ui.py
"""
import functools
import http.server
import json
import pathlib
import sys
import threading

from playwright.sync_api import sync_playwright

DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"
INDEX = json.loads((DOCS / "assets" / "search-index.json").read_text(encoding="utf-8"))
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DOCS)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def results(page):
    return page.eval_on_selector_all(".search-hit", "els => els.map(e => ({href: e.getAttribute('href'), head: e.querySelector('.search-hit-head').textContent, marks: e.querySelectorAll('mark').length}))")


def main():
    if not (DOCS / "assets" / "search.js").is_file():
        sys.exit(f"test_search_ui: {DOCS}/assets/search.js is missing; build the site first (workshop/site/build.py)")
    srv = serve()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 800})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # "/" focuses; a command query finds the section that holds it
        page.goto(base + "/chapter-3.html")
        check(page.evaluate("document.activeElement === document.body"), "nothing focused at load")
        page.keyboard.press("/")
        check(page.evaluate("document.activeElement.id") == "search-input", "'/' focuses the search box")
        check(page.input_value("#search-input") == "", "the '/' key is not typed into the box")
        page.keyboard.type("flink-check")
        page.wait_for_selector(".search-hit")
        want = {(e["p"], e["a"]) for e in INDEX if "flink-check" in e["t"] or "flink-check" in e["h"]}
        got = {tuple(r["href"].split("#")) if "#" in r["href"] else (r["href"], "top") for r in results(page)}
        check(want and got == want, f"'flink-check': results are exactly the {len(want)} sections that hold it")
        check(all(r["marks"] > 0 for r in results(page)), "every result highlights the match")
        first = results(page)[0]
        page.click(".search-hit >> nth=0")
        page.wait_for_url("**" + first["href"])
        check(page.url.endswith("/" + first["href"]), f"click navigates to {first['href']}")
        anchor = first["href"].split("#")[1]
        check(page.evaluate("id => !!document.getElementById(id)", anchor), "the anchor exists on the opened page")
        check(page.is_hidden(".search-results") or page.evaluate("document.querySelector('.search-results').hidden"),
              "results close after navigating")

        # a phrase query: every result holds all the words, the exact phrase ranks first, Enter opens it
        page.goto(base + "/chapter-1.html")
        page.keyboard.press("/")
        page.keyboard.type("Full demo reset")
        page.wait_for_selector(".search-hit")
        res = results(page)
        phrase = [e for e in INDEX if "full demo reset" in e["t"].lower() or "full demo reset" in e["h"].lower()]
        check(bool(res) and len(res) == min(50, len([e for e in INDEX if all(w in (e["h"] + e["c"] + e["g"] + e["t"]).lower() for w in ("full", "demo", "reset"))])),
              f"'Full demo reset': {len(res)} results, all words required")
        check(res[0]["href"] in {f"{e['p']}#{e['a']}" for e in phrase}, f"'Full demo reset': the first result holds the exact phrase ({res[0]['head']!r})")
        page.keyboard.press("Enter")
        page.wait_for_url("**" + res[0]["href"])
        check(page.url.endswith("/" + res[0]["href"]), "Enter opens the selected result")

        # heading matches rank above body-only matches
        page.goto(base + "/chapter-1.html")
        page.keyboard.press("/")
        page.keyboard.type("reset")
        page.wait_for_selector(".search-hit")
        flags = [("reset" in r["head"].lower()) for r in results(page)]
        check(any(flags) and not any(b and not a for a, b in zip(flags, flags[1:])),
              "'reset': every result with the word in its heading comes before the body-only results")

        # arrows move the selection
        page.goto(base + "/chapter-1.html")
        page.keyboard.press("/")
        page.keyboard.type("stock")
        page.wait_for_selector(".search-hit")
        page.keyboard.press("ArrowDown")
        sel = page.eval_on_selector_all(".search-hit", "els => els.findIndex(e => e.getAttribute('aria-selected') === 'true')")
        check(sel == 1, "ArrowDown selects the second result")

        # AND, case, accents
        def count(q):
            page.fill("#search-input", q)
            page.wait_for_timeout(100)
            return len(results(page))
        n_one = count("stock")
        n_two = count("stock FLINK")
        n_exp = len([e for e in INDEX if "stock" in (e["h"] + e["c"] + e["g"] + e["t"]).lower() and "flink" in (e["h"] + e["c"] + e["g"] + e["t"]).lower()])
        check(0 < n_two <= n_one and n_two == min(n_exp, 50), f"two words are AND-ed ({n_one} for 'stock', {n_two} for 'stock FLINK', expected {min(n_exp, 50)})")
        check(count("PREFLIGHT") == count("preflight") > 0, "case-insensitive")
        check(count("preflïght") == count("preflight"), "accent-insensitive (ï matches i)")

        # no match
        page.fill("#search-input", "zzqxjv nothing")
        page.wait_for_selector(".search-msg")
        check("No results" in page.text_content(".search-msg") and not results(page), "no match shows a message")

        # Esc closes, second Esc clears
        page.keyboard.press("Escape")
        check(page.evaluate("document.querySelector('.search-results').hidden"), "Esc closes the results")

        # no stack values in the page's search UI or index fetch: the index is the same file for every reader
        check(not any(("localStorage" in open(DOCS / "assets" / "search.js", encoding="utf-8").read(),)), "search.js does not read local storage (no stack values)")

        # the one-page guide scrolls to the section on the same page
        page.goto(base + "/workshop.html")
        page.keyboard.press("/")
        page.keyboard.type("Full demo reset")
        page.wait_for_selector(".search-hit")
        target = results(page)[0]["href"].split("#")[1]
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        check(page.url.endswith("#" + target) and "workshop.html" in page.url, "one-page guide: the result scrolls to the section on the same page")

        # typing '/' inside another field is not hijacked
        page.goto(base + "/setup.html")
        page.evaluate("document.getElementById('connect').open = true")
        page.fill("#connect-json", "a")
        page.press("#connect-json", "/")
        check(page.input_value("#connect-json") == "a/", "'/' typed in the Connect box stays there")
        check(page.evaluate("document.activeElement.id") == "connect-json", "'/' in the Connect box does not move focus")
        ctx.close()

        # mobile width: icon button opens a full-width box, results fit the screen
        for scheme in ("light", "dark"):
            m = browser.new_context(viewport={"width": 390, "height": 800}, color_scheme=scheme)
            mp = m.new_page()
            mp.on("console", lambda msg: errors.append(msg.text) if msg.type in ("error", "warning") else None)
            mp.goto(base + "/chapter-2.html")
            check(mp.is_hidden("#search-input"), f"[{scheme}] mobile: the box is collapsed until the icon is tapped")
            mp.click(".search-toggle")
            mp.keyboard.type("flink-check")
            mp.wait_for_selector(".search-hit")
            box = mp.eval_on_selector(".search-results", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right]; }")
            check(box[0] >= 0 and box[1] <= 390, f"[{scheme}] mobile: results fit the screen width")
            check(mp.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"[{scheme}] mobile: no horizontal scroll")
            mp.click(".search-hit >> nth=0")
            mp.wait_for_url("**#*")
            check("#" in mp.url, f"[{scheme}] mobile: tapping a result navigates")
            m.close()

        # index load failure is visible, not silent
        bad = browser.new_context(viewport={"width": 1400, "height": 800})
        bp = bad.new_page()
        bp.route("**/search-index.json*", lambda route: route.fulfill(status=404, body="nope"))
        bp.goto(base + "/chapter-1.html")
        bp.keyboard.press("/")
        bp.keyboard.type("stock")
        bp.wait_for_selector(".search-msg")
        check("unavailable" in bp.text_content(".search-msg"), "a missing index is reported in the results, not hidden")
        bad.close()
        browser.close()
    srv.shutdown()
    check(not errors, f"no console errors or warnings during the run ({errors[:2]})")
    if failures:
        sys.exit(f"test_search_ui: {len(failures)} check(s) failed: {failures}")
    print("test_search_ui: all checks passed")


if __name__ == "__main__":
    main()
