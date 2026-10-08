// Datadog Browser RUM (layer dd-rum). Off unless the backend /config returns a `rum` block.
// Only synthetic data goes in: the product id and the answer's status, never a shopper name or id.
import { datadogRum } from '@datadog/browser-rum';

export const DEFAULT_EXPOSURE_BUDGET_MS = 800;

let enabled = false;
let budgetMs = DEFAULT_EXPOSURE_BUDGET_MS;
let exposed = false;
const lastSignature = new Map();

export function resetRumForTests() {
  enabled = false;
  budgetMs = DEFAULT_EXPOSURE_BUDGET_MS;
  exposed = false;
  lastSignature.clear();
}

export function isRumEnabled() {
  return enabled;
}

// Idempotent. Returns true when RUM was started by this call.
export function initRum(rum, origin = window.location.origin) {
  if (enabled || !rum) return false;
  for (const k of ['application_id', 'client_token', 'site', 'service', 'env', 'version']) {
    if (!rum[k]) throw new Error(`rum config from /config is missing "${k}"`);
  }
  budgetMs = Number.isFinite(rum.exposure_budget_ms) ? rum.exposure_budget_ms : DEFAULT_EXPOSURE_BUDGET_MS;
  datadogRum.init({
    applicationId: rum.application_id,
    clientToken: rum.client_token,
    site: rum.site,
    service: rum.service,
    env: rum.env,
    version: rum.version,
    sessionSampleRate: 100,
    sessionReplaySampleRate: 0,
    trackUserInteractions: true,
    trackResources: true,
    trackLongTasks: true,
    defaultPrivacyLevel: 'mask-user-input',
    // Same-origin /api/ calls get trace headers so a browser request links to its backend APM trace.
    // `datadog` + `tracecontext` are the two styles ddtrace (Python) extracts by default.
    allowedTracingUrls: [{ match: (url) => String(url).startsWith(`${origin}/api/`), propagatorTypes: ['datadog', 'tracecontext'] }],
    traceSampleRate: 100,
  });
  datadogRum.setGlobalContext({ project: 'dd-demo', stack: rum.stack, layer: 'dd-rum' });
  enabled = true;
  return true;
}

// Exposed journey: the shopper saw an answer that could mislead or was slow.
export function isExposed(answer, lookupMs) {
  return answer.status === 'unknown' || answer.at_least === true || answer.feed !== 'ok' || lookupMs > budgetMs;
}

// Called for every availability answer rendered (lookupMs = browser-measured round trip).
// An action is sent when the (status, at_least, feed, exposed) signature of a product changes, so a 1 s poll
// does not flood RUM; once a session is exposed the global context carries exposed=true for the rest of it.
export function recordStockAnswer(answer, lookupMs) {
  if (!enabled) return;
  const isExp = isExposed(answer, lookupMs);
  const attrs = {
    product_id: answer.product_id, status: answer.status, at_least: Boolean(answer.at_least),
    feed: answer.feed, lookup_ms: Math.round(lookupMs), exposed: isExp, release: answer.release,
  };
  const sig = `${attrs.status}|${attrs.at_least}|${attrs.feed}|${isExp}`;
  if (isExp && !exposed) {
    exposed = true;
    datadogRum.setGlobalContextProperty('exposed', true);
  }
  if (lastSignature.get(answer.product_id) === sig) return;
  lastSignature.set(answer.product_id, sig);
  datadogRum.addAction('stock_answer', attrs);
}

// A failed lookup is itself an exposure (the shopper saw "can't be confirmed").
export function recordStockError(productId, lookupMs) {
  recordStockAnswer({ product_id: productId, status: 'error', at_least: false, feed: 'unknown' }, lookupMs);
}

const restockOffered = (offer) => Boolean(offer.restock_notice ?? offer.restock_eta);

// "Offer accepted": the shopper swapped a sold-out item for the Offer's alternative (not for another option).
// `decided_by` is about the alternative; `restock_offered` says whether the shopper could have waited instead.
export function recordOfferAccepted(offer) {
  if (!enabled || !offer) return;
  datadogRum.addAction('offer_accepted', {
    product_id: offer.original_product_id, alternative_id: offer.product_id,
    decided_by: offer.decision_route === 'JEV' ? 'ai' : 'rule', discount_pct: offer.discount_pct,
    restock_offered: restockOffered(offer),
  });
}

// The shopper confirmed the Restock notice ("Notify me"). No decided_by: the restock date is a fact, not a decision.
export function recordRestockNoticeConfirmed(offer) {
  if (!enabled || !offer) return;
  datadogRum.addAction('restock_notice_confirmed', {
    product_id: offer.original_product_id, restock_eta: offer.restock_eta || null,
    alternative_offered: Boolean(offer.product_id),
  });
}
