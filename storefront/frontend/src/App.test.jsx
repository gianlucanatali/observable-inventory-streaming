import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.jsx';
import { datadogRum } from '@datadog/browser-rum';
import { resetRumForTests } from './rum.js';

vi.mock('@datadog/browser-rum', () => ({
  datadogRum: { init: vi.fn(), setGlobalContext: vi.fn(), setGlobalContextProperty: vi.fn(), addAction: vi.fn() },
}));

function mockFetch(routes) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const path = String(url).split('?')[0];
    const body = routes[path];
    if (body === undefined) throw new Error(`unexpected fetch ${url}`);
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }));
}

const P42 = { product_id: 'P0042', name: 'Trailrunner GTX', brand: 'Alpenpace', category: 'footwear', size: 'EU 42',
  price_eur: 219.9, colour: { name: 'Forest green', hex: '#2f6b4f' }, description: 'Balanced and responsive.' };
const P1 = { ...P42, product_id: 'P0001', name: 'Trail Cap GTX', brand: 'Vetta', category: 'accessories', size: 'M', price_eur: 18.9 };

const STORES5 = [['S01', 2], ['S02', 1], ['S03', 3], ['S04', 1], ['S05', 2]].map(([store_id, quantity], i) => (
  { store_id, status: 'available', quantity, revision: 810 + i, feed: 'ok' }));
const answer = (over = {}) => ({
  product_id: 'P0042', status: 'available', sellable: 9, at_least: false, last_changed_at: '2026-10-09T11:02:00.123Z',
  unknown_reason: null, feed: 'ok', stores: STORES5,
  product: { name: 'Trailrunner GTX', brand: 'Alpenpace', size: 'EU 42', image_url: '/img/P0042.svg' },
  release: '1.1.0', ...over,
});

afterEach(() => {
  vi.clearAllMocks();
  resetRumForTests();
  vi.unstubAllGlobals();
  window.location.hash = '';
});

describe('App', () => {
  it('renders unknown stock explicitly and hides offers when disabled', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': {
        product_id: 'P0042', status: 'unknown', sellable: null, at_least: true, last_changed_at: null,
        unknown_reason: 'not_ready', feed: 'ok', stores: [], product: null, release: '1.0.0',
      },
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent("can't be confirmed"));
    expect(screen.queryByText('Offer')).toBeNull();
    expect(screen.getByRole('button', { name: 'Add to cart' })).toBeDisabled();
    expect(screen.getByText(/Serving release 1.0.0/)).toBeInTheDocument();
  });

  it('shows the lower online number and withholds a mismatched per-store breakdown', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer({ sellable: 7, at_least: true, stores: STORES5.map((s, i) =>
        (i === 3 ? { ...s, status: 'unknown', quantity: null, revision: null } : s)) }),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('At least 7 available online'));
    expect(screen.queryByTestId('stock-stores')).toBeNull();
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Trailrunner GTX'));
    expect(screen.getByText(/Size EU 42/)).toBeInTheDocument();
    expect(screen.getByText('€219,90')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('adds to cart as ONLINE', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer(),
      '/api/cart': { cart_id: 'cart-0123456789ab' },
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('9 available online'));
    screen.getByRole('button', { name: 'Add to cart' }).click();
    await waitFor(() => expect(fetch.mock.calls.some((c) => c[0] === '/api/cart')).toBe(true));
    const call = fetch.mock.calls.find((c) => c[0] === '/api/cart');
    expect(JSON.parse(call[1].body)).toMatchObject({ store_id: 'ONLINE', product_id: 'P0042', event_type: 'ADD' });
  });

  it('sends the display beacon when a new sellable answer is rendered, not for the first one', async () => {
    window.location.hash = '#/product/P0042';
    const answers = [answer(), answer(), answer({ sellable: 8, last_changed_at: '2026-10-09T11:02:05.000Z' })];
    let n = 0;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      const path = String(url).split('?')[0];
      const bodies = {
        '/api/products/P0042': P42,
        '/config': { poll_ms: 20, offers_enabled: false, release: 'x' },
      };
      if (path === '/api/availability/P0042') bodies[path] = answers[Math.min(n++, answers.length - 1)];
      if (path === '/api/beacon/display') return { ok: true, status: 204, text: async () => '' };
      if (bodies[path] === undefined) throw new Error(`unexpected fetch ${url} ${init && init.method}`);
      return { ok: true, status: 200, text: async () => JSON.stringify(bodies[path]) };
    }));
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('8 available online'));
    await waitFor(() => expect(fetch.mock.calls.filter((c) => c[0] === '/api/beacon/display')).toHaveLength(1));
    const beacon = fetch.mock.calls.find((c) => c[0] === '/api/beacon/display');
    expect(JSON.parse(beacon[1].body)).toEqual({ product_id: 'P0042', last_changed_at: '2026-10-09T11:02:05.000Z' });
  });

  it('home page lists products with the featured one first and makes no stock calls', async () => {
    mockFetch({
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/products': [P1, P42],
    });
    render(<App />);
    await waitFor(() => expect(screen.getAllByTestId('product-card')).toHaveLength(2));
    expect(within(screen.getAllByTestId('product-card')[0]).getByText('Trailrunner GTX')).toBeInTheDocument();
    expect(screen.getByTestId('hero')).toHaveAttribute('href', '#/product/P0042');
    const urls = fetch.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.startsWith('/api/availability/'))).toBe(false);
  });

  it('home hero speaks about scarcity without live numbers', async () => {
    mockFetch({ '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' }, '/api/products': [P42] });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('hero')).toHaveTextContent('Only a few pairs left in Italy'));
  });

  it('starts RUM and records the exposure only when /config carries a rum block', async () => {
    window.location.hash = '#/product/P0042';
    const rum = { application_id: 'a', client_token: 't', site: 'datadoghq.eu', service: 'storefront-web',
      env: 'dd-demo-dev', version: '1.0.0', stack: 'dev', exposure_budget_ms: 800 };
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x', rum },
      '/api/availability/P0042': answer({ status: 'unknown', sellable: null, at_least: true, unknown_reason: 'not_ready' }),
    });
    render(<App />);
    await waitFor(() => expect(datadogRum.addAction).toHaveBeenCalled());
    expect(datadogRum.init).toHaveBeenCalledTimes(1);
    expect(datadogRum.addAction).toHaveBeenCalledWith('stock_answer', expect.objectContaining({ status: 'unknown', exposed: true }));
    expect(datadogRum.setGlobalContextProperty).toHaveBeenCalledWith('exposed', true);
  });

  it('leaves RUM off without a rum block', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer(),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('9 available online'));
    expect(datadogRum.init).not.toHaveBeenCalled();
    expect(datadogRum.addAction).not.toHaveBeenCalled();
  });

  it('shows the restock ETA on an out-of-stock product', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x', time_compression: 60 },
      '/api/availability/P0042': answer({ status: 'out_of_stock', sellable: 0,
        restock_eta: new Date(Date.now() + 48 * 60_000).toISOString() }),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent(/Back in stock in about 2 days/));
    expect(screen.getByTestId('stock-clock')).toHaveTextContent('demo clock: 1 min = 1 h');
  });
});
