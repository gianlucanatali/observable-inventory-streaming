// In-stock alternatives for a sold-out cart item, with the offer-worker's rule: the backend lists the comparable
// products in the worker's order (GET /api/products/<id>/alternatives, mirror of offer-worker policy.py), and the shop
// keeps the first `max_alternatives` that the availability API confirms in stock. Checked one by one, in order, so a
// normal cart costs two or three availability calls.

// Same promise as the worker's confirmed minimum: only a confirmed quantity above 0 counts; unknown never does.
export function confirmedStock(answer) {
  if (!answer) return 0;
  if (Number.isFinite(answer.confirmed_min)) return answer.confirmed_min;
  return answer.status === 'available' && !answer.at_least && Number.isFinite(answer.sellable) ? answer.sellable : 0;
}

export async function inStockAlternatives(productId, { getAlternatives, getAvailability }) {
  const pool = await getAlternatives(productId);
  if (!pool || !Array.isArray(pool.candidates) || !Number.isInteger(pool.max_alternatives)) {
    throw new Error(`GET /api/products/${productId}/alternatives has no candidates list and max_alternatives`);
  }
  const found = [];
  for (const candidate of pool.candidates) {
    if (found.length >= pool.max_alternatives) break;
    let stock = 0;
    try {
      stock = confirmedStock(await getAvailability(candidate.product_id));
    } catch (err) {
      // Like the worker: a stock answer that cannot be read makes the candidate not eligible.
      console.error(`alternative ${candidate.product_id} for ${productId}: availability failed, not offered`, err);
    }
    if (stock > 0) found.push({ ...candidate, stock });
  }
  return found;
}

// "AI" when Jev's choice was accepted, otherwise "safe rule".
export const decidedBy = (offer) => (offer.decision_route === 'JEV' ? 'AI' : 'safe rule');

// Jev's confidence for one choice id ("alt:P0059", "notify_me"), or null when Jev did not pick it.
export function aiConfidence(offer, choiceId) {
  if (offer.jev_choice !== choiceId || typeof offer.jev_confidence !== 'number') return null;
  return Number(offer.jev_confidence).toFixed(2);
}
