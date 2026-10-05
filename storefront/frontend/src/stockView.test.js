import { describe, expect, it } from 'vitest';
import { describeClock, describeRestock, describeStock, describeStore, formatBusinessDuration } from './stockView.js';

const st = (store_id, status, quantity, feed = 'ok') => ({ store_id, status, quantity, revision: 1, feed });
const stores = [st('S01', 'available', 2), st('S02', 'available', 1), st('S03', 'available', 3), st('S04', 'available', 1), st('S05', 'available', 2)];
const base = { product_id: 'P0042', status: 'available', sellable: 9, at_least: false, unknown_reason: null, feed: 'ok', stores };

describe('describeStock', () => {
  it('shows the sellable number online', () => {
    const v = describeStock(base);
    expect(v.title).toBe('9 available online');
    expect(v.tone).toBe('available');
    expect(v.qualifier).toBeNull();
  });
  it('says "At least" when the answer is a lower bound', () => {
    const v = describeStock({ ...base, sellable: 7, at_least: true });
    expect(v.title).toBe('At least 7 available online');
    expect(v.qualifier).toMatch(/not reporting live/);
  });
  it.each([
    ['per-store positions lag the aggregate', 9, 7],
    ['the aggregate lags the per-store positions', 7, 9],
  ])('shows the lower sellable value when %s', (_scenario, aggregate, storeSum) => {
    const v = describeStock({
      ...base,
      sellable: aggregate,
      stores: [st('S01', 'available', storeSum)],
    });

    expect(v.title).toBe(`${Math.min(aggregate, storeSum)} available online`);
    expect(v.stores).toEqual([]);
  });
  it('shows out of stock when reconciliation lowers an available aggregate to zero', () => {
    const v = describeStock({ ...base, sellable: 3, stores: [st('S01', 'out_of_stock', 0)] });

    expect(v).toMatchObject({ tone: 'out', title: 'Out of stock online', stores: [] });
  });
  it('reveals the per-store breakdown only after it matches the aggregate', () => {
    const mismatch = describeStock({ ...base, sellable: 8 });
    const match = describeStock(base);

    expect(mismatch.stores).toEqual([]);
    expect(match.stores.map((store) => store.text)).toEqual([
      'Milano 2', 'Torino 1', 'Bologna 3', 'Roma 1', 'Firenze 2',
    ]);
  });
  it('shows out of stock only for a certain zero', () => {
    expect(describeStock({ ...base, status: 'out_of_stock', sellable: 0 }).title).toBe('Out of stock online');
  });
  it('never shows unknown as zero or available, even when sellable is 0', () => {
    const v = describeStock({ ...base, status: 'unknown', sellable: 0, at_least: true, unknown_reason: 'stores_unknown' });
    expect(v.tone).toBe('unknown');
    expect(v.title).toMatch(/can't be confirmed/);
    expect(v.title).not.toMatch(/Out of stock|available|0/);
  });
  it('treats a failed fetch as unknown, not zero', () => {
    expect(describeStock(null, 'boom').tone).toBe('unknown');
  });
  it('treats an unrecognised status as unknown', () => {
    const v = describeStock({ ...base, status: 'weird' });
    expect(v.tone).toBe('unknown');
    expect(v.stores).toEqual([]);
  });
  it('lists the stores in order with short names', () => {
    expect(describeStock(base).stores.map((s) => s.text).join(' · ')).toBe('Milano 2 · Torino 1 · Bologna 3 · Roma 1 · Firenze 2');
  });
});

describe('describeStore', () => {
  it('marks an unknown store with ? and never a number', () => {
    const v = describeStore(st('S04', 'unknown', null));
    expect(v).toMatchObject({ text: 'Roma ?', tone: 'unknown' });
  });
  it('marks a known store whose feed is not ok as not live', () => {
    expect(describeStore(st('S02', 'available', 1, 'stale'))).toMatchObject({ text: 'Torino 1 (not live)', tone: 'stale' });
  });
  it('shows an unknown store with a stale feed as ?', () => {
    expect(describeStore(st('S02', 'unknown', null, 'unknown')).text).toBe('Torino ?');
  });
  it('renders not_stocked and a zero quantity distinctly', () => {
    expect(describeStore(st('S01', 'not_stocked', null)).text).toBe('Milano not stocked');
    expect(describeStore(st('S01', 'out_of_stock', 0)).text).toBe('Milano 0');
  });
});

describe('restock ETA', () => {
  const NOW = Date.parse('2026-10-09T10:00:00Z');
  it('formats business durations', () => {
    expect(formatBusinessDuration(20_000)).toBe('less than a minute');
    expect(formatBusinessDuration(30 * 60_000)).toBe('about 30 minutes');
    expect(formatBusinessDuration(5 * 3600_000)).toBe('about 5 hours');
    expect(formatBusinessDuration(48 * 3600_000)).toBe('about 2 days');
    expect(formatBusinessDuration(-1)).toBe('any moment now');
  });
  it('multiplies the real time left by the demo clock', () => {
    // 48 real minutes left at C = 60 are 48 business hours = 2 days
    expect(describeRestock('2026-10-09T10:48:00.000Z', 60, NOW)).toBe('Back in stock in about 2 days');
    expect(describeRestock('2026-10-09T10:05:00.000Z', 60, NOW)).toBe('Back in stock in about 5 hours');
    expect(describeRestock('2026-10-09T10:48:00.000Z', 1, NOW)).toBe('Back in stock in about 48 minutes');
    expect(describeRestock('2026-10-09T09:00:00.000Z', 60, NOW)).toBe('Back in stock any moment now');
  });
  it('describes the clock', () => {
    expect(describeClock(60)).toBe('demo clock: 1 min = 1 h');
    expect(describeClock(120)).toBe('demo clock: 1 min = 2 h');
    expect(describeClock(30)).toBe('demo clock: 1 min = 30 min');
    expect(describeClock(1440)).toBe('demo clock: 1 min = 1 day');
    expect(describeClock(1)).toBeNull();
  });
  it('is null without a valid ETA', () => {
    expect(describeRestock(null, 60, NOW)).toBeNull();
    expect(describeRestock('garbage', 60, NOW)).toBeNull();
    expect(describeRestock('2026-10-11T10:00:00.000Z', null, NOW)).toBeNull();  // no demo clock known
  });
  it('shows on out of stock only, never on unknown or available', () => {
    const eta = new Date(Date.now() + 30_000).toISOString();
    expect(describeStock({ ...base, status: 'out_of_stock', sellable: 0, restock_eta: eta }, null, 60).detail).toMatch(/^Back in stock in about (29|30) minutes$/);
    expect(describeStock({ ...base, status: 'out_of_stock', sellable: 0, restock_eta: null }).detail).toBeNull();
    expect(String(describeStock({ ...base, status: 'unknown', sellable: 0, at_least: true, restock_eta: eta }).detail)).not.toMatch(/Back in stock/);
    expect(describeStock({ ...base, restock_eta: eta }).detail).toBeNull();
  });
});
