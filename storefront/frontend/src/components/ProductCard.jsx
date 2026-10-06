import React from 'react';
import ProductImage from './ProductImage.jsx';

export const formatPrice = (eur) => `€${eur.toFixed(2).replace('.', ',')}`;

// A grid card for one model (see variants.js groupModels). Display data only: the home page never asks for stock.
export default function ProductCard({ model, featured }) {
  const { lead, colours } = model;
  return (
    <a className={`card${featured ? ' card-featured' : ''}`} href={`#/product/${lead.product_id}`} data-testid="product-card">
      <ProductImage productId={lead.product_id} alt={`${model.brand} ${model.name}`} loading="lazy" />
      <div className="card-body">
        <div className="brand-name">{model.brand}</div>
        <div className="card-name">{model.name}</div>
        <div className="card-swatches" data-testid="card-swatches" aria-label={`${colours.length} colours: ${colours.map((c) => c.name).join(', ')}`}>
          {colours.map((c) => <span key={c.name} className="dot" style={{ background: c.hex }} title={c.name} />)}
          <span className="card-colours muted">{colours.length} colours</span>
        </div>
        <div className="card-price">{formatPrice(model.price_eur)}</div>
      </div>
    </a>
  );
}
