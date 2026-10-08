// go.html#<key>: a link that never changes when the stack is rebuilt (slides, bookmarks). It reads the stack the
// reader connected in this browser (assets/stack.js, the same storage and validation as the Connect box) and sends
// the browser to that stack's page. Anything that cannot be resolved stays on this page and says why.
(function () {
  "use strict";

  // Chapter focus of the demo home: demo-home-N opens the demo home focused on Chapter N's group.
  // tile_focus=<widget id> is UNDOCUMENTED by Datadog: it scrolls to and highlights a group. If Datadog drops it, the
  // dashboard still opens at the top of the demo home, which is the fallback. The group ids (7000000000000001 to
  // 7000000000000006) are pinned in terraform/datadog/overview.tf. tile_focus alone makes Datadog rewrite the URL to a
  // fixed past window (the dashboard is paused), so the link also carries a sliding one-hour window computed at
  // redirect time: from_ts/to_ts in epoch milliseconds, live=true and refresh_mode=sliding (verified live in Datadog).
  var CHAPTER_GROUP_IDS = { 1: "7000000000000001", 2: "7000000000000002", 3: "7000000000000003",
    4: "7000000000000004", 5: "7000000000000005", 6: "7000000000000006" };
  var LIVE_WINDOW_MS = 3600000;

  // The fixed window of the night of the 1.1.0 saturation (stack hybrid): the same range the demo home's
  // "night of the incident" link uses (terraform/datadog/overview.tf, home_incident_from_ms / home_incident_to_ms).
  var NIGHT = { from_ts: "1791454200000", to_ts: "1791455400000", live: "false" };

  var title = document.getElementById("go-title");
  var message = document.getElementById("go-message");
  var links = document.getElementById("go-links");

  function stop(reason) {
    console.warn("workshop go.html: not redirecting: " + reason);
    title.textContent = "This link cannot open your stack yet";
    message.textContent = reason;
    links.hidden = false;
  }

  // Query string (without ?) merged into a URL, keeping what the URL already has; a given name replaces the old value.
  function withQuery(base, pairs) {
    var u = new URL(base);
    Object.keys(pairs).forEach(function (k) { u.searchParams.set(k, pairs[k]); });
    return u.toString();
  }

  var key = decodeURIComponent(location.hash.replace(/^#/, "")).trim();
  if (!key) return stop("This link has no page name after the #. Open the guide to find the stack's pages.");

  var stack;
  try { stack = WorkshopStack.loadSaved(); }
  catch (e) { return stop("The stack saved in this browser is not valid (" + e.message + "). Connect it again in the Connect box."); }
  if (!stack) return stop("No stack is connected in this browser. Connect it once in the Connect box, then open this link again.");

  var target = null, m;
  if (key === "demo-home") {
    target = stack.links["overview-dashboard"];
  } else if ((m = /^demo-home-([1-6])$/.exec(key))) {
    target = stack.links["overview-dashboard"];
    if (target) {
      var now = Date.now();
      target = withQuery(target, { tile_focus: CHAPTER_GROUP_IDS[m[1]], from_ts: String(now - LIVE_WINDOW_MS),
        to_ts: String(now), live: "true", refresh_mode: "sliding" });
    }
  } else if (key === "night-of-incident") {
    target = stack.links["overview-dashboard"];
    if (target) target = withQuery(target, { tpl_var_env: stack.env, from_ts: NIGHT.from_ts, to_ts: NIGHT.to_ts, live: NIGHT.live });
  } else if (WorkshopStack.LINK_KEYS.indexOf(key) >= 0) {
    target = stack.links[key];
  } else {
    return stop('"' + key + '" is not a page name this link knows. Known names: demo-home, demo-home-1 to demo-home-6, night-of-incident, ' + WorkshopStack.LINK_KEYS.join(", ") + ".");
  }
  if (!target) return stop('Your connected stack has no "' + (key.indexOf("demo-home") === 0 || key === "night-of-incident" ? "overview-dashboard" : key) + '" link (that layer may be off). Check the Connect box.');

  location.replace(target);
})();
