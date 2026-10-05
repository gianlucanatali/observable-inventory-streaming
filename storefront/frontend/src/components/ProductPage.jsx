import React, { useEffect, useRef, useState } from 'react';
import { getProduct, getAvailability, postDisplayBeacon } from '../api.js';
import { usePolling } from '../hooks.js';
import StockPanel from './StockPanel.jsx';
import { recordStockAnswer, recordStockError } from '../rum.js';
import { formatPrice } from './ProductCard.jsx';

// One product, sold online. This is the only place that polls availability, and only for this product.
export default function ProductPage({ productId, config, cart, onCart, children }) {
  const [product, setProduct] = useState(null);
  const [productError, setProductError] = useState(null);
  const [stock, setStock] = useState(null);
  const [stockError, setStockError] = useState(null);
  // last_changed_at of the sellable answer last committed to the DOM; null until the first answer.
  const lastChanged = useRef(null);

  useEffect(() => {
    setProduct(null);
    setProductError(null);
    getProduct(productId).then(setProduct).catch((err) => {
      console.error(err);
      setProductError(err.message);
    });
  }, [productId]);

  useEffect(() => {
    setStock(null);
    setStockError(null);
    lastChanged.current = null;
  }, [productId]);

  usePolling(async () => {
    const t0 = performance.now();
    try {
      const s = await getAvailability(productId);
      recordStockAnswer(s, performance.now() - t0);
      return s;
    } catch (err) {
      recordStockError(productId, performance.now() - t0);
      throw err;
    }
  }, config ? config.poll_ms : 1000, [productId], {
    enabled: Boolean(config),
    onResult: (s) => {
      setStock(s);
      setStockError(null);
    },
    onError: (err) => {
      console.error(err);
      setStockError(err.message);
    },
  });

  // Display-delay beacon: runs after React committed a new sellable answer (changed last_changed_at) to the DOM.
  // The backend measures the delay from last_changed_at to its own receive time, so no browser timestamp is sent.
  useEffect(() => {
    if (!stock || !stock.last_changed_at || stock.product_id !== productId) return;
    const seen = lastChanged.current;
    if (seen === stock.last_changed_at) return;
    lastChanged.current = stock.last_changed_at;
    // The first answer for a product is not a change the shopper watched appear.
    if (seen === null) return;
    postDisplayBeacon({ product_id: stock.product_id, last_changed_at: stock.last_changed_at })
      .catch((err) => console.error('display beacon failed', err));
  }, [stock, productId]);

  const canAdd = stock && stock.status === 'available';

  return (
    <>
      <nav className="crumbs"><a href="#/">← All products</a>{product && <span> / {product.category}</span>}</nav>
      {productError && <div className="banner banner-error">Cannot load product {productId}: {productError}</div>}
      <section className="product">
        <div className="product-image">
          {product ? <img src={`/img/${productId}.svg`} alt={`${product.brand} ${product.name}`} /> : <div className="placeholder" />}
        </div>
        <div className="product-body">
          {product && <div className="brand-name">{product.brand}</div>}
          <h1>{product ? product.name : `Product ${productId}`}</h1>
          {product && (
            <>
              <div className="price">{formatPrice(product.price_eur)}</div>
              <div className="product-meta">Size {product.size} · {product.colour.name}</div>
              <p className="product-desc">{product.description}</p>
            </>
          )}
          <h2 className="avail-title">Availability online</h2>
          <StockPanel stock={stock} fetchError={stockError} compression={config ? config.time_compression : null} />
          <div className="actions">
            <button className="primary" onClick={() => onCart('ADD', productId)} disabled={!canAdd}>Add to cart</button>
            <button className="debug" onClick={() => onCart('ABANDON', productId)} disabled={!cart.id}>Abandon cart (debug)</button>
          </div>
        </div>
      </section>
      {children}
      <footer className="foot">
        Serving release {stock && stock.release ? stock.release : '–'}
      </footer>
    </>
  );
}
