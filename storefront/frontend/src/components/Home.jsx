import React, { useEffect, useState } from 'react';
import { getProducts } from '../api.js';
import ProductCard from './ProductCard.jsx';
import ProductImage from './ProductImage.jsx';
import { groupModels } from '../variants.js';

export const FEATURED = 'P0042';
const PAGE_SIZE = 24;
const CATEGORIES = [
  { id: 'all', label: 'All' },
  { id: 'footwear', label: 'Footwear' },
  { id: 'apparel', label: 'Apparel' },
  { id: 'accessories', label: 'Accessories' },
];

export default function Home() {
  const [products, setProducts] = useState(null);
  const [error, setError] = useState(null);
  const [category, setCategory] = useState('all');
  const [shown, setShown] = useState(PAGE_SIZE);

  useEffect(() => {
    // Grouping fails loudly (catch below) when an entry has no model_id.
    getProducts().then((items) => setProducts({ items, models: groupModels(items, FEATURED) })).catch((err) => {
      console.error(err);
      setError(err.message);
    });
  }, []);

  if (error) return <div className="banner banner-error">Cannot load the catalogue: {error}</div>;
  if (!products) return <p className="muted">Loading the catalogue…</p>;

  const { models } = products;
  const featured = products.items.find((p) => p.product_id === FEATURED);
  const featuredModel = models.find((m) => m.lead.product_id === FEATURED);
  const filtered = models.filter((m) => category === 'all' || m.category === category);
  // The featured model leads the list whenever it matches the filter.
  const ordered = featuredModel && filtered.includes(featuredModel)
    ? [featuredModel, ...filtered.filter((m) => m !== featuredModel)] : filtered;
  const visible = ordered.slice(0, shown);

  return (
    <>
      {featured && (
        <a className="hero" href={`#/product/${featured.product_id}`} data-testid="hero">
          <div className="hero-text">
            <div className="hero-kicker">Only a few pairs left in Italy</div>
            <h2>{featured.brand} {featured.name}</h2>
            <p>{featured.description}</p>
            <span className="hero-cta">See the {featured.name} →</span>
          </div>
          <ProductImage productId={featured.product_id} alt={`${featured.brand} ${featured.name}`} />
        </a>
      )}
      <div className="chips" role="tablist" aria-label="Category">
        {CATEGORIES.map((c) => (
          <button key={c.id} role="tab" aria-selected={category === c.id} className={`chip${category === c.id ? ' chip-on' : ''}`}
            onClick={() => { setCategory(c.id); setShown(PAGE_SIZE); }}>{c.label}</button>
        ))}
      </div>
      <div className="grid">
        {visible.map((m) => <ProductCard key={m.model_id} model={m} featured={m === featuredModel} />)}
      </div>
      {shown < ordered.length && (
        <div className="more"><button onClick={() => setShown(shown + PAGE_SIZE)}>Show more ({ordered.length - shown} left)</button></div>
      )}
    </>
  );
}
