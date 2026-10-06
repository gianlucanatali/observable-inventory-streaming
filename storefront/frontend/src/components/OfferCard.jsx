import React from 'react';
import ProductImage from './ProductImage.jsx';

const TYPE_LABEL = {
  SAME_PRODUCT_OTHER_STORE: 'Same product, another store',
  ALTERNATIVE_PRODUCT: 'Alternative product',
  NOTIFY_ME: 'Notify me when back',
};

function choiceLabel(choice) {
  return choice === 'notify_me' ? 'notify me' : choice;
}

function confidence(value) {
  return Number(value).toFixed(2).replace(/0$/, '');
}

export function decisionSummary(offer) {
  const hasJevDecision = typeof offer.jev_choice === 'string' && typeof offer.jev_confidence === 'number';
  const hasThreshold = typeof offer.min_confidence === 'number';
  if (offer.decision_route === 'JEV' && hasJevDecision && hasThreshold) {
    return `Decided by: AI — selected ${choiceLabel(offer.jev_choice)} at ${confidence(offer.jev_confidence)}, meeting ${confidence(offer.min_confidence)}`;
  }
  if (hasJevDecision && hasThreshold && offer.decision_reason === 'low_confidence') {
    return `Decided by: safe rule — AI suggested ${choiceLabel(offer.jev_choice)} at ${confidence(offer.jev_confidence)}, below ${confidence(offer.min_confidence)}`;
  }
  return 'Decided by: safe rule — AI did not return a decision';
}

export default function OfferCard({ offer, error }) {
  if (error) {
    return (
      <section className="offer offer-error">
        <h2>Offer</h2>
        <p>Offers are temporarily unavailable: {error}</p>
      </section>
    );
  }
  if (!offer) {
    return (
      <section className="offer offer-empty">
        <h2>Offer</h2>
        <p>No offer for this cart right now.</p>
      </section>
    );
  }
  return (
    <section className="offer" data-testid="offer-card">
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
          <dl className="offer-facts">
            <div><dt>Offer type</dt><dd>{TYPE_LABEL[offer.offer_type] || offer.offer_type}</dd></div>
            {offer.product_id && <div><dt>Product</dt><dd>{offer.product_id}</dd></div>}
            {offer.store_id && <div><dt>Store</dt><dd>{offer.store_id}</dd></div>}
            <div><dt>Discount</dt><dd>{offer.discount_pct}%</dd></div>
          </dl>
        </div>
      </div>
    </section>
  );
}
