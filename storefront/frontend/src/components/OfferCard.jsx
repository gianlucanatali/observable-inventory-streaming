import React from 'react';
import ProductImage from './ProductImage.jsx';
import { formatBusinessDuration } from '../stockView.js';

// The Offer has two independent parts: the alternative (`product_id`, picked by the AI or the safe rule) and the
// Restock notice (`restock_eta`, a fact from the open purchase order). The backend says which are present in
// `restock_notice` and `no_offer`; the fallbacks read the same record fields (offer-worker README, "Card states").
export function hasRestockNotice(offer) {
  if (typeof offer.restock_notice === 'boolean') return offer.restock_notice;
  return Boolean(offer.restock_eta) || offer.chosen_choice === 'notify_me';  // notify_me: an older record
}

// Neither part: no eligible alternative (or none the AI judged a good substitute) and no near restock.
export function isNoOffer(offer) {
  if (typeof offer.no_offer === 'boolean') return offer.no_offer;
  return !offer.product_id && !hasRestockNotice(offer);
}

// "Back in about 17 hours": the time left to the purchase order's due time, in business time (demo clock).
export function restockWait(offer, compression, now = Date.now()) {
  const t = Date.parse(offer.restock_eta || '');
  if (Number.isNaN(t) || !Number.isFinite(compression) || compression <= 0) return 'Back in stock soon';
  return t <= now ? 'Back any moment now' : `Back in ${formatBusinessDuration((t - now) * compression)}`;
}

// "Pathfinder Air in Ember red": the colour tells the same model in another colour apart.
export function alternativeName(offer) {
  const alt = offer.alternative;
  if (!alt) return `product ${offer.product_id}`;
  return alt.colour ? `${alt.name} in ${alt.colour}` : alt.name;
}

function offerKind(offer) {
  const restock = hasRestockNotice(offer);
  if (offer.product_id) return restock ? 'Alternative or restock notice' : 'Alternative product';
  if (restock) return 'Restock notice';
  return offer.decision_reason === 'no_good_substitute' ? 'None: no good substitute' : 'None: nothing comparable in stock';
}

// Jev's choice by name ("Pathfinder Air"); the backend's label, else the raw id.
function choiceLabel(offer) {
  if (offer.jev_choice_label) return offer.jev_choice_label;
  if (offer.jev_choice === 'none') return 'no substitute';
  return offer.jev_choice === 'notify_me' ? 'a restock notice' : offer.jev_choice;
}

const score = (value) => Number(value).toFixed(2);
const threshold = (value) => Number(value).toFixed(2).replace(/0$/, '');

// "Decided by" is about the alternative part only: the Restock notice is a fact, never decided.
export function decisionSummary(offer) {
  const hasJevDecision = typeof offer.jev_choice === 'string' && typeof offer.jev_confidence === 'number';
  const hasThreshold = typeof offer.min_confidence === 'number';
  if (offer.decision_route === 'JEV' && hasJevDecision && hasThreshold && offer.decision_reason === 'no_good_substitute') {
    return `Decided by: AI — no good substitute (confidence ${score(offer.jev_confidence)}, threshold ${threshold(offer.min_confidence)})`;
  }
  if (offer.decision_route === 'JEV' && hasJevDecision && hasThreshold) {
    return `Decided by: AI — chose ${choiceLabel(offer)} (confidence ${score(offer.jev_confidence)}, threshold ${threshold(offer.min_confidence)})`;
  }
  if (hasJevDecision && hasThreshold && offer.decision_reason === 'low_confidence') {
    return `Decided by: safe rule — AI suggested ${choiceLabel(offer)} (confidence ${score(offer.jev_confidence)}, below threshold ${threshold(offer.min_confidence)})`;
  }
  if (offer.decision_reason === 'no_alternative') {
    // No AI call: there was nothing comparable to choose from.
    return 'Decided by: safe rule, no eligible alternative';
  }
  return 'Decided by: safe rule — AI did not return a decision';
}

// The card's four states: alternative and Restock notice (the Shopper chooses), alternative only, Restock notice only,
// or neither. `onSwap(fromId, toId, offer)` takes the alternative; `onNotify(offer)` confirms the Restock notice.
export default function OfferCard({ offer, error, hasCart = true, compression = null, onSwap, onNotify,
  notified = false }) {
  if (error) {
    return (
      <section className="offer offer-error">
        <h2>Offer</h2>
        <p>Offers are temporarily unavailable: {error}</p>
      </section>
    );
  }
  if (!hasCart) {
    return (
      <section className="offer offer-empty">
        <h2>Offer</h2>
        <p>Add something to your cart to see offers.</p>
      </section>
    );
  }
  if (!offer) {
    return (
      <section className="offer offer-empty">
        <h2>Offer</h2>
        <p>No offer for this product right now.</p>
      </section>
    );
  }
  const none = isNoOffer(offer);
  const restock = hasRestockNotice(offer);
  const alt = Boolean(offer.product_id);
  return (
    <section className={`offer${none ? ' offer-none' : ''}`} data-testid="offer-card">
      <div className="offer-main">
        {alt && (
          <div className="offer-product" data-testid="offer-product">
            <ProductImage productId={offer.product_id} alt={`Alternative product ${offer.product_id}`} />
          </div>
        )}
        <div className="offer-details" data-testid="offer-details">
          <h2>{offer.headline}</h2>
          {restock && (
            <div className="offer-action" data-testid="offer-restock">
              <span>{restockWait(offer, compression)}</span>
              {notified
                ? <span className="offer-notified">We will let you know.</span>
                : <button className="offer-button" onClick={() => onNotify && onNotify(offer)}>Notify me</button>}
            </div>
          )}
          {alt && (
            <>
              <p className="offer-body">{offer.body}</p>
              <div className="offer-action" data-testid="offer-switch">
                <button className="offer-button"
                  onClick={() => onSwap && onSwap(offer.original_product_id, offer.product_id, offer)}>
                  {restock ? 'Or switch' : 'Switch'} to {alternativeName(offer)}, {offer.discount_pct}% off
                </button>
              </div>
            </>
          )}
          {none && <p className="offer-body">{offer.body}</p>}
          {offer.decision_route && <p className="offer-decision" data-testid="offer-decision">{decisionSummary(offer)}</p>}
          <dl className="offer-facts">
            <div><dt>Offer type</dt><dd>{offerKind(offer)}</dd></div>
            {alt && <div><dt>Product</dt><dd>{offer.product_id}</dd></div>}
            {offer.store_id && <div><dt>Store</dt><dd>{offer.store_id}</dd></div>}
            {alt && <div><dt>Discount</dt><dd>{offer.discount_pct}%</dd></div>}
          </dl>
        </div>
      </div>
    </section>
  );
}
