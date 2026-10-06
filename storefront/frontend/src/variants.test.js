import { describe, expect, it } from 'vitest';
import { coloursOf, groupModels, sizeRank, sizeState, variantForColour } from './variants.js';

const green = { name: 'Forest green', hex: '#2f6b4f' };
const slate = { name: 'Slate', hex: '#5a6678' };
const sku = (product_id, colour, size, variant_rank, over = {}) => ({
  product_id, colour, size, variant_rank, model_id: 'alpenpace-trailrunner-gtx', brand: 'Alpenpace',
  name: 'Trailrunner GTX', category: 'footwear', description: 'd', price_eur: 219.9, ...over,
});
const TRAIL = [
  sku('P0007', slate, 'EU 37', 3), sku('P0001', green, 'EU 41', 0), sku('P0042', green, 'EU 42', 1),
  sku('P0002', green, 'EU 43', 2), sku('P0008', slate, 'EU 38', 4),
];
const CAP = [sku('P0101', green, 'One size', 0, { model_id: 'vetta-trail-cap-air', name: 'Trail Cap Air', price_eur: 24.9 }),
  sku('P0102', slate, 'One size', 1, { model_id: 'vetta-trail-cap-air', name: 'Trail Cap Air', price_eur: 24.9 })];

describe('groupModels', () => {
  it('groups SKUs by model in variant order and is deterministic', () => {
    const models = groupModels([...TRAIL, ...CAP], 'P0042');
    expect(models.map((m) => m.model_id)).toEqual(['alpenpace-trailrunner-gtx', 'vetta-trail-cap-air']);
    expect(models[0].skus.map((s) => s.product_id)).toEqual(['P0001', 'P0042', 'P0002', 'P0007', 'P0008']);
    expect(models[0].colours.map((c) => c.name)).toEqual(['Forest green', 'Slate']);
    expect(groupModels([...TRAIL, ...CAP], 'P0042')).toEqual(models);
  });

  it('leads with the featured SKU, else the middle size of the first colour', () => {
    expect(groupModels(TRAIL, 'P0042')[0].lead.product_id).toBe('P0042');
    expect(groupModels(TRAIL, 'P9999')[0].lead.product_id).toBe('P0042'); // EU 41, 42, 43 -> 42
    expect(groupModels(CAP, 'P0042')[0].lead.product_id).toBe('P0101');
  });

  it('fails loudly on an entry without a model', () => {
    expect(() => groupModels([{ product_id: 'P0001' }], 'P0042')).toThrow(/P0001 has no model_id/);
  });
});

describe('variantForColour', () => {
  const variants = [...TRAIL].sort((a, b) => a.variant_rank - b.variant_rank);
  it('keeps the size when the colour has it, else picks the nearest size', () => {
    expect(variantForColour(variants, 'Forest green', 'EU 43').product_id).toBe('P0002');
    expect(variantForColour(variants, 'Slate', 'EU 42').product_id).toBe('P0008');
    expect(variantForColour(variants, 'Forest green', 'EU 38').product_id).toBe('P0001');
    expect(() => variantForColour(variants, 'Navy', 'EU 42')).toThrow(/Navy/);
  });
  it('orders sizes by EU number and letter size', () => {
    expect(sizeRank('EU 42')).toBe(42);
    expect(sizeRank('XS')).toBeLessThan(sizeRank('XL'));
    expect(coloursOf(variants)).toEqual([green, slate]);
  });
});

describe('sizeState', () => {
  it('disables only a confirmed sold-out size', () => {
    expect(sizeState({ status: 'out_of_stock', sellable: 0 }, null)).toBe('sold_out');
    expect(sizeState({ status: 'available', sellable: 0 }, null)).toBe('sold_out');
    expect(sizeState({ status: 'available', sellable: 4 }, null)).toBe('available');
    expect(sizeState({ status: 'unknown', sellable: null }, null)).toBe('unknown');
    expect(sizeState(null, null)).toBe('unknown');
    expect(sizeState(null, 'HTTP 503')).toBe('unknown');
  });
});
