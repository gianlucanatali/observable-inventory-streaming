#!/usr/bin/env python3
"""Build the static workshop site: a landing page and the guide rendered from workshop/README.md.

The Markdown guide is the only source of the guide. This script renders it to docs/workshop.html,
fills the landing page template (workshop/site/landing.html) into docs/index.html, and copies the
images and assets (served by GitHub Pages from the "main /docs" folder). Both pages are checked:
every image must exist and every in-site link must resolve.

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
import posixpath
import re
import shutil
import sys
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
LANDING_PAGE = "index.html"
ASSETS = ("site.css", "site.js", "fonts.css", "landing.css", "landing.js", "landing-noscript.css")
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


# A guide link written as [stock dashboard](#4-open-the-datadog-stock-dashboard "stack-link:stock-dashboard") is
# an "open the X" reference. On GitHub it is an ordinary link to the step that explains X. On the site, site.js
# points it to the reader's own resource once they pasted their stack JSON (see CONNECT_BOX), else to the box.
STACK_LINK_PREFIX = "stack-link:"
STACK_LINK_KEYS = ("shop", "shop-home", "control", "stock-dashboard", "online-dashboard", "apm", "dsm",
                   "cost-dashboard", "confluent", "control-center", "ecs")
CONNECT_MARKER = "<!-- connect-box -->"
CONNECT_BOX = """<div class="connect-box" id="connect-box">
<p class="connect-title">Connect your stack</p>
<p class="connect-lead">Paste the JSON from your control panel or from <code>make links-json</code>.</p>
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
        lang = (tok.info or "").strip().split()[0] if tok.info.strip() else "text"
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
        # Only commands get a Copy button: outputs and examples are for reading, not for pasting.
        button = (f'<button type="button" class="copy" aria-label="Copy {label.lower()} to clipboard">'
                  f'<span class="copy-icon" aria-hidden="true"></span><span class="copy-text">Copy</span></button>'
                  if kind == "cmd" else "")
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


def check_landing(page: str, guide_ids: set[str]) -> list[str]:
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
    page = (template
            .replace("{{title}}", html.escape(title))
            .replace("{{toc}}", toc)
            .replace("{{source_url}}", BLOB_URL + SRC_REPO_PATH)
            .replace("{{repo_url}}", REPO_URL)
            .replace("{{content}}", body))

    arch_w, arch_h = png_size(WORKSHOP / "img/arch-5-datadog.png")
    facts = intro_facts(md_text)
    landing = ((SITE / "landing.html").read_text(encoding="utf-8")
               .replace("{{repo_url}}", REPO_URL)
               .replace("{{repo_label}}", REPO_LABEL)
               .replace("{{time}}", facts["time"])
               .replace("{{cost}}", facts["cost"])
               .replace("{{arch_w}}", str(arch_w))
               .replace("{{arch_h}}", str(arch_h)))
    landing_imgs = check_landing(landing, ids)

    # write output: docs/index.html (landing), docs/workshop.html (guide), docs/assets/*, docs/img/*, docs/images/*
    if OUT.exists():
        for child in (LANDING_PAGE, GUIDE_PAGE, "assets", "img", "images", ".nojekyll"):
            p = OUT / child
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    (OUT / "assets").mkdir(parents=True, exist_ok=True)
    for asset in ASSETS:
        shutil.copy2(SITE / asset, OUT / "assets" / asset)
    shutil.copytree(SITE / "vendor", OUT / "assets" / "vendor")
    shutil.copytree(SITE / "images", OUT / "images", ignore=shutil.ignore_patterns("*.md"))
    (OUT / GUIDE_PAGE).write_text(bust_cache(external_links_in_new_tab(page)), encoding="utf-8")
    (OUT / LANDING_PAGE).write_text(bust_cache(external_links_in_new_tab(landing)), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    used = sorted(set(re.findall(r'src="(img/[^"]+)"', body)) | set(landing_imgs))
    (OUT / "img").mkdir()
    for rel in used:
        shutil.copy2(WORKSHOP / rel, OUT / rel)
    print(f"build.py: wrote {OUT / GUIDE_PAGE} ({len(diagrams)} diagrams, {len(ids)} anchors, "
          f"{len(links_seen)} in-page links checked, {len(stack_links)} stack links) and {OUT / LANDING_PAGE} ({len(landing_imgs)} images); "
          f"{len(used)} images in {OUT / 'img'}")


if __name__ == "__main__":
    main()
