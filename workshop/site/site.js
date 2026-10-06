// Workshop site behaviour: lab path (control panel or terminal), copy buttons, table of contents, term tooltips, image zoom.
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
  var heads = Array.prototype.slice.call(document.querySelectorAll(".doc h2[id], .doc h3[id]"))
    .filter(function (h) { return byId[h.id]; });
  var current = null;
  function spy() {
    var y = window.scrollY + 120, active = heads[0];
    for (var i = 0; i < heads.length; i++) { if (heads[i].offsetTop <= y) active = heads[i]; else break; }
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
  var ticking = false;
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; requestAnimationFrame(function () { ticking = false; spy(); }); }
  }, { passive: true });
  window.addEventListener("load", spy);
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
