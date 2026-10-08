"""Build-level test of the search index (docs/assets/search-index.json) written by workshop/site/build.py.

Checks the index against the built pages: every step page and every heading with an id is covered, the text holds no
HTML tags, entries are well formed, code is searchable text, no connected-stack value is in it, the size is sane.
Run after the site is built:

    python3 overlay/workshop/site/tests/test_search_index.py
"""
import json
import pathlib
import re
import sys

DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"
INDEX = DOCS / "assets" / "search-index.json"
PAGES = ["intro", "setup"] + [f"chapter-{n}" for n in range(1, 7)] + ["troubleshooting", "teardown", "recap", "reference"]
TAG = re.compile(r"</?(?:a|b|i|p|br|hr|div|span|em|strong|code|pre|ul|ol|li|table|thead|tbody|tr|td|th|details|summary|aside|img|svg|button|input|h[1-6]|mark|kbd|nav|section|article|figure|figcaption|script|style)(?:\s[^>]*)?/?>")
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def main():
    if not INDEX.is_file():
        sys.exit(f"test_search_index: {INDEX} is missing; build the site first (workshop/site/build.py)")
    raw = INDEX.read_bytes()
    entries = json.loads(raw)
    check(isinstance(entries, list) and len(entries) > 50, f"index is a list with plenty of entries ({len(entries)})")
    check(len(raw) < 600 * 1024, f"index size is reasonable ({len(raw) // 1024} KiB, limit 600)")
    check(all(set(e) == {"p", "a", "h", "c", "g", "t"} for e in entries), "every entry has exactly p, a, h, c, g, t")
    check(all(e["h"].strip() and e["c"].strip() for e in entries), "every entry has a heading and a chapter label")
    check(len({(e["p"], e["a"]) for e in entries}) == len(entries), "no duplicate page + anchor")
    check(not [e for e in entries if TAG.search(e["t"]) or TAG.search(e["h"])], "no HTML tags in heading or text")
    check(not [e for e in entries if "\n" in e["t"] or "  " in e["t"]], "text is whitespace-collapsed")
    check(not [e for e in entries if re.search(r"(?<![\w-])#$", e["h"])], "headings carry no permalink '#'")

    by_page = {}
    for e in entries:
        by_page.setdefault(e["p"], []).append(e)
    for page in PAGES:
        f = page + ".html"
        check(f in by_page, f"{f} is in the index")
        html = (DOCS / f).read_text(encoding="utf-8")
        article = html.split('<article id="top"', 1)[1].split("</article>", 1)[0]
        ids = re.findall(r'<h[1-6] id="([^"]+)"', article)
        have = {e["a"] for e in by_page.get(f, [])}
        missing = [i for i in ids if i not in have]
        check(not missing, f"{f}: all {len(ids)} headings with an id are indexed" + (f" (missing {missing[:3]})" if missing else ""))
        check(all(f"id=\"{e['a']}\"" in html for e in by_page.get(f, []) if e["a"] != "top") and 'id="top"' in html,
              f"{f}: every indexed anchor exists on the page")
    check(set(by_page) <= {p + ".html" for p in PAGES}, "no entries for pages outside the step pages")

    blob = " ".join(e["t"] for e in entries)
    check("flink-check" in blob, "code is indexed as text (flink-check)")
    check(any("Full demo reset" in e["h"] or "Full demo reset" in e["t"] for e in entries), "'Full demo reset' is in the index")
    leak = re.findall(r"https?://[^\s\"']*(?:/dashboard/[a-z0-9]{3}-[a-z0-9]{3}-[a-z0-9]{3}|confluent\.cloud/environments/|\.elb\.amazonaws\.com)[^\s\"']*", blob)
    check(not leak, f"no dashboard ids, console paths or load balancer hosts in the index ({leak[:2]})")
    check(not re.search(r"\b(?:lkc|env|lsrc)-[a-z0-9]{5,}\b", blob), "no Confluent resource ids in the index")
    for page in PAGES:
        html = (DOCS / (page + ".html")).read_text(encoding="utf-8")
        check('data-index="assets/search-index.json?v=' in html and 'src="assets/search.js?v=' in html, f"{page}.html: search box and script are wired with cache-busting")
    wh = (DOCS / "workshop.html").read_text(encoding="utf-8")
    check('data-index="assets/search-index.json?v=' in wh, "workshop.html (one-page guide) has the search box")

    if failures:
        sys.exit(f"test_search_index: {len(failures)} check(s) failed: {failures}")
    print("test_search_index: all checks passed")


if __name__ == "__main__":
    main()
