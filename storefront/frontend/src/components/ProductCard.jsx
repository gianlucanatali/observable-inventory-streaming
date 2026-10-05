import React from 'react';

export const formatPrice = (eur) => `€${eur.toFixed(2).replace('.', ',')}`;

// A grid card. Display data only: the home page never asks for stock.
export default function ProductCard({ product, featured }) {
  return (
    <a className={`card${featured ? ' card-featured' : ''}`} href={`#/product/${product.product_id}`} data-testid="product-card">
      <img src={`/img/${product.product_id}.svg`} alt={`${product.brand} ${product.name}`} loading="lazy" />
      <div className="card-body">
        <div className="brand-name">{product.brand}</div>
        <div className="card-name">{product.name}</div>
        <div className="card-price">{formatPrice(product.price_eur)}</div>
      </div>
    </a>
  );
}
