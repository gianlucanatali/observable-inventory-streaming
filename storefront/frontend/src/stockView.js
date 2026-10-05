// Pure mapping from an availability answer (contracts section 5) to what the shopper sees.
// "unknown" is never rendered as zero or as available.

export const STORES = [
  { id: 'S01', name: 'Milano Centrale', short: 'Milano' },
  { id: 'S02', name: 'Torino Porta Nuova', short: 'Torino' },
  { id: 'S03', name: 'Bologna Centro', short: 'Bologna' },
  { id: 'S04', name: 'Roma Termini', short: 'Roma' },
  { id: 'S05', name: 'Firenze SMN', short: 'Firenze' },
];

const UNKNOWN_TITLE = "Availability can't be confirmed right now";

// One entry of the "By store" line: the number is shown only when the store's position is known.
export function describeStore(entry) {
  const store = STORES.find((s) => s.id === entry.store_id);
  const name = store ? store.short : entry.store_id;
  const notLive = entry.feed !== 'ok';
  let value;
  switch (entry.status) {
    case 'available':
    case 'out_of_stock':
      value = String(entry.quantity);
      break;
    case 'not_stocked':
      value = 'not stocked';
      break;
    default:
      value = '?';
  }
  return {
    id: entry.store_id,
    name,
    value,
    tone: entry.status === 'unknown' ? 'unknown' : notLive ? 'stale' : 'ok',
    text: `${name} ${value}${notLive && entry.status !== 'unknown' ? ' (not live)' : ''}`,
  };
}

// Business duration phrase for the demo clock: "about 2 days", "about 5 hours", "about 30 minutes".
export function formatBusinessDuration(businessMs) {
  if (businessMs <= 0) return 'any moment now';
  const min = Math.round(businessMs / 60_000);
  if (min < 1) return 'less than a minute';
  if (min < 60) return `about ${min} minute${min === 1 ? '' : 's'}`;
  const h = Math.round(min / 60);
  if (h < 48) return `about ${h} hour${h === 1 ? '' : 's'}`;
  return `about ${Math.round(h / 24)} days`;
}

// Demo clock C (business seconds per real second) as a note: 60 -> "demo clock: 1 min = 1 h". null at C = 1.
export function describeClock(c) {
  if (!Number.isFinite(c) || c <= 0 || c === 1) return null;
  const businessMin = c; // business minutes per real minute
  let right;
  if (businessMin < 60) right = `${+businessMin.toFixed(1)} min`;
  else if (businessMin < 1440) right = `${+(businessMin / 60).toFixed(1)} h`;
  else { const d = +(businessMin / 1440).toFixed(1); right = `${d} ${d === 1 ? 'day' : 'days'}`; }
  return `demo clock: 1 min = ${right}`;
}

// "Back in stock in about 2 days": the real time left (ETA - now) times C, shown as business time.
// null when there is no (valid) ETA or no valid demo clock.
export function describeRestock(iso, compression, now = Date.now()) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  if (!Number.isFinite(compression) || compression <= 0) return null;
  const phrase = formatBusinessDuration((t - now) * compression);
  return t - now <= 0 ? `Back in stock ${phrase}` : `Back in stock in ${phrase}`;
}

export function describeStock(answer, fetchError, compression = null) {
  if (fetchError) {
    return {
      tone: 'unknown',
      title: UNKNOWN_TITLE,
      detail: 'The stock service did not answer. This is not the same as out of stock.',
      qualifier: null,
      stores: [],
    };
  }
  if (!answer) {
    return { tone: 'loading', title: 'Checking availability…', detail: null, qualifier: null, stores: [] };
  }
  const sourceStores = Array.isArray(answer.stores) ? answer.stores : [];
  const perStoreSum = sourceStores.reduce(
    (sum, store) => sum + (Number.isFinite(store.quantity) ? store.quantity : 0), 0);
  const storesMatchAggregate = sourceStores.length === 0 || perStoreSum === answer.sellable;
  const displayedSellable = storesMatchAggregate ? answer.sellable : Math.min(answer.sellable, perStoreSum);
  const stores = storesMatchAggregate ? sourceStores.map(describeStore) : [];
  const displayStatus = answer.status === 'available' && displayedSellable === 0 ? 'out_of_stock' : answer.status;
  switch (displayStatus) {
    case 'available':
      return {
        tone: 'available',
        title: answer.at_least ? `At least ${displayedSellable} available online` : `${displayedSellable} available online`,
        detail: null,
        qualifier: answer.at_least ? 'Some stores are not reporting live, so the real number may be higher' : null,
        stores,
      };
    case 'out_of_stock':
      return { tone: 'out', title: 'Out of stock online', detail: describeRestock(answer.restock_eta, compression), qualifier: null, stores, clockNote: describeClock(compression) };
    case 'unknown':
      return {
        tone: 'unknown',
        title: UNKNOWN_TITLE,
        detail: answer.unknown_reason ? `Reason: ${answer.unknown_reason}` : null,
        qualifier: null,
        stores,
      };
    default:
      return {
        tone: 'unknown',
        title: UNKNOWN_TITLE,
        detail: `Unrecognised status "${answer.status}"`,
        qualifier: null,
        stores: [],
      };
  }
}
