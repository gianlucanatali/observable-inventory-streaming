// Workshop site behaviour: lab path (control panel or terminal), connected stack, copy buttons, table of contents, term tooltips, image zoom.
// Source: workshop/site/site.js (copied to docs/assets by build.py). No dependencies, no tracking.
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var live = document.getElementById("live");

  // ---------- lab path: control panel or terminal ----------
  // The inline script in <head> already set html[data-path-mode] (from ?path=, localStorage, or the default "panel")
  // before the first paint; CSS hides the blocks of the other path. Here the reader changes it.
  var root = document.documentElement;
  var PATH_KEY = "workshop-path";
  var NAMES = { panel: "control panel", terminal: "terminal" };
  function currentPath() { return root.getAttribute("data-path-mode") === "terminal" ? "terminal" : "panel"; }
  function syncPathButtons() {
    var p = currentPath();
    document.querySelectorAll("[data-set-path]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-set-path") === p));
    });
  }
  // Keep the content under the top bar where it is: blocks above it may change height when the path changes.
  function scrollAnchor() {
    var top = document.querySelector(".topbar").getBoundingClientRect().bottom;
    var kids = document.querySelectorAll(".doc > :not([data-path])");
    for (var i = 0; i < kids.length; i++) {
      var r = kids[i].getBoundingClientRect();
      if (r.bottom > top && r.height > 0) return { el: kids[i], y: r.top };
    }
    return null;
  }
  function applyPath(p) {
    var a = scrollAnchor();
    root.setAttribute("data-path-mode", p);
    root.setAttribute("data-path-chosen", "");
    try { localStorage.setItem(PATH_KEY, p); }
    catch (e) { console.warn("workshop: localStorage unavailable, the path choice is not remembered:", e); }
    if (a) window.scrollBy(0, a.el.getBoundingClientRect().top - a.y);
    syncPathButtons();
    live.textContent = "Showing the " + NAMES[p] + " steps";
  }
  var pathDialog = document.querySelector(".path-dialog");
  var pathOpener = null, pathWanted = null;
  function closePathDialog() {
    if (pathDialog.open) pathDialog.close();
    if (pathOpener) pathOpener.focus();
    pathOpener = pathWanted = null;
  }
  function requestPath(p, opener) {
    if (!root.hasAttribute("data-path-chosen") || p === currentPath()) { applyPath(p); return; }
    // A change after a first choice: warn, because the two paths keep separate Rollback memories.
    pathOpener = opener; pathWanted = p;
    pathDialog.querySelector(".path-dialog-other").textContent = NAMES[p];
    pathDialog.querySelector(".path-keep").textContent = "Keep " + NAMES[currentPath()];
    pathDialog.querySelector(".path-switch-confirm").textContent = "Switch to " + NAMES[p];
    pathDialog.showModal();
    pathDialog.querySelector(".path-keep").focus();
  }
  pathDialog.querySelector(".path-keep").addEventListener("click", closePathDialog);
  pathDialog.querySelector(".path-switch-confirm").addEventListener("click", function () {
    var p = pathWanted;
    closePathDialog();
    applyPath(p);
  });
  pathDialog.addEventListener("cancel", function (e) { e.preventDefault(); closePathDialog(); });  // Esc keeps the path
  pathDialog.addEventListener("keydown", function (e) {
    if (e.key !== "Tab") return;  // keep focus on the two buttons
    var b = pathDialog.querySelectorAll("button"), first = b[0], last = b[b.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-set-path]");
    if (b) requestPath(b.getAttribute("data-set-path"), b);
  });
  syncPathButtons();

  // ---------- copy buttons ----------
  function codeText(block) {
    var text = block.querySelector("pre code").textContent.replace(/\n$/, "");
    if (block.dataset.lang === "sh" || block.dataset.lang === "bash" || block.dataset.lang === "shell") {
      // Copy commands only: drop any leading shell prompt.
      text = text.split("\n").map(function (l) { return l.replace(/^\$ /, ""); }).join("\n");
    }
    return text;
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (!ok) throw new Error("document.execCommand('copy') returned false");
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { fallbackCopy(text); });
    }
    return new Promise(function (resolve) { fallbackCopy(text); resolve(); });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".copy");
    if (!btn) return;
    var block = btn.closest(".code");
    var label = btn.querySelector(".copy-text");
    copy(codeText(block)).then(function () {
      btn.classList.add("copied");
      label.textContent = "Copied";
      live.textContent = "Copied to clipboard";
      setTimeout(function () { btn.classList.remove("copied"); label.textContent = "Copy"; }, 1600);
    }).catch(function (err) {
      label.textContent = "Copy failed";
      console.error("copy to clipboard failed:", err);
    });
  });

  // ---------- table of contents: mobile drawer ----------
  var toc = document.getElementById("toc");
  var toggle = document.querySelector(".toc-toggle");
  var scrim = document.querySelector(".scrim");
  function setOpen(open) {
    toc.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    scrim.hidden = !open;
    if (open) { var a = toc.querySelector("a.active") || toc.querySelector("a"); if (a) a.focus(); }
  }
  toggle.addEventListener("click", function () { setOpen(!toc.classList.contains("open")); });
  scrim.addEventListener("click", function () { setOpen(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && toc.classList.contains("open")) { setOpen(false); toggle.focus(); }
  });
  toc.addEventListener("click", function (e) {
    if (e.target.closest("a") && toc.classList.contains("open")) setOpen(false);
  });

  // ---------- table of contents: scroll spy ----------
  var links = Array.prototype.slice.call(toc.querySelectorAll("a[href^='#']"));
  var byId = {};
  links.forEach(function (a) { byId[decodeURIComponent(a.getAttribute("href").slice(1))] = a; });
  var heads = Array.prototype.slice.call(document.querySelectorAll(".doc h2[id], .doc h3[id], .doc h4[id]"))
    .filter(function (h) { return byId[h.id]; });
  var current = null;
  var lockUntil = 0;  // after a TOC click the clicked entry stays active while the page scrolls there
  function spy(forced) {
    if (!forced && Date.now() < lockUntil) return;
    var y = window.scrollY + 120, active = heads[0];
    for (var i = 0; i < heads.length; i++) { if (heads[i].offsetTop <= y) active = heads[i]; else break; }
    // At the very bottom the last headings cannot reach the top: take the last heading that is on screen.
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
      for (var j = heads.length - 1; j >= 0; j--) {
        if (heads[j].offsetTop < window.scrollY + window.innerHeight - 80) { if (heads[j].offsetTop > y) active = heads[j]; break; }
      }
    }
    activate(active);
  }
  function activate(active) {
    if (!active || active === current) return;
    current = active;
    links.forEach(function (a) { a.classList.remove("active"); });
    toc.querySelectorAll(".active-section").forEach(function (li) { li.classList.remove("active-section"); });
    var link = byId[active.id];
    link.classList.add("active");
    var section = link.closest(".toc-h2");
    if (section) section.classList.add("active-section");
    if (!toc.classList.contains("open")) {
      var r = link.getBoundingClientRect(), t = toc.getBoundingClientRect();
      if (r.top < t.top + 40 || r.bottom > t.bottom - 40) toc.scrollTop += r.top - t.top - t.height / 3;
    }
  }
  links.forEach(function (a) {
    a.addEventListener("click", function () {
      var h = document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1)));
      if (!h || heads.indexOf(h) < 0) return;
      lockUntil = Date.now() + 900;
      activate(h);
    });
  });
  var ticking = false;
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; requestAnimationFrame(function () { ticking = false; spy(); }); }
  }, { passive: true });
  window.addEventListener("load", function () { spy(); });
  spy();

  // ---------- term explanations: <abbr title> becomes a tooltip on hover, focus and tap ----------
  // The native title is kept only when JS is off; here it moves to data-tip so the browser does not show a second tooltip.
  var abbrs = Array.prototype.slice.call(document.querySelectorAll(".doc abbr[title]"));
  if (abbrs.length) {
    var tip = document.createElement("div");
    tip.className = "tip";
    tip.id = "term-tip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.appendChild(tip);
    var shown = null, pinned = false;
    abbrs.forEach(function (a) {
      a.setAttribute("data-tip", a.getAttribute("title"));
      a.removeAttribute("title");
      a.setAttribute("tabindex", "0");
      a.setAttribute("aria-expanded", "false");
    });
    function place(a) {
      var r = a.getBoundingClientRect();
      var w = tip.offsetWidth, h = tip.offsetHeight, vw = document.documentElement.clientWidth;
      var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), vw - w - 8);
      var side = r.top - h - 10 < 64 ? "bottom" : "top";  // keep clear of the sticky top bar
      var top = side === "top" ? r.top - h - 10 : r.bottom + 10;
      tip.dataset.side = side;
      tip.style.left = (left + window.scrollX) + "px";
      tip.style.top = (top + window.scrollY) + "px";
      tip.style.setProperty("--arrow-x", Math.min(Math.max(12, r.left + r.width / 2 - left), w - 12) + "px");
    }
    function show(a, pin) {
      if (shown && shown !== a) shown.setAttribute("aria-expanded", "false");
      shown = a; pinned = !!pin;
      tip.textContent = a.getAttribute("data-tip");
      tip.setAttribute("data-term", a.textContent.trim());
      tip.hidden = false;
      a.setAttribute("aria-describedby", "term-tip");
      a.setAttribute("aria-expanded", "true");
      place(a);
    }
    function hide() {
      if (!shown) return;
      shown.removeAttribute("aria-describedby");
      shown.setAttribute("aria-expanded", "false");
      shown = null; pinned = false;
      tip.hidden = true;
    }
    document.addEventListener("mouseover", function (e) {
      var a = e.target.closest && e.target.closest("abbr[data-tip]");
      if (a && a !== shown) show(a, false);
    });
    document.addEventListener("mouseout", function (e) {
      var a = e.target.closest && e.target.closest("abbr[data-tip]");
      if (a && a === shown && !pinned && !a.contains(e.relatedTarget)) hide();
    });
    document.addEventListener("focusin", function (e) {
      if (e.target.matches && e.target.matches("abbr[data-tip]")) show(e.target, false);
    });
    document.addEventListener("focusout", function (e) {
      if (e.target === shown) hide();
    });
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest("abbr[data-tip]");
      if (a) {
        if (a === shown && pinned) hide(); else show(a, true);  // tap toggles on touch screens
        return;
      }
      if (shown) hide();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && shown) hide();
      if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("abbr[data-tip]")) {
        e.preventDefault();
        if (e.target === shown && pinned) hide(); else show(e.target, true);
      }
    });
    window.addEventListener("resize", function () { if (shown) place(shown); });
    // A hover tooltip goes away when the page scrolls under the pointer; a tapped or focused one stays with its term.
    window.addEventListener("scroll", function () { if (shown && !pinned && shown !== document.activeElement) hide(); }, { passive: true });
  }

  // ---------- connect your stack ----------
  // The reader pastes the JSON from the control panel (or `make links-json`). It is validated, kept only in
  // localStorage, and then (1) placeholders such as <alb-dns-name> are replaced by the reader's values and
  // (2) the "open the X" links (data-stack-link) go to the reader's own pages. Without a stack, or without
  // localStorage, the guide reads as before and those links go to the Connect box.
  var STACK_KEY = WorkshopStack.STACK_KEY, parseStack = WorkshopStack.parseStack;  // assets/stack.js
  var box = document.getElementById("connect-box");  // only on the guide and on the Setup page
  var badge = document.getElementById("stack-badge");
  // Chapter pages other than Setup have no box: links that need it go to the Setup page (data-connect-href on <body>).
  var CONNECT_HREF = document.body.getAttribute("data-connect-href") || "#connect-box";
  var applied = [];  // {orig, nodes}: text nodes the stack replaced, so Forget can restore them
  var stackNow = null;


  // ---- placeholder replacement ----
  // A segment is a plain string, or {text, href?, ph} for a value taken from the stack.
  function stackRules(s) {
    var host = new URL(s.alb).host;
    var origin = s.alb;
    var values = { "alb-dns-name": host, "vm-public-ip": s.vm_public_ip, "env-id": s.confluent_env,
      "cluster-id": s.kafka_cluster, "env": s.env, "stack name": s.env, "stack": s.stack };
    function sub(str) {
      return str.replace(/https?:\/\/<alb-dns-name>/g, origin)
        .replace(/<(alb-dns-name|vm-public-ip|env-id|cluster-id|env|stack name|stack)>/g, function (m, k) { return values[k]; });
    }
    function pathOf(url) { var u = new URL(url); return u.pathname; }
    var rules = [];
    // sample output lines of ./demo status and ./demo links
    [[/(overview dashboard: )https:\/\/\S+/g, "overview-dashboard"], [/(stock dashboard: )https:\/\/\S+/g, "stock-dashboard"], [/(online dashboard: )https:\/\/\S+/g, "online-dashboard"],
     [/(account-cost dashboard: )https:\/\/\S+/g, "cost-dashboard"], [/(APM inventory-api 1\.1\.0: )https:\/\/\S+/g, "apm"],
     [/(DSM map: )https:\/\/\S+/g, "dsm"]].forEach(function (r) {
      var url = s.links[r[1]];
      if (url) rules.push({ re: r[0], fn: function (m) { return [m[1], { text: url, href: url, ph: r[1] }]; } });
    });
    if (s.links.apm) {
      var cmp = new URL(s.links.apm);
      cmp.searchParams.delete("version");
      cmp.searchParams.set("compare_to", "previous");
      rules.push({ re: /(APM latency comparison: )https:\/\/\S+/g, fn: function (m) { return [m[1], { text: cmp.href, href: cmp.href, ph: "apm" }]; } });
    }
    if (s.links["cost-dashboard"]) {
      rules.push({ re: /(cost dashboard: )\/dashboard\/<id>\/\S+/g, fn: function (m) {
        return [m[1], { text: pathOf(s.links["cost-dashboard"]), href: s.links["cost-dashboard"], ph: "cost-dashboard" }]; } });
    }
    if (s.links["stock-dashboard"]) {
      rules.push({ re: /(\bdashboard: )\/dashboard\/<id>\/\S+/g, fn: function (m) {
        return [m[1], { text: pathOf(s.links["stock-dashboard"]), href: s.links["stock-dashboard"], ph: "stock-dashboard" }]; } });
    }
    // a whole URL that holds a placeholder: the replaced URL is a link
    rules.push({ re: /https?:\/\/[^\s"'`]+/g, fn: function (m) {
      var url = m[0].replace(/[.,;:)]+$/, ""), tail = m[0].slice(url.length);
      if (!/<(alb-dns-name|vm-public-ip)>/.test(url)) return null;
      var real = sub(url);
      return [{ text: real, href: /</.test(real) ? null : real, ph: "url" }, tail];
    } });
    rules.push({ re: /<(alb-dns-name|vm-public-ip|env-id|cluster-id|env|stack name|stack)>/g, fn: function (m) { return [{ text: values[m[1]], ph: m[1] }]; } });
    if (s.stack !== "hybrid") {
      rules.push({ re: /stack:hybrid\b/g, fn: function () { return ["stack:", { text: s.stack, ph: "stack" }]; } });
    }
    if (s.env !== "dd-demo-hybrid") {
      rules.push({ re: /dd-demo-hybrid/g, fn: function () { return [{ text: s.env, ph: "env" }]; } });
    }
    return rules;
  }
  function splitBy(segs, rule) {
    var out = [];
    segs.forEach(function (seg) {
      if (typeof seg !== "string") { out.push(seg); return; }
      var last = 0, m;
      rule.re.lastIndex = 0;
      while ((m = rule.re.exec(seg))) {
        var rep = rule.fn(m);
        if (!rep) continue;
        if (m.index > last) out.push(seg.slice(last, m.index));
        rep.forEach(function (r) { if (r !== "") out.push(r); });
        last = m.index + m[0].length;
      }
      if (last < seg.length) out.push(seg.slice(last));
    });
    return out;
  }
  function nodeFor(seg, inCmd) {
    var el;
    if (seg.href && !inCmd) {
      el = document.createElement("a");
      el.href = seg.href;
      el.target = "_blank";
      el.rel = "noopener";
    } else {
      el = document.createElement("span");
    }
    el.className = "stk";
    el.setAttribute("data-ph", seg.ph);
    el.textContent = seg.text;
    return el;
  }
  var PH_TEST = /<(?:alb-dns-name|vm-public-ip|env-id|cluster-id|env|stack name|stack|id)>|dd-demo-hybrid|stack:hybrid/;
  function replaceInDoc(s) {
    var rules = stackRules(s);
    var walker = document.createTreeWalker(document.querySelector(".doc"), NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!PH_TEST.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
        var p = n.parentElement;
        if (!p || p.closest(".connect-box, .path-chooser, script, style, textarea, h1, h2, h3, h4, .header-anchor, .stk")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      } });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (n) {
      var segs = [n.nodeValue];
      rules.forEach(function (r) { segs = splitBy(segs, r); });
      if (segs.length === 1 && typeof segs[0] === "string") return;
      var inCmd = !!n.parentElement.closest(".code-cmd");
      var frag = document.createDocumentFragment(), made = [];
      segs.forEach(function (seg) {
        var x = typeof seg === "string" ? document.createTextNode(seg) : nodeFor(seg, inCmd);
        frag.appendChild(x); made.push(x);
      });
      n.parentNode.insertBefore(frag, n);
      n.parentNode.removeChild(n);
      applied.push({ orig: n, nodes: made });
    });
  }
  function restoreDoc() {
    applied.forEach(function (a) {
      a.nodes[0].parentNode.insertBefore(a.orig, a.nodes[0]);
      a.nodes.forEach(function (x) { x.parentNode.removeChild(x); });
    });
    applied = [];
  }

  // ---- "open the X" links ----
  function applyLinks(s) {
    document.querySelectorAll("a[data-stack-link]").forEach(function (a) {
      if (!a.hasAttribute("data-guide-href")) a.setAttribute("data-guide-href", a.getAttribute("href"));
      var url = s && s.links[a.getAttribute("data-stack-link")];
      a.classList.add("stk-link");
      if (url) {
        a.href = url; a.target = "_blank"; a.rel = "noopener";
        a.classList.add("stk-on");
        a.title = "Opens your stack's page in a new tab";
      } else {
        a.href = CONNECT_HREF; a.removeAttribute("target"); a.removeAttribute("rel");
        a.classList.remove("stk-on");
        a.title = s ? "This page is not in your pasted JSON. Open the Connect box." : "Connect your stack to open this directly";
      }
    });
  }

  function setStatus(msg, bad) {
    if (!box) return;
    var el = box.querySelector(".connect-status");
    el.textContent = msg;
    el.classList.toggle("connect-bad", !!bad);
  }
  function showConnected(s) {
    stackNow = s;
    restoreDoc();
    if (s) replaceInDoc(s);
    applyLinks(s);
    if (badge) {
      badge.hidden = !s;
      badge.classList.remove("stack-badge-bad");
      badge.title = "Your stack is connected to this guide. Open the Connect box.";
      if (s) badge.querySelector(".stack-badge-name").textContent = ": stack " + s.stack;
    }
    if (box) {
      box.querySelector(".connect-forget").hidden = !s;
      box.classList.toggle("connected", !!s);
      var state = document.querySelector(".connect-state");
      if (state) state.textContent = s ? "connected: stack " + s.stack : "recommended";
    }
  }
  // A saved stack that cannot be read is KEPT (only Forget removes it): it may come from a newer or older guide, and
  // deleting it would lose what the reader pasted without a word. The box and the badge say why it is not used.
  function showUnreadable(reason) {
    var msg = "The saved stack could not be read: " + reason + " Paste it again and press Connect, or press Forget my stack.";
    console.warn("workshop: " + msg);
    setStatus(msg, true);
    if (badge) {
      badge.hidden = false;
      badge.classList.add("stack-badge-bad");
      badge.title = "The stack saved in this browser could not be read. Open the Connect box.";
      badge.querySelector(".stack-badge-name").textContent = ": saved stack unreadable";
    }
    if (box) box.querySelector(".connect-forget").hidden = false;
    var state = document.querySelector(".connect-state");
    if (state) state.textContent = "saved stack unreadable";
  }
  var saved = null, unreadable = null;
  try { saved = WorkshopStack.loadSaved(); }
  catch (e) { unreadable = e.message; }
  showConnected(saved);
  if (saved) setStatus("Connected: stack " + saved.stack + ", environment " + saved.env + ". Placeholders and links now use your values.");
  if (unreadable) showUnreadable(unreadable);
  if (box) {
    var input = document.getElementById("connect-json");
    box.querySelector(".connect-go").addEventListener("click", function () {
      var s;
      try { s = parseStack(input.value); }
      catch (e) { setStatus(e.message, true); return; }
      var remembered = true;
      try { localStorage.setItem(STACK_KEY, JSON.stringify(s)); }
      catch (e) {
        remembered = false;
        console.warn("workshop: localStorage unavailable, the stack is not remembered:", e);
      }
      showConnected(s);
      input.value = "";
      setStatus("Connected: stack " + s.stack + ", environment " + s.env + ". Placeholders and links now use your values." +
        (remembered ? "" : " This browser blocks storage, so the stack is forgotten when you reload."));
      live.textContent = "Stack " + s.stack + " connected";
    });
    box.querySelector(".connect-forget").addEventListener("click", function () {
      try { localStorage.removeItem(STACK_KEY); }
      catch (e) { console.warn("workshop: localStorage unavailable:", e); }
      showConnected(null);
      setStatus("Your stack is forgotten. The guide shows the placeholders again.");
      live.textContent = "Stack forgotten";
    });
  }

  // ---------- a link to something inside a closed <details> (the Connect box) opens it ----------
  function openDetailsOf(id) {
    var el = id && document.getElementById(id), d = el && el.closest("details");
    if (d && !d.open) { d.open = true; return el; }
    return null;
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest('a[href^="#"]');
    if (a && a.getAttribute("href").length > 1) openDetailsOf(decodeURIComponent(a.getAttribute("href").slice(1)));
  });
  function revealHash() {
    var el = location.hash.length > 1 && openDetailsOf(decodeURIComponent(location.hash.slice(1)));
    if (el) el.scrollIntoView();
  }
  window.addEventListener("hashchange", revealHash);
  revealHash();

  // ---------- image zoom ----------
  var dialog = document.querySelector(".lightbox");
  if (dialog && typeof dialog.showModal === "function") {
    var big = dialog.querySelector("img"), cap = dialog.querySelector(".lightbox-caption");
    document.addEventListener("click", function (e) {
      var z = e.target.closest(".zoom");
      if (!z) return;
      var img = z.querySelector("img");
      var fig = z.closest("figure");
      var fc = fig && fig.querySelector("figcaption");
      big.src = img.currentSrc || img.src;
      big.alt = img.alt;
      cap.textContent = fc ? fc.textContent : img.alt;
      dialog.showModal();
    });
    dialog.addEventListener("click", function (e) { if (e.target === dialog || e.target === big) dialog.close(); });
  } else {
    document.querySelectorAll(".zoom").forEach(function (z) { z.style.cursor = "default"; });
  }
})();
