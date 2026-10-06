// Landing page behaviour: entrance animation, header, menu panel, image zoom, old deep links.
// Adapted from Solid State's main.js (HTML5 UP, CC BY 3.0) without jQuery.
// Source: workshop/site/landing.js (copied to docs/assets by build.py). No dependencies, no tracking.
(function () {
  "use strict";
  var body = document.body;

  // Deep links from before the guide moved to workshop.html (index.html#lab-3-...) still land.
  var hash = decodeURIComponent(location.hash.slice(1));
  if (hash && hash !== "menu" && !document.getElementById(hash)) {
    location.replace("workshop.html" + location.hash);
    return;
  }

  window.addEventListener("load", function () {
    setTimeout(function () { body.classList.remove("is-preload"); }, 100);
  });

  // Header: transparent over the banner, solid below it.
  var header = document.getElementById("header");
  var banner = document.getElementById("banner");
  if (banner && "IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      header.classList.toggle("alt", entries[0].isIntersecting);
    }, { rootMargin: "-" + header.offsetHeight + "px 0px 0px 0px" }).observe(banner);
  } else {
    header.classList.remove("alt");
  }

  // Menu panel.
  var menu = document.getElementById("menu");
  var toggle = header.querySelector('a[href="#menu"]');
  body.appendChild(menu);
  function setMenu(open) {
    body.classList.toggle("is-menu-visible", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (open) { var first = menu.querySelector(".links a"); if (first) first.focus({ preventScroll: true }); }
  }
  toggle.addEventListener("click", function (e) { e.preventDefault(); setMenu(!body.classList.contains("is-menu-visible")); });
  menu.addEventListener("click", function (e) {
    if (e.target === menu || e.target.closest(".close")) { e.preventDefault(); setMenu(false); toggle.focus(); return; }
    var a = e.target.closest("a");
    if (a) setMenu(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && body.classList.contains("is-menu-visible")) { setMenu(false); toggle.focus(); }
  });

  // Image zoom.
  var dialog = document.querySelector(".lightbox");
  if (dialog && typeof dialog.showModal === "function") {
    var big = dialog.querySelector("img"), cap = dialog.querySelector(".lightbox-caption");
    document.addEventListener("click", function (e) {
      var z = e.target.closest(".zoom");
      if (!z) return;
      var img = z.querySelector("img");
      big.src = img.currentSrc || img.src;
      big.alt = img.alt;
      cap.textContent = img.alt;
      dialog.showModal();
    });
    dialog.addEventListener("click", function (e) { if (e.target === dialog || e.target === big) dialog.close(); });
  } else {
    document.querySelectorAll(".zoom").forEach(function (z) { z.style.cursor = "default"; });
  }
})();
