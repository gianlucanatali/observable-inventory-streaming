// Shared by site.js (the Connect box) and go.html (stable redirect links): the one place that knows the storage key
// and how the stack JSON is validated. Loaded before both. Exposes window.WorkshopStack.
(function () {
  "use strict";
  var STACK_KEY = "workshop-stack";
  var STACK_MAX_BYTES = 20000;
  var TEXT_FIELDS = { stack: /^[A-Za-z0-9][A-Za-z0-9_-]*$/, env: /^[A-Za-z0-9][A-Za-z0-9_-]*$/,
    vm_public_ip: /^[A-Za-z0-9][A-Za-z0-9.-]*$/, confluent_env: /^[A-Za-z0-9_-]+$/, kafka_cluster: /^[A-Za-z0-9_-]+$/ };
  var REQUIRED_LINKS = ["shop", "shop-home", "control"];
  var OPTIONAL_LINKS = ["overview-dashboard", "stock-dashboard", "online-dashboard", "apm", "dsm", "cost-dashboard", "confluent", "stream-lineage", "topic-inventory-cdc", "topic-stock-sellable", "control-center", "ecs"];

  function webUrl(value, what) {
    if (typeof value !== "string") throw new Error(what + " must be a text value");
    var u;
    try { u = new URL(value); } catch (e) { throw new Error(what + " is not a valid URL"); }
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error(what + " must start with http:// or https://");
    return value;
  }
  // Returns a clean copy of the stack, or throws an Error whose message says what to fix.
  function parseStack(text) {
    if (!text.trim()) throw new Error("Paste the JSON first.");
    if (text.length > STACK_MAX_BYTES) throw new Error("That is too long to be the stack JSON.");
    var raw;
    try { raw = JSON.parse(text); } catch (e) { throw new Error("This is not valid JSON (" + e.message + "). Copy it again from the panel or from make links-json."); }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("The JSON must be an object, as copied from the panel.");
    if (raw.version !== 1) throw new Error('Unsupported "version": this guide reads version 1.');
    var out = { version: 1, links: {} };
    Object.keys(TEXT_FIELDS).forEach(function (k) {
      if (typeof raw[k] !== "string" || !raw[k]) throw new Error('The field "' + k + '" is missing.');
      if (!TEXT_FIELDS[k].test(raw[k])) throw new Error('The field "' + k + '" has characters that do not belong in it.');
      out[k] = raw[k];
    });
    out.alb = webUrl(raw.alb, 'The field "alb"').replace(/\/+$/, "");
    if (!raw.links || typeof raw.links !== "object") throw new Error('The field "links" is missing.');
    REQUIRED_LINKS.concat(OPTIONAL_LINKS).forEach(function (k) {
      var v = raw.links[k];
      if (v === undefined || v === null || v === "") {
        if (REQUIRED_LINKS.indexOf(k) >= 0) throw new Error('The link "' + k + '" is missing.');
        return;
      }
      out.links[k] = webUrl(v, 'The link "' + k + '"');
    });
    return out;
  }

  // The stack saved by the Connect box: null when nothing is saved (or the browser blocks storage); throws (message
  // says why) when what is saved is invalid. It never removes anything: only the Forget button does (site.js).
  function loadSaved() {
    var raw;
    try { raw = localStorage.getItem(STACK_KEY); }
    catch (e) { console.warn("workshop: localStorage unavailable, no saved stack can be read:", e); return null; }
    return raw ? parseStack(raw) : null;
  }
  window.WorkshopStack = { STACK_KEY: STACK_KEY, parseStack: parseStack, loadSaved: loadSaved,
    LINK_KEYS: REQUIRED_LINKS.concat(OPTIONAL_LINKS) };
})();
