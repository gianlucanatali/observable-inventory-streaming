import React from 'react';
import ProductImage from './ProductImage.jsx';

const TYPE_LABEL = {
  SAME_PRODUCT_OTHER_STORE: 'Same product, another store',
  ALTERNATIVE_PRODUCT: 'Alternative product',
  NOTIFY_ME: 'Restock notice',
};

// No eligible alternative and no near restock: the worker published the case without an Offer. The backend says so
// in `no_offer`; the fallback reads the same record fields (offer-worker README, "No Offer").
export function isNoOffer(offer) {
  if (typeof offer.no_offer === 'boolean') return offer.no_offer;
  return offer.chosen_choice === null && !offer.product_id
    && ['no_alternative', 'invalid_choice'].includes(offer.decision_reason);
}

// Jev's choice by name ("Pathfinder Air"); the backend's label, else the raw id.
function choiceLabel(offer) {
  if (offer.jev_choice_label) return offer.jev_choice_label;
  return offer.jev_choice === 'notify_me' ? 'a restock notice' : offer.jev_choice;
}

const score = (value) => Number(value).toFixed(2);
const threshold = (value) => Number(value).toFixed(2).replace(/0$/, '');

export function decisionSummary(offer) {
  const hasJevDecision = typeof offer.jev_choice === 'string' && typeof offer.jev_confidence === 'number';
  const hasThreshold = typeof offer.min_confidence === 'number';
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
  if (offer.decision_reason === 'no_choice') {
    // The worker made no AI call: only one option was left.
    return 'Decided by: safe rule — only one in-stock alternative, no AI choice needed';
  }
  return 'Decided by: safe rule — AI did not return a decision';
}

export default function OfferCard({ offer, error, hasCart = true }) {
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
  return (
    <section className={`offer${isNoOffer(offer) ? ' offer-none' : ''}`} data-testid="offer-card">
      <div className="offer-main">
        {offer.product_id && (
          <div className="offer-product" data-testid="offer-product">
            <ProductImage productId={offer.product_id} alt={`Alternative product ${offer.product_id}`} />
          </div>
        )}
        <div className="offer-details" data-testid="offer-details">
          <h2>{offer.headline}</h2>
          <p className="offer-body">{offer.body}</p>
          {offer.decision_route && <p className="offer-decision" data-testid="offer-decision">{decisionSummary(offer)}</p>}
          {isNoOffer(offer)
            ? <dl className="offer-facts"><div><dt>Offer type</dt><dd>None: nothing comparable in stock</dd></div></dl>
            : (
              <dl className="offer-facts">
                <div><dt>Offer type</dt><dd>{TYPE_LABEL[offer.offer_type] || offer.offer_type}</dd></div>
                {offer.product_id && <div><dt>Product</dt><dd>{offer.product_id}</dd></div>}
                {offer.store_id && <div><dt>Store</dt><dd>{offer.store_id}</dd></div>}
                <div><dt>Discount</dt><dd>{offer.discount_pct}%</dd></div>
              </dl>
            )}
        </div>
      </div>
    </section>
  );
}
