// Pure helpers for models and their variants. A product id (P0042) is a SKU: one model in one colour and size.

// Group the SKU list of /api/products into models, in catalogue order of each model's first SKU.
// The lead SKU (the card's photo and link) is `featuredId` when the model has it, else the middle size of the
// model's first colour.
export function groupModels(products, featuredId) {
  const byModel = new Map();
  for (const p of products) {
    if (typeof p.model_id !== 'string') throw new Error(`catalogue entry ${p.product_id} has no model_id`);
    if (!byModel.has(p.model_id)) byModel.set(p.model_id, []);
    byModel.get(p.model_id).push(p);
  }
  return [...byModel.entries()].map(([modelId, skus]) => {
    const ordered = [...skus].sort((a, b) => a.variant_rank - b.variant_rank);
    const colours = coloursOf(ordered);
    const firstColour = ordered.filter((p) => p.colour.name === colours[0].name);
    const lead = ordered.find((p) => p.product_id === featuredId) || firstColour[Math.floor((firstColour.length - 1) / 2)];
    return {
      model_id: modelId, brand: lead.brand, name: lead.name, category: lead.category, description: lead.description,
      price_eur: Math.min(...ordered.map((p) => p.price_eur)), colours, lead, skus: ordered,
    };
  });
}

// Distinct colours in variant order: [{ name, hex }].
export function coloursOf(variants) {
  const seen = new Map();
  for (const v of variants) if (!seen.has(v.colour.name)) seen.set(v.colour.name, v.colour);
  return [...seen.values()];
}

// The variant to open when the shopper picks `colourName`: the same size when that colour has it,
// otherwise the nearest size in that colour's run (the smaller one on a tie).
export function variantForColour(variants, colourName, currentSize) {
  const inColour = variants.filter((v) => v.colour.name === colourName);
  if (inColour.length === 0) throw new Error(`no variant in colour ${colourName}`);
  const same = inColour.find((v) => v.size === currentSize);
  if (same) return same;
  const order = variants.map((v) => v.size);
  const rank = (size) => sizeRank(size, order);
  const target = rank(currentSize);
  return inColour.reduce((best, v) => (Math.abs(rank(v.size) - target) < Math.abs(rank(best.size) - target) ? v : best));
}

const LETTER_SIZES = ['XS', 'S', 'M', 'L', 'XL'];

// Comparable position of a size: EU numbers, letter sizes, else its first position in `fallbackOrder`.
export function sizeRank(size, fallbackOrder = []) {
  const eu = /^EU (\d+)$/.exec(size);
  if (eu) return Number(eu[1]);
  const letter = LETTER_SIZES.indexOf(size);
  if (letter >= 0) return letter;
  return fallbackOrder.indexOf(size);
}

// What a size button shows for one sibling's availability answer (or fetch error, or nothing yet).
// Only a confirmed out_of_stock disables the size: unknown or not-yet-loaded is never shown as sold out.
export function sizeState(answer, fetchError) {
  if (fetchError || !answer) return 'unknown';
  if (answer.status === 'out_of_stock') return 'sold_out';
  if (answer.status === 'available' && answer.sellable === 0) return 'sold_out';
  if (answer.status === 'available') return 'available';
  return 'unknown';
}
