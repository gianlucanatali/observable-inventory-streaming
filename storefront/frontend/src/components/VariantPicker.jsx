import React from 'react';
import { coloursOf, sizeState, variantForColour } from '../variants.js';

const go = (productId) => { window.location.hash = `#/product/${productId}`; };

// Colour swatches and size buttons of one model. Every variant is its own SKU page (#/product/<id>).
// `selectedState` is the live state of the selected SKU; `siblingStock` maps a sibling id to { answer, error }.
export default function VariantPicker({ product, selectedState, siblingStock }) {
  const variants = product.variants;
  const colours = coloursOf(variants);
  const sizes = variants.filter((v) => v.colour.name === product.colour.name);
  const states = sizes.map((v) => (v.product_id === product.product_id
    ? selectedState
    : sizeState(siblingStock[v.product_id] && siblingStock[v.product_id].answer, siblingStock[v.product_id] && siblingStock[v.product_id].error)));
  const anySoldOut = states.includes('sold_out');

  return (
    <div className="variants" data-testid="variant-picker">
      <div className="variant-label">Colour: <strong>{product.colour.name}</strong></div>
      <div className="swatches" role="group" aria-label="Colour">
        {colours.map((c) => {
          const on = c.name === product.colour.name;
          return (
            <button key={c.name} type="button" className={`swatch${on ? ' swatch-on' : ''}`} aria-pressed={on}
              aria-label={c.name} title={c.name} data-testid="colour-swatch"
              onClick={() => { if (!on) go(variantForColour(variants, c.name, product.size).product_id); }}>
              <span className="swatch-fill" style={{ background: c.hex }} />
            </button>
          );
        })}
      </div>
      <div className="variant-label">Size: <strong>{product.size}</strong></div>
      <div className="sizes" role="group" aria-label="Size">
        {sizes.map((v, i) => {
          const on = v.product_id === product.product_id;
          const soldOut = states[i] === 'sold_out';
          return (
            <button key={v.product_id} type="button" data-testid="size-option" data-state={states[i]}
              className={`size${on ? ' size-on' : ''}${soldOut ? ' size-sold-out' : ''}`}
              aria-pressed={on} disabled={soldOut && !on}
              aria-label={soldOut ? `${v.size}, sold out online` : v.size} title={soldOut ? 'Sold out online' : undefined}
              onClick={() => { if (!on) go(v.product_id); }}>
              {v.size}
            </button>
          );
        })}
      </div>
      {anySoldOut && <div className="variant-note muted">Crossed-out sizes are sold out online.</div>}
    </div>
  );
}
