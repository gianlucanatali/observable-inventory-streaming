/* Workshop guide search. Source: workshop/site/search.js (copied to docs/assets by build.py).
 * Fetches assets/search-index.json (one entry per heading: p page, a anchor, h heading, c chapter, g parent, t text)
 * on first use. Words are AND-ed; case and accents are ignored; heading hits rank above body hits. */
(function () {
  "use strict";
  var root = document.querySelector(".search");
  if (!root) return;
  var input = root.querySelector(".search-input");
  var results = root.querySelector(".search-results");
  var toggle = root.querySelector(".search-toggle");
  var closeBtn = root.querySelector(".search-close");
  var indexUrl = root.getAttribute("data-index");
  var MAX = 25;

  var entries = null, loading = null, loadError = null;
  var hits = [], active = -1;

  // Lower case and strip accents, one output character per input character so match offsets still fit the original.
  function norm(s) {
    var out = "";
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
      out += c.length ? c.charAt(0) : s.charAt(i);
    }
    return out;
  }

  function load() {
    if (entries) return Promise.resolve();
    if (loading) return loading;
    loading = fetch(indexUrl).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " for " + indexUrl);
      return r.json();
    }).then(function (data) {
      if (!Array.isArray(data) || !data.length) throw new Error("the search index is empty");
      data.forEach(function (e) { e.nh = norm(e.h); e.nc = norm(e.c + " " + e.g); e.nt = norm(e.t); });
      entries = data;
      loadError = null;
    }).catch(function (err) {
      loadError = err;
      loading = null;
      console.error("workshop search: could not load the index:", err);
    });
    return loading;
  }

  function terms(q) {
    return norm(q).split(/\s+/).filter(Boolean);
  }

  function score(e, ts) {
    var s = 0, headAll = true, count = 0;
    for (var i = 0; i < ts.length; i++) {
      var t = ts[i], inH = e.nh.indexOf(t) >= 0, inC = e.nc.indexOf(t) >= 0, inT = e.nt.indexOf(t) >= 0;
      if (!inH && !inC && !inT) return 0;
      if (inH) s += 20; else headAll = false;
      if (inC) s += 4;
      if (inT) { s += 2; count += Math.min(5, e.nt.split(t).length - 1); }
    }
    if (headAll) s += 100;
    if (e.nh.indexOf(ts.join(" ")) === 0) s += 40;
    else if (e.nh.indexOf(ts.join(" ")) > 0) s += 15;
    if (ts.length > 1 && e.nt.indexOf(ts.join(" ")) >= 0) s += 10;  // the words together, in order
    return s + count;
  }

  function search(q) {
    var ts = terms(q);
    if (!ts.length) return [];
    var out = [];
    entries.forEach(function (e, i) {
      var s = score(e, ts);
      if (s) out.push({ e: e, s: s, i: i });
    });
    out.sort(function (a, b) { return b.s - a.s || a.i - b.i; });
    return out;
  }

  // Append `text` to `parent`, wrapping every occurrence of a term in <mark>. Built with DOM nodes, never innerHTML.
  function highlight(parent, text, ts) {
    var n = norm(text), marks = new Array(text.length + 1).fill(0);
    ts.forEach(function (t) {
      for (var at = n.indexOf(t); at >= 0; at = n.indexOf(t, at + t.length)) {
        for (var k = at; k < at + t.length; k++) marks[k] = 1;
      }
    });
    var i = 0;
    while (i < text.length) {
      var j = i;
      while (j < text.length && marks[j] === marks[i]) j++;
      var piece = text.slice(i, j);
      if (marks[i]) { var m = document.createElement("mark"); m.textContent = piece; parent.appendChild(m); }
      else parent.appendChild(document.createTextNode(piece));
      i = j;
    }
  }

  function snippet(e, ts) {
    var n = e.nt, first = -1;
    ts.forEach(function (t) { var at = n.indexOf(t); if (at >= 0 && (first < 0 || at < first)) first = at; });
    if (first < 0) return e.t.slice(0, 140) + (e.t.length > 140 ? "…" : "");
    var start = Math.max(0, first - 50);
    if (start > 0) { var sp = e.t.indexOf(" ", start); if (sp >= 0 && sp < first) start = sp + 1; }
    var end = Math.min(e.t.length, start + 150);
    return (start > 0 ? "…" : "") + e.t.slice(start, end) + (end < e.t.length ? "…" : "");
  }

  function hrefFor(e) {
    return e.p + (e.a && e.a !== "top" ? "#" + e.a : "");
  }

  function message(text) {
    results.textContent = "";
    var p = document.createElement("p");
    p.className = "search-msg";
    p.setAttribute("role", "status");
    p.textContent = text;
    results.appendChild(p);
    hits = [];
    active = -1;
    show();
  }

  function show() { results.hidden = false; input.setAttribute("aria-expanded", "true"); }
  function hide() { results.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }

  function setActive(i) {
    var items = results.querySelectorAll(".search-hit");
    if (!items.length) { active = -1; return; }
    active = (i + items.length) % items.length;
    items.forEach(function (el, k) { el.setAttribute("aria-selected", k === active ? "true" : "false"); });
    input.setAttribute("aria-activedescendant", items[active].id);
    items[active].scrollIntoView({ block: "nearest" });
  }

  function render() {
    var q = input.value;
    if (!terms(q).length) { hide(); results.textContent = ""; return; }
    if (loadError) { message("Search is unavailable: the index could not be loaded (" + loadError.message + "). Serve the site over http or reload."); return; }
    if (!entries) { message("Loading the search index…"); return; }
    var ts = terms(q);
    hits = search(q);
    if (!hits.length) { message("No results for “" + q.trim() + "”. Try fewer or different words."); return; }
    results.textContent = "";
    hits.slice(0, MAX).forEach(function (h, k) {
      var a = document.createElement("a");
      a.className = "search-hit";
      a.id = "search-hit-" + k;
      a.setAttribute("role", "option");
      a.setAttribute("aria-selected", "false");
      a.href = hrefFor(h.e);
      a.tabIndex = -1;
      var head = document.createElement("span"); head.className = "search-hit-head"; highlight(head, h.e.h, ts);
      var crumb = document.createElement("span"); crumb.className = "search-hit-crumb";
      crumb.textContent = h.e.c + (h.e.g && h.e.g !== h.e.h && h.e.g !== h.e.c ? " › " + h.e.g : "");
      var snip = document.createElement("span"); snip.className = "search-hit-snippet"; highlight(snip, snippet(h.e, ts), ts);
      a.appendChild(head); a.appendChild(crumb); a.appendChild(snip);
      a.addEventListener("click", function (ev) { ev.preventDefault(); go(h.e); });
      results.appendChild(a);
    });
    if (hits.length > MAX) {
      var foot = document.createElement("p");
      foot.className = "search-foot";
      foot.textContent = "Showing " + MAX + " of " + hits.length + " results. Add a word to narrow them.";
      results.appendChild(foot);
    }
    show();
    setActive(0);
  }

  // The one-page guide holds every anchor: scroll there. A step page opens the page that owns the section.
  function go(e) {
    close(true);
    var onThisPage = e.a && e.a !== "top" && document.getElementById(e.a);
    var here = location.pathname.split("/").pop() || "index.html";
    if (onThisPage || (here === e.p && (!e.a || e.a === "top"))) {
      if (onThisPage) { location.hash = e.a; } else { window.scrollTo(0, 0); }
      return;
    }
    location.href = hrefFor(e);
  }

  function open() {
    root.classList.add("open");
    toggle.setAttribute("aria-expanded", "true");
    input.focus();
    input.select();
    load().then(render);
  }

  function close(clear) {
    hide();
    if (clear) input.value = "";
    root.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    if (document.activeElement === input) input.blur();
  }

  toggle.addEventListener("click", function () { if (root.classList.contains("open")) close(false); else open(); });
  closeBtn.addEventListener("click", function () { close(true); toggle.focus(); });
  input.addEventListener("focus", function () { load().then(render); });
  input.addEventListener("input", function () { load().then(render); render(); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); if (!results.hidden) setActive(active + 1); else render(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (!results.hidden) setActive(active - 1); }
    else if (e.key === "Enter") {
      var h = hits[active];
      if (h) { e.preventDefault(); go(h.e); }
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (!results.hidden) close(false); else close(true);
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
    e.preventDefault();
    open();
  });
  document.addEventListener("click", function (e) {
    if (!root.contains(e.target) && !results.hidden) hide();
  });
  window.matchMedia("(min-width: 1181px)").addEventListener("change", function () { root.classList.remove("open"); });
})();
