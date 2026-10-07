import React, { useEffect, useRef, useState } from 'react';
import { getProduct, getAvailability, postDisplayBeacon } from '../api.js';
import { usePolling } from '../hooks.js';
import StockPanel from './StockPanel.jsx';
import ProductImage from './ProductImage.jsx';
import { recordStockAnswer, recordStockError } from '../rum.js';
import { formatPrice } from './ProductCard.jsx';
import VariantPicker from './VariantPicker.jsx';
import { sizeState } from '../variants.js';

// Sibling sizes (same model and colour) are checked once on arrival and then at this slower pace, never per poll:
// at most one request per size in the colour's run, and they are not RUM stock answers (not what the shopper reads).
export const SIBLING_REFRESH_MS = 10_000;

// One SKU, sold online, with its model's colour and size variants. This is the only place that asks for availability:
// the selected SKU every poll, its sibling sizes every SIBLING_REFRESH_MS.
export default function ProductPage({ productId, config, cart, onCart, children }) {
  const [product, setProduct] = useState(null);
  const [productError, setProductError] = useState(null);
  const [stock, setStock] = useState(null);
  const [stockError, setStockError] = useState(null);
  const [siblingStock, setSiblingStock] = useState({});
  // last_changed_at of the sellable answer last committed to the DOM; null until the first answer.
  const lastChanged = useRef(null);

  useEffect(() => {
    setProduct(null);
    setProductError(null);
    getProduct(productId).then((p) => {
      if (!Array.isArray(p.variants) || !p.variants.some((v) => v.product_id === p.product_id)) {
        throw new Error(`GET /api/products/${productId} has no variants list that includes it`);
      }
      setProduct(p);
    }).catch((err) => {
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

  const siblings = product
    ? product.variants.filter((v) => v.colour.name === product.colour.name && v.product_id !== productId).map((v) => v.product_id)
    : [];
  usePolling(async () => {
    const settled = await Promise.allSettled(siblings.map((id) => getAvailability(id)));
    return Object.fromEntries(siblings.map((id, i) => [id, settled[i].status === 'fulfilled'
      ? { answer: settled[i].value, error: null }
      : { answer: null, error: settled[i].reason.message }]));
  }, SIBLING_REFRESH_MS, [productId, siblings.join(',')], {
    enabled: Boolean(config) && siblings.length > 0,
    onResult: (r) => {
      Object.entries(r).forEach(([id, s]) => { if (s.error) console.error(`sibling size ${id} availability failed: ${s.error}`); });
      setSiblingStock(r);
    },
    onError: (err) => console.error(err),
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
          {product ? <ProductImage productId={productId} alt={`${product.brand} ${product.name}`} /> : <div className="placeholder" />}
        </div>
        <div className="product-body">
          {product && <div className="brand-name">{product.brand}</div>}
          <h1>{product ? product.name : `Product ${productId}`}</h1>
          {product && (
            <>
              <div className="price">{formatPrice(product.price_eur)}</div>
              <div className="product-meta" data-testid="product-sku">Item {product.product_id}</div>
              {product.kind && (
                <div className="product-meta" data-testid="product-attributes">
                  {product.kind.charAt(0).toUpperCase() + product.kind.slice(1)}{product.waterproof ? ' · Waterproof' : ''}
                </div>
              )}
              <p className="product-desc">{product.description}</p>
              <VariantPicker product={product} siblingStock={siblingStock}
                selectedState={sizeState(stock && stock.product_id === productId ? stock : null, stockError)} />
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
