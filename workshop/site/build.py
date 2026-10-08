#!/usr/bin/env python3
"""Build the static workshop site: a landing page and the guide rendered from workshop/README.md.

The Markdown guide is the only source of the guide. This script renders it to docs/workshop.html (the one-page guide),
fills the landing page template (workshop/site/landing.html) into docs/index.html, and copies the
images and assets (served by GitHub Pages from the "main /docs" folder). Both pages are checked:
every image must exist and every in-site link must resolve.

Code blocks: a Copy button appears on `sh`/`bash`/`shell`/`sql`/`yaml`/`json` blocks (things to run or paste) and not on
`text`/other blocks (output). A word after the language wins: ```` ```sql copy ```` forces the button on, ```` ```yaml nocopy ````
(or ```` ```text copy ````) turns it off/on for that block. Use `nocopy` on examples the reader should only read.

The same parsed guide is also cut into one page per Chapter (intro.html, setup.html, chapter-1.html to chapter-6.html,
troubleshooting.html, teardown.html, recap.html, reference.html). A link to an anchor on another page is pointed at that
page, and the build fails when an anchor is on no page.

Run from the repository root (the directory that holds workshop/ and docs/):

    uv run --with markdown-it-py --with mdit-py-plugins --with pygments --with playwright \
        python workshop/site/build.py

Playwright is needed only when a Mermaid diagram changed: diagrams are rendered once to SVG
and cached in workshop/site/mermaid/ (commit the cache). Use --strict to fail on missing images.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import posixpath
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

SITE = Path(__file__).resolve().parent
WORKSHOP = SITE.parent
ROOT = WORKSHOP.parent  # the public repository root
SRC = WORKSHOP / "README.md"
OUT = ROOT / "docs"
MERMAID_CACHE = SITE / "mermaid"
REPO_URL = "https://github.com/gianlucanatali/observable-inventory-streaming"
REPO_LABEL = "github.com/gianlucanatali/observable-inventory-streaming"
GUIDE_PAGE = "workshop.html"
SEARCH_INDEX = "search-index.json"  # written to docs/assets by write_search_index(); search.js fetches it
# Real markup left in index text (the guide's own "<this>" placeholders are text and fine).
HTML_TAG_RE = r"</?(?:a|b|i|p|br|hr|div|span|em|strong|code|pre|ul|ol|li|table|thead|tbody|tr|td|th|details|summary|aside|img|svg|button|input|h[1-6]|mark|kbd|nav|section|article|figure|figcaption|script|style)(?:\s[^>]*)?/?>"
SEARCH_HASH = ""  # content hash of the index, set once it is built, appended to its URL by bust_cache()
GO_PAGE = "go.html"  # stable redirect links for slides and bookmarks (go.js); a static page, copied as is
LANDING_PAGE = "index.html"
ASSETS = ("site.css", "stack.js", "site.js", "go.js", "fonts.css", "landing.css", "landing.js", "landing-noscript.css", "search.js")
BLOB_URL = f"{REPO_URL}/blob/main/"
SRC_REPO_PATH = "workshop/README.md"

MERMAID_VERSION = "11.4.1"  # build time only; the site ships the rendered SVG
MERMAID_CONFIG = {
    "startOnLoad": False,
    "theme": "base",
    "securityLevel": "strict",
    "fontFamily": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    "themeVariables": {
        "primaryColor": "#F3EDFB",
        "primaryBorderColor": "#632CA6",
        "primaryTextColor": "#1d1a24",
        "lineColor": "#7B4FB8",
        "secondaryColor": "#FFFFFF",
        "tertiaryColor": "#FAF8FD",
        "clusterBkg": "#FAF8FD",
        "clusterBorder": "#C9B6E4",
        "edgeLabelBackground": "#FFFFFF",
        "fontSize": "15px",
    },
    "flowchart": {"curve": "basis", "padding": 12},
}

ALERTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "WARNING": "Warning", "CAUTION": "Caution"}
COPY_LANGS = {"sh", "bash", "shell", "sql", "yaml", "json"}
CODE_LABELS = {"sh": "Terminal", "bash": "Terminal", "shell": "Terminal", "text": "Expected output", "json": "JSON"}


# The guide marks where the path chooser goes with this comment (GitHub hides it). Steps that differ are wrapped in
# <div data-path="panel"> or <div data-path="terminal"> blocks; site.js shows only the chosen path.
PATH_MARKER = "<!-- path-chooser -->"
PATH_CHOOSER = """<div class="path-chooser" role="group" aria-labelledby="path-chooser-title">
<p class="path-chooser-title" id="path-chooser-title">Choose your path</p>
<div class="path-cards">
<button type="button" class="path-card" data-set-path="panel" aria-pressed="true">
<span class="path-card-icon path-card-icon-panel" aria-hidden="true"></span>
<span class="path-card-title">Control panel</span>
<span class="path-card-text">Guided: click buttons in a web page. Easier if you rarely use a terminal.</span>
<span class="path-card-state" aria-hidden="true">Selected</span>
</button>
<button type="button" class="path-card" data-set-path="terminal" aria-pressed="false">
<span class="path-card-icon path-card-icon-terminal" aria-hidden="true"></span>
<span class="path-card-title">Terminal</span>
<span class="path-card-text"><code>make</code> commands, for people at home in a shell.</span>
<span class="path-card-state" aria-hidden="true">Selected</span>
</button>
</div>
<p class="path-chooser-note">The guide then shows only the steps for your path. The <b>Path</b> switch at the top of the page changes it later.</p>
</div>"""


# A guide link written as [stock dashboard](#3-open-the-datadog-stock-dashboard "stack-link:stock-dashboard") is
# an "open the X" reference. On GitHub it is an ordinary link to the step that explains X. On the site, site.js
# points it to the reader's own resource once they pasted their stack JSON (see CONNECT_BOX), else to the box.
STACK_LINK_PREFIX = "stack-link:"
STACK_LINK_KEYS = ("shop", "shop-home", "control", "overview-dashboard", "stock-dashboard", "online-dashboard", "apm", "dsm", "llm-obs",
                   "cost-dashboard", "confluent", "stream-lineage", "topic-inventory-cdc",
                   "topic-stock-sellable", "control-center", "ecs", "shop-p0048", "shop-p0092", "synthetics", "monitors", "rum")
CONNECT_MARKER = "<!-- connect-box -->"
CONNECT_BOX = """<div class="connect-box" id="connect-box">
<p class="connect-title">Connect your stack</p>
<p class="connect-lead">Paste the output of <code>make links-json</code> (see <a href="setup.html#024-connect-this-guide-to-your-stack-recommended">0.2.4</a>).</p>
<label class="connect-label" for="connect-json">Stack JSON</label>
<textarea id="connect-json" rows="6" spellcheck="false" autocomplete="off" placeholder='{"version":1,"stack":"hybrid","env":"dd-demo-hybrid", ...}'></textarea>
<div class="connect-buttons">
<button type="button" class="connect-go">Connect</button>
<button type="button" class="connect-forget" hidden>Forget my stack</button>
</div>
<p class="connect-status" role="status" aria-live="polite"></p>
<p class="connect-note">The JSON holds your addresses and ids, no passwords. It stays in this browser (local storage) and is never sent anywhere. The guide uses it to fill in placeholders and to link to your pages.</p>
</div>"""


def die(msg: str) -> None:
    print(f"build.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def github_slug(text: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, each space becomes a hyphen."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


# ---------------------------------------------------------------- source pre-pass

def drop_missing_images(md: str, strict: bool) -> tuple[str, list[str]]:
    """Remove image lines whose file does not exist yet.

    A footnote attached to an image line is a screenshot capture note for the guide's authors,
    not reader content, so it is left out of the site in every case.
    """
    missing: list[str] = []
    out_lines = []
    dropped_refs: set[str] = set()
    for line in md.splitlines():
        m = re.fullmatch(r"(!\[[^\]]*\]\((img/[^)\s]+)\))((?:\[\^[^\]]+\])*)\s*", line)
        if m:
            dropped_refs.update(re.findall(r"\[\^([^\]]+)\]", m.group(3)))
            if not (WORKSHOP / m.group(2)).is_file():
                missing.append(m.group(2))
                continue
            line = m.group(1)
        out_lines.append(line)
    if missing and strict:
        die(f"images referenced by {SRC} but missing in {WORKSHOP / 'img'}: {', '.join(missing)}")
    kept = [l for l in out_lines if not any(l.startswith(f"[^{r}]:") for r in dropped_refs)]
    return "\n".join(kept) + "\n", missing


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG header (the guide's screenshots are all PNG)."""
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        die(f"{path} is not a PNG; add a size reader for its format to build.py")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


# ---------------------------------------------------------------- mermaid

def mermaid_key(code: str) -> str:
    h = hashlib.sha256()
    h.update(MERMAID_VERSION.encode())
    h.update(repr(sorted(MERMAID_CONFIG.items())).encode())
    h.update(code.encode())
    return h.hexdigest()[:16]


def render_mermaid(diagrams: dict[str, str]) -> None:
    """Render diagrams missing from the cache to SVG with headless Chromium."""
    todo = {k: c for k, c in diagrams.items() if not (MERMAID_CACHE / f"{k}.svg").is_file()}
    if not todo:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die(f"{len(todo)} Mermaid diagram(s) changed and need rendering: add '--with playwright' to the uv command "
            "(and run 'uv run --with playwright playwright install chromium' once)")
    import json

    MERMAID_CACHE.mkdir(parents=True, exist_ok=True)
    url = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(url=url)
        page.evaluate(f"mermaid.initialize({json.dumps(MERMAID_CONFIG)})")
        for key, code in todo.items():
            svg = page.evaluate("async ([id, code]) => (await mermaid.render(id, code)).svg", [f"mmd-{key}", code])
            if "<svg" not in svg:
                die(f"Mermaid render of diagram {key} returned no SVG")
            (MERMAID_CACHE / f"{key}.svg").write_text(svg, encoding="utf-8")
            print(f"build.py: rendered Mermaid diagram {key}")
        browser.close()


# ---------------------------------------------------------------- markdown-it rules

def core_alerts(state) -> None:
    """GitHub alerts (> [!WARNING]) and '> **Note:**' quotes become admonitions."""
    toks = state.tokens
    for i, tok in enumerate(toks):
        if tok.type != "blockquote_open" or i + 2 >= len(toks) or toks[i + 2].type != "inline":
            continue
        inline = toks[i + 2]
        kids = inline.children or []
        kind = None
        if kids and kids[0].type == "text":
            m = re.match(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", kids[0].content)
            if m:
                kind = m.group(1)
                drop = 2 if len(kids) > 1 and kids[1].type in ("softbreak", "hardbreak") else 1
                inline.children = kids[drop:]
        if not kind and len(kids) >= 4 and kids[0].type == "strong_open" and kids[1].content.strip() in ("Note:", "Tip:", "Warning:", "Important:"):
            kind = kids[1].content.strip().rstrip(":").upper()
            rest = kids[3:]
            if rest and rest[0].type == "text":
                rest[0].content = rest[0].content.lstrip()
                if rest[0].content:
                    rest[0].content = rest[0].content[0].upper() + rest[0].content[1:]
            inline.children = rest
        if kind:
            tok.meta["alert"] = kind


def is_image_par(toks, i) -> bool:
    if toks[i].type != "paragraph_open" or i + 1 >= len(toks):
        return False
    kids = [k for k in (toks[i + 1].children or []) if not (k.type == "text" and not k.content.strip())]
    return len(kids) == 1 and kids[0].type == "image"


def is_caption_par(toks, i) -> bool:
    if i >= len(toks) or toks[i].type != "paragraph_open":
        return False
    kids = toks[i + 1].children or []
    return (len(kids) >= 3 and kids[0].type == "em_open" and kids[-1].type == "em_close"
            and kids[1].type == "text" and kids[1].content.startswith("Figure"))


def core_figures(state) -> None:
    toks = state.tokens
    for i in range(len(toks)):
        if is_image_par(toks, i):
            toks[i].meta["figure"] = True
            img = next(k for k in toks[i + 1].children if k.type == "image")
            alt = "".join(c.content for c in (img.children or []))
            if is_caption_par(toks, i + 3):
                toks[i + 2].meta["figure_close"] = "open"
                toks[i + 3].meta["caption"] = True
                kids = toks[i + 4].children
                toks[i + 4].children = kids[1:-1]  # drop the emphasis, keep the text
            else:
                toks[i + 2].meta["figure_close"] = alt


def add_rules(md: MarkdownIt, diagrams: dict[str, str], links_seen: list[str], stack_links: list[str]) -> None:
    md.core.ruler.push("alerts", core_alerts)
    md.core.ruler.push("figures", core_figures)
    default_render = md.renderer.renderToken

    def blockquote_open(self, tokens, idx, options, env):
        kind = tokens[idx].meta.get("alert")
        if kind:
            # find the matching close and mark it
            depth = 0
            for j in range(idx, len(tokens)):
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        tokens[j].meta["alert"] = kind
                        break
            k = kind.lower()
            return (f'<aside class="admonition admonition-{k}" role="note"><p class="admonition-title">'
                    f'<span class="admonition-icon" aria-hidden="true"></span>{ALERTS[kind]}</p>\n')
        return default_render(tokens, idx, options, env)

    def blockquote_close(self, tokens, idx, options, env):
        if tokens[idx].meta.get("alert"):
            return "</aside>\n"
        return default_render(tokens, idx, options, env)

    def paragraph_open(self, tokens, idx, options, env):
        if tokens[idx].meta.get("figure"):
            return '<figure class="figure">'
        if tokens[idx].meta.get("caption"):
            return "<figcaption>"
        return default_render(tokens, idx, options, env)

    def paragraph_close(self, tokens, idx, options, env):
        fc = tokens[idx].meta.get("figure_close")
        if fc == "open":
            return "\n"
        if fc is not None:
            cap = f"<figcaption>{html.escape(fc)}</figcaption>" if fc else ""
            return f"{cap}</figure>\n"
        if tokens[idx - 2].meta.get("caption"):
            return "</figcaption></figure>\n"
        return default_render(tokens, idx, options, env)

    def image(self, tokens, idx, options, env):
        tok = tokens[idx]
        src = tok.attrGet("src") or ""
        alt = html.escape("".join(c.content for c in (tok.children or [])), quote=True)
        size = ""
        if src.startswith("img/"):
            w, h = png_size(WORKSHOP / src)
            size = f' width="{w}" height="{h}"'  # reserves space, so deep links land correctly
        return (f'<button type="button" class="zoom" aria-label="Enlarge image: {alt}">'
                f'<img src="{html.escape(src, quote=True)}" alt="{alt}"{size} loading="lazy" decoding="async"></button>')

    def link_open(self, tokens, idx, options, env):
        tok = tokens[idx]
        href = tok.attrGet("href") or ""
        title = tok.attrGet("title") or ""
        if title.startswith(STACK_LINK_PREFIX):
            key = title[len(STACK_LINK_PREFIX):]
            if key not in STACK_LINK_KEYS:
                die(f"link {href!r} has unknown stack link {key!r}; known: {', '.join(STACK_LINK_KEYS)}")
            tok.attrs.pop("title")
            tok.attrSet("data-stack-link", key)
            stack_links.append(key)
        if href.startswith("#"):
            links_seen.append(href[1:])
        elif not re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
            path, _, frag = href.partition("#")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(SRC_REPO_PATH), path))
            if target.startswith(".."):
                die(f"relative link {href!r} leaves the repository")
            if not (ROOT / target).exists():
                die(f"relative link {href!r} points to {target}, which does not exist in {ROOT}")
            tok.attrSet("href", BLOB_URL + target + (f"#{frag}" if frag else ""))
        if (tok.attrGet("href") or "").startswith("http"):
            tok.attrSet("rel", "noopener")
            tok.attrSet("target", "_blank")
        return default_render(tokens, idx, options, env)

    def fence(self, tokens, idx, options, env):
        tok = tokens[idx]
        words = (tok.info or "").split()
        lang = words[0] if words else "text"
        flags = set(words[1:])
        if flags - {"copy", "nocopy"} or flags == {"copy", "nocopy"}:
            raise ValueError(f"build.py: bad code fence info {tok.info!r}: only one of 'copy' or 'nocopy' may follow the language")
        with_copy = ("copy" in flags) or (lang in COPY_LANGS and "nocopy" not in flags)
        code = tok.content
        if lang == "mermaid":
            key = mermaid_key(code)
            diagrams[key] = code
            return f'<figure class="diagram">@@MERMAID:{key}@@</figure>\n'
        try:
            lexer = get_lexer_by_name({"sh": "bash"}.get(lang, lang))
        except ClassNotFound:
            lexer = get_lexer_by_name("text")
        body = highlight(code, lexer, HtmlFormatter(nowrap=True))
        label = CODE_LABELS.get(lang, lang.upper())
        kind = "cmd" if lang in ("sh", "bash", "shell") else "out"
        # On the control panel path, commands outside a path block are the few that both paths need.
        label_html = html.escape(label) + ('<span class="code-both"> (both paths)</span>' if kind == "cmd" else "")
        # Copy button only where the reader pastes (see the module docstring); the fence flag overrides the default.
        button = (f'<button type="button" class="copy" aria-label="Copy {label.lower()} to clipboard">'
                  f'<span class="copy-icon" aria-hidden="true"></span><span class="copy-text">Copy</span></button>'
                  if with_copy else "")
        return (f'<div class="code code-{kind}" data-lang="{html.escape(lang)}">'
                f'<div class="code-bar"><span class="code-label">{label_html}</span>{button}</div>'
                f'<pre tabindex="0"><code>{body}</code></pre></div>\n')

    def table_open(self, tokens, idx, options, env):
        return '<div class="table-wrap" tabindex="0"><table>\n'

    def table_close(self, tokens, idx, options, env):
        return "</table></div>\n"

    def heading_open(self, tokens, idx, options, env):
        tok = tokens[idx]
        text = "".join(c.content for c in tokens[idx + 1].children or [] if c.type in ("text", "code_inline")).strip()
        if tok.tag == "h3" and re.match(r"Lab \w+", text):
            tok.attrJoin("class", "lab")
        if tok.tag == "h4" and text == "Checkpoint":
            tok.attrJoin("class", "checkpoint")
        return default_render(tokens, idx, options, env)

    for name, fn in [("blockquote_open", blockquote_open), ("blockquote_close", blockquote_close),
                     ("paragraph_open", paragraph_open), ("paragraph_close", paragraph_close),
                     ("image", image), ("link_open", link_open), ("fence", fence),
                     ("table_open", table_open), ("table_close", table_close),
                     ("heading_open", heading_open)]:
        md.add_render_rule(name, fn)


# ---------------------------------------------------------------- page

def build_toc(tokens) -> str:
    items: list[tuple[int, str, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            text = "".join(c.content for c in tokens[i + 1].children or [] if c.type in ("text", "code_inline")).strip()
            if text == "Contents":
                continue
            items.append((int(tok.tag[1]), tok.attrGet("id"), text))
    out = ['<ol class="toc-list">']
    open_sub = False
    for n, (level, anchor, text) in enumerate(items):
        if level == 2:
            if open_sub:
                out.append("</ol></li>")
                open_sub = False
            elif n:
                out.append("</li>")
            out.append(f'<li class="toc-h2"><a href="#{anchor}">{html.escape(text)}</a>')
        else:
            if not open_sub:
                out.append('<ol class="toc-sub">')
                open_sub = True
            out.append(f'<li class="toc-h3"><a href="#{anchor}">{html.escape(text)}</a></li>')
    out.append("</ol></li>" if open_sub else "</li>")
    out.append("</ol>")
    return "\n".join(out)



def external_links_in_new_tab(page: str) -> str:
    """Every link that leaves the site (http/https) opens in a new tab; in-site links stay in the same tab."""
    def fix(m: "re.Match[str]") -> str:
        tag = m.group(0)
        if "target=" in tag:
            return tag
        tag = tag[:-1] + ' target="_blank">'
        return tag if "rel=" in tag else tag[:-1] + ' rel="noopener">'
    return re.sub(r'<a\s[^>]*href="https?://[^"]*"[^>]*>', fix, page)


def bust_cache(page: str) -> str:
    """Append ?v=<content hash> to our own CSS/JS so browsers (also on file://) never keep a stale copy."""
    import hashlib
    for asset in ASSETS:
        src = SITE / asset
        if src.is_file():
            h = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
            page = page.replace(f'"assets/{asset}"', f'"assets/{asset}?v={h}"')
    if SEARCH_HASH:
        page = page.replace(f'"assets/{SEARCH_INDEX}"', f'"assets/{SEARCH_INDEX}?v={SEARCH_HASH}"')
    return page

def intro_facts(md_text: str) -> dict[str, str]:
    """Time and cost for the landing page's call to action, taken from the guide's first table
    (Time | Cost | You need): the text before the first ':' or ',' of each cell, first letter lower-cased."""
    rows: list[list[str]] = []
    for line in md_text.splitlines():
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif rows:
            break
    if len(rows) != 3 or not re.fullmatch(r"-+", rows[1][0]) or len(rows[0]) != len(rows[2]):
        die(f"expected the first table of {SRC} to be one header row and one data row (Time | Cost | You need)")
    heads = [h.split()[0].lower() for h in rows[0]]
    if heads[:2] != ["time", "cost"]:
        die(f"expected the first table of {SRC} to start with Time and Cost columns, found {rows[0][:2]}")
    facts = {}
    for key, cell in zip(("time", "cost"), rows[2]):
        short = re.split(r"[:,]", cell, maxsplit=1)[0].strip()
        if not short:
            die(f"empty {key} cell in the first table of {SRC}")
        facts[key] = html.escape(short[0].lower() + short[1:] if key == "cost" else short)
    return facts


def check_landing(page: str, guide_ids: set[str], page_ids_by_file: dict[str, set[str]]) -> list[str]:
    """Fail on a landing link or image that does not resolve; return the guide images it uses."""
    own_ids = set(re.findall(r'\sid="([^"]+)"', page))
    problems = []
    if "{{" in page:
        problems.append("unfilled placeholder " + re.search(r"\{\{[^}]*\}\}", page).group(0))
    for ref in re.findall(r'\s(?:href|src)="([^"]+)"', page):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", ref, re.I):
            continue
        path, _, frag = ref.partition("#")
        if path in ("", LANDING_PAGE):
            if frag and frag != "menu" and frag not in own_ids:
                problems.append(f"{ref}: no element with id {frag!r} on the landing page")
        elif path == GUIDE_PAGE:
            if frag and frag not in guide_ids:
                problems.append(f"{ref}: no heading with id {frag!r} in the guide")
        elif path in PAGE_FILES:
            if frag and frag not in page_ids_by_file[path]:
                problems.append(f"{ref}: no element with id {frag!r} on {path}")
        elif path.startswith("img/"):
            if not (WORKSHOP / path).is_file():
                problems.append(f"{ref}: image missing in {WORKSHOP / 'img'}")
        elif path.startswith("images/"):
            if not (SITE / path).is_file():
                problems.append(f"{ref}: photo missing in {SITE / 'images'}")
        elif path.startswith("assets/vendor/"):
            if not (SITE / path.removeprefix("assets/")).is_file():
                problems.append(f"{ref}: missing in {SITE / 'vendor'}")
        elif path.startswith("assets/"):
            if path.removeprefix("assets/") not in ASSETS:
                problems.append(f"{ref}: not one of the assets build.py copies ({', '.join(ASSETS)})")
        else:
            problems.append(f"{ref}: unexpected relative link on the landing page")
    css = (SITE / "landing.css").read_text(encoding="utf-8")
    for ref in re.findall(r'url\("\.\./(images/[^"]+)"\)', css):
        if not (SITE / ref).is_file():
            problems.append(f"landing.css {ref}: photo missing in {SITE / 'images'}")
    if problems:
        die("landing page " + str(SITE / "landing.html") + ":\n  " + "\n  ".join(problems))
    return sorted(set(re.findall(r'src="(img/[^"]+)"', page)))


# ---------------------------------------------------------------- chapter pages

# One page per Chapter, cut from the same parsed README as the one-page guide. "labs" are the lab headings (by id) that a
# Chapter shows, in order. The first lab's opening paragraph, and the "Needs:" paragraph after it, become the
# "What you will see" box. Chapter 5 shows Lab 5.1 (offers) first, then Lab 5.2 (restock) as "also in this chapter".
CHAPTERS = [
    dict(n=1, title="One honest number",
         labs=["lab-11-one-product-five-stores-one-online-number", "lab-12-optional-look-inside-confluent"]),
    dict(n=2, title="Unknown is not zero", labs=["lab-21-unknown-is-not-zero"]),
    dict(n=3, title="The incident", labs=["lab-31-the-incident"]),
    dict(n=4, title="Canary the fix", labs=["lab-41-canary-the-fix"]),
    dict(n=5, title="The AI offer",
         labs=["lab-51-offers-with-a-safe-default", "lab-52-restock-that-learns"],
         also={"lab-52-restock-that-learns": "also-lab-5-2"}),
    dict(n=6, title="Datadog on top", labs=["lab-61-datadog-on-top-of-the-solution"]),
]
TOTAL = len(CHAPTERS)
# Pages in reading order: (file, key, short name). The progress bar shows the first eight.
PAGE_LIST = ([("intro.html", "intro", "Intro"), ("setup.html", "setup", "Setup")]
             + [(f"chapter-{c['n']}.html", c["n"], c["title"]) for c in CHAPTERS]
             + [("troubleshooting.html", "troubleshooting", "Troubleshooting"), ("teardown.html", "teardown", "Teardown"),
                ("recap.html", "recap", "Recap"), ("reference.html", "reference", "Reference")])
PAGE_FILES = tuple(f for f, _, _ in PAGE_LIST)
INTRO_PAGE = PAGE_FILES[0]
SETUP_PAGE = PAGE_FILES[1]
CONNECT_HREF = f"{SETUP_PAGE}#connect-box"
CONNECT_DETAILS = ('<details class="connect-details" id="connect">\n<summary>Connect your stack <span class="connect-state">recommended</span></summary>\n'
                   + CONNECT_BOX + "\n</details>\n")


def read_boxes(md_text: str) -> dict[str, str]:
    """Texts the Chapter pages show but the guide hides, written in the README as an HTML comment:
    <!-- box: name  (next lines: Markdown)  -->  Returns {name: Markdown}."""
    boxes = {m.group(1): m.group(2).strip() for m in re.finditer(r"<!-- box: ([a-z0-9-]+)\n(.*?)\n-->", md_text, re.S)}
    for need in ("setup-do", "also-lab-5-2"):
        if need not in boxes:
            die(f"{SRC}: the box '{need}' is missing; write it as <!-- box: {need} ... --> in the README")
    return boxes


def heading_idx(tokens, hid: str) -> int:
    for i, t in enumerate(tokens):
        if t.type == "heading_open" and t.level == 0 and t.attrGet("id") == hid:
            return i
    die(f"heading #{hid} not found in {SRC}")


def section(tokens, hid: str, until: str | None = None) -> list:
    """Tokens from heading #hid up to the next heading of the same or a higher level (or to heading #until)."""
    start = heading_idx(tokens, hid)
    depth = int(tokens[start].tag[1])
    end = heading_idx(tokens, until) if until else len(tokens)
    for i in range(start + 1, end):
        t = tokens[i]
        if t.type == "heading_open" and t.level == 0 and int(t.tag[1]) <= depth:
            end = i
            break
    toks = tokens[start:end]
    while toks and toks[-1].type == "hr":
        toks = toks[:-1]
    return toks


def html_token(content: str) -> Token:
    t = Token("html_block", "", 0)
    t.content = content
    return t


def cut_pages(tokens, boxes: dict[str, str]) -> dict[str, list]:
    """The token list of every page, by file name."""
    h1 = next(i for i, t in enumerate(tokens) if t.type == "heading_open" and t.tag == "h1")
    contents = heading_idx(tokens, "contents")
    why = heading_idx(tokens, "why-change-events")
    conv = next((i for i in range(contents, why) if tokens[i].type == "paragraph_open"
                 and tokens[i + 1].content.startswith("**Conventions.**")), None)
    if conv is None:
        die(f"{SRC}: the paragraph starting '**Conventions.**' was not found between 'Contents' and 'Why change events'")
    pages = {
        INTRO_PAGE: (tokens[h1:contents] + tokens[conv:why] + section(tokens, "why-change-events")
                     + section(tokens, "the-architecture-at-a-glance") + section(tokens, "labs", until="lab-11-one-product-five-stores-one-online-number")),
        SETUP_PAGE: section(tokens, "01-prerequisites") + section(tokens, "02-build-the-stack"),
        "troubleshooting.html": section(tokens, "troubleshooting"),
        "teardown.html": section(tokens, "teardown"),
        "recap.html": section(tokens, "recap-and-further-reading"),
        "reference.html": section(tokens, "reference"),
    }
    for c in CHAPTERS:
        toks = []
        for lab in c["labs"]:
            if c.get("also", {}).get(lab):
                toks = toks + [html_token(f'<div class="chapter-sub"><p>{html.escape(boxes[c["also"][lab]])}</p></div>\n')]
            toks = toks + section(tokens, lab)
        pages[f"chapter-{c['n']}.html"] = toks
    return pages


def split_see_box(toks: list, lab: str) -> tuple[list, list]:
    """Take the lab's opening paragraph out of the page body: it becomes the 'What you will see' box."""
    i = heading_idx(toks, lab)
    if toks[i + 3].type != "paragraph_open" or toks[i + 5].type != "paragraph_close":
        die(f"lab #{lab} does not open with one plain paragraph; the 'What you will see' box is taken from it")
    end = i + 6
    if not (toks[end].type == "paragraph_open" and toks[end + 1].content.startswith("**Needs:**")):
        die(f"lab #{lab}: the opening paragraph must be followed by a paragraph starting '**Needs:**'")
    return toks[i + 3:end + 3], toks[:i + 3] + toks[end + 3:]


def page_ids(body: str) -> set[str]:
    return set(re.findall(r'\sid="([^"]+)"', body))


def summary_boxes(body: str) -> tuple[str, int]:
    """A lab's closing paragraph '... **Next:** [Lab 2.1](...)' becomes a Summary box; Previous/Next replace the pointer."""
    pat = re.compile(r"<p>((?:(?!</p>).)*?)\s*<strong>Next:</strong>.*?</p>", re.S)
    return pat.subn(lambda m: f'<aside class="summary-box"><p class="see-title">Summary</p><p>{m.group(1)}</p></aside>', body)


def toc_from(tokens, ids_ok: set[str]) -> str:
    """Contents of one page: its two highest heading levels among h2 to h4."""
    heads = [(i, t) for i, t in enumerate(tokens) if t.type == "heading_open" and t.level == 0
             and t.tag in ("h2", "h3", "h4") and t.attrGet("id") in ids_ok]
    levels = sorted({t.tag for _, t in heads})[:2]
    items = []
    for i, t in heads:
        if t.tag in levels:
            text = "".join(c.content for c in tokens[i + 1].children or [] if c.type in ("text", "code_inline")).strip()
            items.append((levels.index(t.tag), t.attrGet("id"), text))
    out, sub = ['<ol class="toc-list">'], False
    for n, (lv, a, text) in enumerate(items):
        if lv == 0:
            if sub:
                out.append("</ol></li>")
                sub = False
            elif n:
                out.append("</li>")
            out.append(f'<li class="toc-h2"><a href="#{a}">{html.escape(text)}</a>')
        else:
            if not sub:
                out.append('<ol class="toc-sub">')
                sub = True
            out.append(f'<li class="toc-h3"><a href="#{a}">{html.escape(text)}</a></li>')
    out.append("</ol></li>" if sub else "</li>")
    out.append("</ol>")
    return "\n".join(out)


def current_attr(on: bool) -> str:
    return ' aria-current="page"' if on else ""


def progress_html(key) -> str:
    items = []
    for file, k, name in PAGE_LIST[:2 + TOTAL]:
        dot = "&middot;" if k == "intro" else ("0" if isinstance(k, str) else str(k))
        here = k == key
        extra = " cp-extra" if isinstance(k, str) else ""
        items.append(f'<li class="cp-step{extra}{" cp-current" if here else ""}"><a href="{file}"{current_attr(here)}>'
                     f'<span class="cp-dot" aria-hidden="true">{dot}</span><span class="cp-label">{html.escape(name)}</span></a></li>')
    return f'<nav class="chapter-progress" aria-label="Chapters"><ol>{"".join(items)}</ol></nav>'


def pager_html(key) -> str:
    keys = [k for _, k, _ in PAGE_LIST]
    i = keys.index(key)

    def label(j):
        f, k, name = PAGE_LIST[j]
        return f"Chapter {k}: {name}" if isinstance(k, int) else name

    def btn(kind, j, word):
        if j is None:
            return '<span class="pager-gap"></span>'
        return (f'<a class="pager-btn pager-{kind}" href="{PAGE_LIST[j][0]}"><span class="pager-dir">{word}</span>'
                f'<span class="pager-title">{html.escape(label(j))}</span></a>')
    prev = i - 1 if i > 0 else None
    nxt = i + 1 if i + 1 < len(PAGE_LIST) else None
    return f'<nav class="pager" aria-label="Previous and next">{btn("prev", prev, "Previous")}{btn("next", nxt, "Next")}</nav>'


def sidebar_html(key, toc: str) -> str:
    def li(file, k, name):
        n = f'<span class="cl-n">{k}</span>' if isinstance(k, int) else ('<span class="cl-n">0</span>' if k == "setup" else "")
        return f'<li><a href="{file}"{current_attr(k == key)}>{n}{html.escape(name)}</a></li>'
    main = "".join(li(*p) for p in PAGE_LIST[:2 + TOTAL])
    help_ = "".join(li(*p) for p in PAGE_LIST[2 + TOTAL:])
    return (f'<p class="toc-title">Workshop</p><ol class="chapter-list">{main}</ol>'
            f'<p class="toc-title">Help and reference</p><ol class="chapter-list chapter-list-help">{help_}'
            f'<li><a class="guide-link" href="{GUIDE_PAGE}">One-page guide</a></li></ol>'
            '<p class="toc-title toc-title-page">On this page</p>' + toc)


def retarget_links(body: str, name: str, mine: set[str], owner: dict[str, str], problems: list[str]) -> str:
    """An anchor on this page stays; an anchor on another page points to that page; one nowhere is a build error."""
    def fix(m):
        a = m.group(1)
        if a in mine or a == "top":
            return m.group(0)
        if a in owner:
            return f'href="{owner[a]}#{a}"'
        problems.append(f"{name}: link to #{a} matches no heading or element on any page")
        return m.group(0)
    return re.sub(r'href="#([^"]+)"', fix, body)


def page_shell(template: str, key, title: str, toc: str, content: str) -> str:
    p = (template.replace("{{title}}", html.escape(title)).replace("{{toc}}", sidebar_html(key, toc))
         .replace("{{source_url}}", BLOB_URL + SRC_REPO_PATH).replace("{{repo_url}}", REPO_URL)
         .replace("{{content}}", content))
    def swap(old: str, new: str) -> None:
        nonlocal p
        if old not in p:
            die(f"template.html changed: {old!r} not found, update chapter page_shell()")
        p = p.replace(old, new)
    swap('<p class="toc-title">Contents</p>\n    ', "")
    swap('<a class="brand" href="index.html" title="Home">', f'<a class="brand" href="{INTRO_PAGE}" title="Start of the workshop">')
    swap('<a class="topbar-current" href="#top" aria-current="page">Workshop</a>',
         f'<a class="topbar-current" href="{INTRO_PAGE}" aria-current="page">Workshop</a>'
         f'<a class="topbar-extra" href="{GUIDE_PAGE}">One-page guide</a>')
    swap('<p><a href="#top">Back to top</a></p>', f'<p><a href="#top">Back to top</a> &middot; <a href="{GUIDE_PAGE}">One-page guide</a></p>')
    if key != "setup":
        swap('<body>', f'<body data-connect-href="{CONNECT_HREF}">')
        swap('href="#connect-box" hidden', f'href="{CONNECT_HREF}" hidden')
    return p


def build_chapter_pages(md, env, tokens, diagrams, template: str, boxes: dict[str, str]) -> dict[str, str]:
    """Render every page; return {file: html}. Fails on a link whose anchor is on no page."""
    cut = cut_pages(tokens, boxes)
    see: dict[str, str] = {}
    page_tokens = {}
    for c in CHAPTERS:
        f = f"chapter-{c['n']}.html"
        box_toks, rest = split_see_box(cut[f], c["labs"][0])
        see[f] = md.renderer.render(box_toks, md.options, env)
        page_tokens[f] = rest
    for f, toks in cut.items():
        page_tokens.setdefault(f, toks)
    bodies = {}
    for f, toks in page_tokens.items():
        if any(t.type.startswith("footnote") for t in toks):
            die(f"{f}: the page contains footnotes; cutting the guide into pages would break them")
        body = md.renderer.render(toks, md.options, env)
        for key in diagrams:
            body = body.replace(f"@@MERMAID:{key}@@", (MERMAID_CACHE / f"{key}.svg").read_text(encoding="utf-8"))
        bodies[f] = body
    # the path chooser goes on the Intro, the Connect box on Setup
    if bodies[INTRO_PAGE].count(PATH_MARKER) != 1:
        die(f"{INTRO_PAGE}: expected one {PATH_MARKER} marker")
    cta = (f'<aside class="see-box start-cta"><p class="see-title">Two ways through this workshop</p><ul>'
           '<li><b>Step by step.</b> One page per Chapter: Setup, then six Chapters of one or two labs each, with a summary and a checkpoint. '
           'Choose your path below, then press Start.</li>'
           f'<li><b>Everything on one page.</b> The <a href="{GUIDE_PAGE}">one-page guide</a> holds the same content, handy to search or print.</li></ul>'
           f'<p class="see-actions"><a class="pager-btn pager-next" href="{SETUP_PAGE}"><span class="pager-dir">Start</span>'
           '<span class="pager-title">Setup: build the stack</span></a></p></aside>\n')
    bodies[INTRO_PAGE] = bodies[INTRO_PAGE].replace(PATH_MARKER, cta + PATH_CHOOSER)
    if bodies[SETUP_PAGE].count(CONNECT_MARKER) != 1:
        die(f"{SETUP_PAGE}: expected one {CONNECT_MARKER} marker")
    # The Connect box sits at the step that uses it (0.2.4, right after the stack is built), not at the top of the page.
    bodies[SETUP_PAGE] = bodies[SETUP_PAGE].replace(CONNECT_MARKER, CONNECT_DETAILS)
    setup_html = md.render(boxes["setup-do"], {})
    k = setup_html.rfind("<p>")
    setup_html = setup_html[:k] + '<p class="see-meta">' + setup_html[k + 3:]
    setup_head = ('<aside class="see-box"><p class="see-title">What you will do</p>' + setup_html + '</aside>\n')

    # ids per page; "connect-box" lives in the Setup page's details
    ids = {f: page_ids(b) | ({"connect-box", "connect"} if f == SETUP_PAGE else set()) | page_ids(see.get(f, "")) for f, b in bodies.items()}
    owner: dict[str, str] = {}
    for f in PAGE_FILES:
        for a in ids[f]:
            owner.setdefault(a, f)
    problems: list[str] = []
    out = {}
    for file, key, name in PAGE_LIST:
        body = bodies[file]
        head = ""
        if isinstance(key, int):
            c = CHAPTERS[key - 1]
            body, _ = summary_boxes(body)
            head = (f'<p class="chapter-kicker">Chapter {key} of {TOTAL}</p>\n<h1 class="chapter-title">{html.escape(c["title"])}</h1>\n'
                    f'<aside class="see-box"><p class="see-title">What you will see</p>{see[file]}</aside>\n')
            title = f"Chapter {key} of {TOTAL}: {c['title']}"
        elif key == "setup":
            head = '<p class="chapter-kicker">Getting started, before Chapter 1</p>\n<h1 class="chapter-title">Setup</h1>\n' + setup_head
            title = "Setup"
        elif key == "intro":
            title = "Observable inventory streaming: intro"
        else:
            head = f'<p class="chapter-kicker">{html.escape(name)}</p>\n'
            title = name
        body = retarget_links(head + body, file, ids[file], owner, problems)
        toc_ids = page_ids(body)
        toc = retarget_links(toc_from(page_tokens[file], toc_ids), file, ids[file], owner, problems)
        page = page_shell(template, key, f"{title} | Observable inventory streaming" if key != "intro" else title, toc,
                          progress_html(key) + "\n" + body + "\n" + pager_html(key))
        if "@@MERMAID" in page or "{{" in page:
            die(f"{file}: unfilled placeholder")
        out[file] = page
    if problems:
        die("cross-page links:\n  " + "\n  ".join(sorted(set(problems))))
    return out


def check_page_links(pages: dict[str, str], guide_ids: set[str]) -> int:
    """Every link between generated pages (and to the one-page guide) must point at an element that exists."""
    ids = {f: page_ids(p) for f, p in pages.items()}
    ids[GUIDE_PAGE] = guide_ids
    problems, n = [], 0
    for f, page in pages.items():
        for ref in re.findall(r'\shref="([^"]*)"', page):
            if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", ref, re.I):
                continue
            path, _, frag = ref.partition("#")
            target = path or f
            if target == LANDING_PAGE or target.startswith("assets/"):
                continue
            if target not in ids:
                problems.append(f"{f}: link {ref!r} goes to an unknown page")
                continue
            n += 1
            if frag and frag != "top" and frag not in ids[target]:
                problems.append(f"{f}: link {ref!r}: no element with id {frag!r} on {target}")
    if problems:
        die("page links:\n  " + "\n  ".join(sorted(set(problems))))
    return n


def check_step_stack_links(md_text: str) -> None:
    """Fail when a numbered step ('#### 3. Title') in the main flow tells the reader to open a Datadog page by a menu
    path ('Open Monitors > Manage Monitors', 'Open Synthetic Monitoring > Tests') or says 'In Datadog' but holds no
    stack-link. The menu path belongs in the step's closed <details> box, which is not scanned, next to the link."""
    step_re = re.compile(r"(?m)^#{2,4} ")
    heads = [m.start() for m in step_re.finditer(md_text)] + [len(md_text)]
    menu = re.compile(r"\b[Oo]pen [A-Z][A-Za-z ]*? > |\bIn Datadog\b|\bin Datadog, open\b")
    problems = []
    for a, b in zip(heads, heads[1:]):
        block = md_text[a:b]
        title = block.split("\n", 1)[0]
        if not re.match(r"#### \d+\. ", title):
            continue
        if STACK_LINK_PREFIX in block:
            continue
        main = re.sub(r"(?s)<details.*?</details>", "", block)
        main = re.sub(r"(?s)```.*?```", "", main)
        m = menu.search(main)
        if m:
            problems.append(f"{title.strip()!r}: mentions {m.group(0).strip()!r} but has no stack-link")
    if problems:
        die("steps that send the reader to a Datadog page without a stack link "
            '([text](#anchor "stack-link:<name>")):\n  ' + "\n  ".join(problems))


class _SectionCutter(HTMLParser):
    """Cut one built page into sections, one per heading that has an id. Collects the visible text of the article
    (code included); skips the permalink '#', copy buttons, diagrams, the Connect box and the progress and pager bars."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    SKIP_TAGS = {"script", "style", "svg", "textarea", "button", "dialog", "template"}
    SKIP_CLASSES = {"header-anchor", "chapter-progress", "pager", "connect-box", "code-bar"}
    BLOCKS = {"p", "li", "div", "pre", "tr", "td", "th", "br", "ul", "ol", "table", "aside", "details", "summary",
              "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "blockquote"}

    def __init__(self, page_title: str):
        super().__init__(convert_charrefs=True)
        self.sections: list[dict] = [{"id": "top", "level": 0, "heading": page_title, "parts": []}]
        self.stack: list[tuple[str, bool]] = []
        self.in_article = False
        self.heading: dict | None = None  # the section whose heading text is being read
        self.head_level = 0

    def skipping(self) -> bool:
        return any(skip for _, skip in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.VOID:
            if self.in_article and tag == "br":
                self.sections[-1]["parts"].append(" ")
            return
        classes = set((a.get("class") or "").split())
        skip = tag in self.SKIP_TAGS or bool(classes & self.SKIP_CLASSES)
        self.stack.append((tag, skip))
        if tag == "article" and a.get("id") == "top":
            self.in_article = True
            return
        if not self.in_article or self.skipping():
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and a.get("id"):
            sec = {"id": a["id"], "level": int(tag[1]), "heading": "", "parts": []}
            self.sections.append(sec)
            self.heading = sec
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and "chapter-title" in classes:
            self.heading = self.sections[0]
            self.sections[0]["heading"] = ""
        if tag in self.BLOCKS:
            self.sections[-1]["parts"].append(" ")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break
        else:
            die(f"search index: unbalanced </{tag}> while cutting a page into sections")
        if tag == "article":
            self.in_article = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = None
        if self.in_article and tag in self.BLOCKS:
            self.sections[-1]["parts"].append(" ")

    def handle_data(self, data):
        if not self.in_article or self.skipping():
            return
        if self.heading is not None:
            self.heading["heading"] += data
        else:
            self.sections[-1]["parts"].append(data)


def build_search_index(pages: dict[str, str]) -> list[dict]:
    """One entry per heading of every step page: page file, anchor, heading, chapter label, parent heading, plain text."""
    names = {f: (f"Chapter {k}: {n}" if isinstance(k, int) else n) for f, k, n in PAGE_LIST}
    entries: list[dict] = []
    for file in PAGE_FILES:
        if file not in pages:
            die(f"search index: page {file} was not built")
        cutter = _SectionCutter(names[file])
        cutter.feed(pages[file])
        cutter.close()
        if cutter.stack:
            die(f"search index: {file}: unclosed <{cutter.stack[-1][0]}> while cutting the page into sections")
        if len(cutter.sections) < 2:
            die(f"search index: {file}: found no heading with an id")
        parents: list[dict] = []
        for sec in cutter.sections:
            heading = re.sub(r"\s+", " ", sec["heading"]).strip() or names[file]
            text = re.sub(r"\s+", " ", "".join(sec["parts"])).strip()
            if sec["level"]:
                while parents and parents[-1]["level"] >= sec["level"]:
                    parents.pop()
            parent = parents[-1]["heading"] if parents else ""
            if sec["level"]:
                parents.append({"level": sec["level"], "heading": heading})
            if not text and not sec["level"]:
                continue  # the lead of a page that starts straight with a heading
            entries.append({"p": file, "a": sec["id"], "h": heading, "c": names[file], "g": parent, "t": text})
    if not entries:
        die("search index: no entries")
    return entries


def write_search_index(entries: list[dict]) -> None:
    """Write docs/assets/search-index.json and record its hash for bust_cache(). Fails on empty or duplicate entries."""
    global SEARCH_HASH
    seen = set()
    for e in entries:
        key = (e["p"], e["a"])
        if key in seen:
            die(f"search index: duplicate anchor {e['a']!r} on {e['p']}")
        seen.add(key)
        if not e["h"]:
            die(f"search index: empty heading for {e['a']!r} on {e['p']}")
        if re.search(HTML_TAG_RE, e["t"] + " " + e["h"]):
            die(f"search index: HTML left in the text of {e['a']!r} on {e['p']}")
    data = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    SEARCH_HASH = hashlib.sha256(data).hexdigest()[:10]
    (OUT / "assets" / SEARCH_INDEX).write_bytes(data)
    print(f"build.py: wrote {OUT / 'assets' / SEARCH_INDEX} ({len(entries)} entries, {len(data) // 1024} KiB)")


def check_unrendered_markdown(pages: dict[str, str]) -> None:
    """Fail if the visible text of a page still holds raw markdown (an HTML block swallowed it)."""
    patterns = [
        (re.compile(r"\]\(#"), "link syntax ']('"),
        (re.compile(r'"stack-link:'), "stack-link title"),
        (re.compile(r"\*\*[^\s*][^*\n]*?\*\*"), "**bold** markers"),
        (re.compile(r"(?m)^\s*\d+\. +(?:\S.*)?[\[`*]"), "numbered list line with markdown"),
    ]
    problems = []
    for name, page in pages.items():
        text = re.sub(r"(?is)<(pre|code|script|style|svg)\b.*?</\1>", " ", page)
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"<[^>]*>", "", text)  # visible text only; attributes go with their tags
        for rx, what in patterns:
            for m in rx.finditer(text):
                line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())].strip()
                problems.append(f"{name}: unrendered markdown ({what}): {line[:140]!r}")
    if problems:
        die("unrendered markdown in the built pages (an HTML block such as <details> inside a list "
            "needs a blank line after its closing tag):\n  " + "\n  ".join(problems[:20]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="fail if the guide references a missing image")
    args = ap.parse_args()

    if not SRC.is_file():
        die(f"source guide not found at {SRC}")
    md_text, missing = drop_missing_images(SRC.read_text(encoding="utf-8"), args.strict)
    for m in missing:
        print(f"build.py: WARNING: {m} does not exist yet; left out of the site", file=sys.stderr)

    diagrams: dict[str, str] = {}
    links_seen: list[str] = []
    stack_links: list[str] = []
    if args.strict:
        check_step_stack_links(md_text)
    md = (MarkdownIt("commonmark", {"html": True, "typographer": False})
          .enable(["table", "strikethrough"])
          .use(footnote_plugin)
          .use(tasklists_plugin, enabled=True)
          .use(anchors_plugin, min_level=1, max_level=6, slug_func=github_slug,
               permalink=True, permalinkSymbol="#", permalinkBefore=False))
    add_rules(md, diagrams, links_seen, stack_links)

    env: dict = {}
    tokens = md.parse(md_text, env)
    title_tok = next((tokens[i + 1] for i, t in enumerate(tokens) if t.type == "heading_open" and t.tag == "h1"), None)
    if title_tok is None:
        die(f"{SRC} has no '# ' title")
    title = title_tok.content
    body = md.renderer.render(tokens, md.options, env)
    toc = build_toc(tokens)

    render_mermaid(diagrams)
    for key in diagrams:
        svg = (MERMAID_CACHE / f"{key}.svg").read_text(encoding="utf-8")
        body = body.replace(f"@@MERMAID:{key}@@", svg)

    if body.count(PATH_MARKER) != 1:
        die(f"expected exactly one {PATH_MARKER} marker in {SRC}, found {body.count(PATH_MARKER)}")
    body = body.replace(PATH_MARKER, PATH_CHOOSER)
    if body.count(CONNECT_MARKER) != 1:
        die(f"expected exactly one {CONNECT_MARKER} marker in {SRC}, found {body.count(CONNECT_MARKER)}")
    body = body.replace(CONNECT_MARKER, CONNECT_BOX)
    if not stack_links:
        die(f"{SRC} has no stack-link: links, so connecting a stack would change nothing")
    for path in ("panel", "terminal"):
        n = body.count(f'<div data-path="{path}"')
        if not n:
            die(f"{SRC} has no <div data-path=\"{path}\"> block")
    ids = set(re.findall(r'\sid="([^"]+)"', body))
    broken = sorted({a for a in links_seen if a not in ids})
    if broken:
        die(f"in-page links without a matching heading: {', '.join('#' + b for b in broken)}")

    template = (SITE / "template.html").read_text(encoding="utf-8")
    chapter_pages = build_chapter_pages(md, env, tokens, diagrams, template, read_boxes(md_text))
    check_unrendered_markdown({GUIDE_PAGE: body, **chapter_pages})
    chapter_ids = {f: page_ids(p) for f, p in chapter_pages.items()}
    search_entries = build_search_index(chapter_pages)
    n_page_links = check_page_links(chapter_pages, ids)
    page = (template
            .replace("{{title}}", html.escape(title))
            .replace("{{toc}}", toc)
            .replace("{{source_url}}", BLOB_URL + SRC_REPO_PATH)
            .replace("{{repo_url}}", REPO_URL)
            .replace("{{content}}", body)
            .replace('<a class="topbar-extra topbar-md"', f'<a class="topbar-extra" href="{INTRO_PAGE}">Step by step</a><a class="topbar-extra topbar-md"', 1))

    arch_w, arch_h = png_size(WORKSHOP / "img/arch-5-datadog.png")
    facts = intro_facts(md_text)
    landing = ((SITE / "landing.html").read_text(encoding="utf-8")
               .replace("{{repo_url}}", REPO_URL)
               .replace("{{repo_label}}", REPO_LABEL)
               .replace("{{time}}", facts["time"])
               .replace("{{cost}}", facts["cost"])
               .replace("{{arch_w}}", str(arch_w))
               .replace("{{arch_h}}", str(arch_h)))
    landing_imgs = check_landing(landing, ids, chapter_ids)

    # write output: docs/index.html (landing), docs/workshop.html (guide), docs/assets/*, docs/img/*, docs/images/*
    if OUT.exists():
        for child in (LANDING_PAGE, GUIDE_PAGE, GO_PAGE, *PAGE_FILES, "assets", "img", "images", ".nojekyll"):
            p = OUT / child
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    (OUT / "assets").mkdir(parents=True, exist_ok=True)
    for asset in ASSETS:
        shutil.copy2(SITE / asset, OUT / "assets" / asset)
    shutil.copytree(SITE / "vendor", OUT / "assets" / "vendor")
    write_search_index(search_entries)
    shutil.copytree(SITE / "images", OUT / "images", ignore=shutil.ignore_patterns("*.md"))
    (OUT / GUIDE_PAGE).write_text(bust_cache(external_links_in_new_tab(page)), encoding="utf-8")
    for fname, chapter_page in chapter_pages.items():
        (OUT / fname).write_text(bust_cache(external_links_in_new_tab(chapter_page)), encoding="utf-8")
    (OUT / LANDING_PAGE).write_text(bust_cache(external_links_in_new_tab(landing)), encoding="utf-8")
    go_page = (SITE / GO_PAGE).read_text(encoding="utf-8")
    for need in (f'href="{SETUP_PAGE}#connect-box"', f'href="{GUIDE_PAGE}"', 'href="assets/site.css"', 'src="assets/stack.js"', 'src="assets/go.js"'):
        if need not in go_page:
            die(f"{SITE / GO_PAGE} must contain {need}")
    (OUT / GO_PAGE).write_text(bust_cache(go_page), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    used = sorted(set(re.findall(r'src="(img/[^"]+)"', body)) | set(landing_imgs)
                  | {m for p in chapter_pages.values() for m in re.findall(r'src="(img/[^"]+)"', p)})
    (OUT / "img").mkdir()
    for rel in used:
        shutil.copy2(WORKSHOP / rel, OUT / rel)
    print(f"build.py: wrote {len(chapter_pages)} chapter pages ({', '.join(PAGE_FILES)}), {n_page_links} links between pages checked")
    print(f"build.py: wrote {OUT / GUIDE_PAGE} ({len(diagrams)} diagrams, {len(ids)} anchors, "
          f"{len(links_seen)} in-page links checked, {len(stack_links)} stack links) and {OUT / LANDING_PAGE} ({len(landing_imgs)} images); "
          f"{len(used)} images in {OUT / 'img'}")


if __name__ == "__main__":
    main()
