import React, { useEffect, useState } from 'react';
import ProductImage from './ProductImage.jsx';
import { getAlternatives, getAvailability } from '../api.js';
import { aiConfidence, decidedBy, inStockAlternatives } from '../alternatives.js';
import { formatPrice } from './ProductCard.jsx';
import { isNoOffer } from './OfferCard.jsx';

const altTitle = (alt) => [alt.name, alt.colour].filter(Boolean).join(', ');

// One alternative row: name and colour, kind, price, stock. The offered one carries the discount and the AI score.
function AltRow({ alt, offer, offered, stock, onSwap }) {
  const ai = aiConfidence(offer, `alt:${alt.product_id}`);
  const meta = [alt.kind, Number.isFinite(alt.price_eur) ? formatPrice(alt.price_eur) : null,
    Number.isFinite(stock) ? `${stock} in stock` : null].filter(Boolean).join(' · ');
  return (
    <li className={`cart-alt${offered ? ' cart-alt-offered' : ''}`} data-testid={offered ? 'cart-alt-offered' : 'cart-alt'}>
      <div className="cart-alt-body">
        <div className="cart-alt-title">
          {altTitle(alt)}
          {offered && <span className="cart-alt-tag">Offer −{offer.discount_pct}%</span>}
          {ai && <span className="cart-alt-ai">AI: {ai}</span>}
        </div>
        {meta && <div className="cart-item-meta">{meta}</div>}
      </div>
      <button className="cart-remove" onClick={() => onSwap(offer.original_product_id, alt.product_id, offered ? offer : null)}
        aria-label={`Swap to ${altTitle(alt)}`}>Swap</button>
    </li>
  );
}

// Under a sold-out item with no Offer: nothing comparable in stock, so no alternatives to list and no stock calls.
function NoOffer({ offer }) {
  return (
    <div className="cart-offer" data-testid="cart-offer">
      <div className="cart-offer-line" data-testid="cart-offer-line">
        <b>Sold out</b> → No comparable product in stock · Decided by: {decidedBy(offer)}
      </div>
    </div>
  );
}

// Under a sold-out item: the offer line, then the in-stock alternatives the worker considered (offered one first).
function ItemOffer({ offer, onSwap }) {
  const [alts, setAlts] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let stopped = false;
    setAlts(null);
    setError(null);
    inStockAlternatives(offer.original_product_id, { getAlternatives, getAvailability })
      .then((found) => { if (!stopped) setAlts(found); })
      .catch((err) => {
        console.error(err);
        if (!stopped) setError(err.message);
      });
    return () => { stopped = true; };
  }, [offer.offer_id, offer.original_product_id]);

  const offeredId = offer.offer_type === 'ALTERNATIVE_PRODUCT' ? offer.product_id : null;
  const offeredAlt = offeredId ? (offer.alternative || { product_id: offeredId, name: `Product ${offeredId}` }) : null;
  const found = alts || [];
  const offeredStock = (found.find((a) => a.product_id === offeredId) || {}).stock;
  const others = found.filter((a) => a.product_id !== offeredId);
  const notifyAi = aiConfidence(offer, 'notify_me');
  const target = offeredAlt ? `${altTitle(offeredAlt)} −${offer.discount_pct}%` : 'Restock notice: we will let you know when it is back';

  return (
    <div className="cart-offer" data-testid="cart-offer">
      <div className="cart-offer-line" data-testid="cart-offer-line">
        <b>Sold out</b> → {target} · Decided by: {decidedBy(offer)}
        {notifyAi && <span className="cart-alt-ai">AI: restock notice {notifyAi}</span>}
      </div>
      <ul className="cart-alts">
        {offeredAlt && <AltRow alt={offeredAlt} offer={offer} offered stock={offeredStock} onSwap={onSwap} />}
        {others.map((a) => <AltRow key={a.product_id} alt={a} offer={offer} offered={false} stock={a.stock} onSwap={onSwap} />)}
      </ul>
      {alts === null && !error && <div className="cart-item-meta">Checking other options…</div>}
      {error && <div className="cart-item-meta">Other options unavailable: {error}</div>}
    </div>
  );
}

// Small panel under the header cart button: one line per product with its size, colour and quantity, and under each
// sold-out product its offer and alternatives.
export default function CartDrawer({ cart, offers = [], onRemove, onSwap, onClose }) {
  return (
    <aside className="cart-drawer" data-testid="cart-drawer" aria-label="Your cart">
      <div className="cart-drawer-head">
        <h2>Your cart</h2>
        <button className="toast-close" onClick={onClose} aria-label="Close cart">×</button>
      </div>
      {cart.items.length === 0
        ? <p className="muted">Your cart is empty.</p>
        : (
          <ul className="cart-items">
            {cart.items.map((item) => {
              const title = item.name ? `${item.brand} ${item.name}` : `Product ${item.product_id}`;
              const offer = offers.find((o) => o.original_product_id === item.product_id);
              return (
                <li key={item.product_id} className="cart-item" data-testid="cart-item">
                  <ProductImage productId={item.product_id} alt="" />
                  <div className="cart-item-body">
                    <a href={`#/product/${item.product_id}`} onClick={onClose}>{title}</a>
                    <div className="cart-item-meta">
                      {[item.size, item.colour, `Qty ${item.quantity}`].filter(Boolean).join(' · ')}
                    </div>
                  </div>
                  <button className="cart-remove" onClick={() => onRemove(item.product_id)} aria-label={`Remove ${title}`}>Remove</button>
                  {offer && onSwap && (isNoOffer(offer) ? <NoOffer offer={offer} /> : <ItemOffer offer={offer} onSwap={onSwap} />)}
                </li>
              );
            })}
          </ul>
        )}
    </aside>
  );
}
