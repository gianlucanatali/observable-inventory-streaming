import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    const body = { ...SIBLING_ROUTES, ...routes }[path];
    if (body === undefined) throw new Error(`unexpected fetch ${url}`);
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }));
}

const GREEN = { name: 'Forest green', hex: '#2f6b4f' };
const SLATE = { name: 'Slate', hex: '#5a6678' };
const VARIANTS = [
  { product_id: 'P0003', colour: GREEN, size: 'EU 41' },
  { product_id: 'P0042', colour: GREEN, size: 'EU 42' },
  { product_id: 'P0004', colour: GREEN, size: 'EU 43' },
  { product_id: 'P0007', colour: SLATE, size: 'EU 41' },
];
const P42 = { product_id: 'P0042', name: 'Trailrunner GTX', brand: 'Alpenpace', category: 'footwear', size: 'EU 42',
  price_eur: 219.9, colour: GREEN, description: 'Balanced and responsive.', model_id: 'alpenpace-trailrunner-gtx', variant_rank: 1,
  kind: 'trail running shoe', waterproof: true,
  variants: VARIANTS };
const P1 = { ...P42, product_id: 'P0101', name: 'Trail Cap Air', brand: 'Vetta', category: 'accessories', size: 'One size', price_eur: 24.9,
  model_id: 'vetta-trail-cap-air', variant_rank: 0, kind: 'running cap', waterproof: false };

const STORES5 = [['S01', 2], ['S02', 1], ['S03', 3], ['S04', 1], ['S05', 2]].map(([store_id, quantity], i) => (
  { store_id, status: 'available', quantity, revision: 810 + i, feed: 'ok' }));
const answer = (over = {}) => ({
  product_id: 'P0042', status: 'available', sellable: 9, at_least: false, last_changed_at: '2026-10-09T11:02:00.123Z',
  unknown_reason: null, feed: 'ok', stores: STORES5,
  product: { name: 'Trailrunner GTX', brand: 'Alpenpace', size: 'EU 42', image_url: '/img/P0042.svg' },
  release: '1.1.0', ...over,
});

const CART = { cart_id: 'cart-0123456789ab', scenario_id: 'sc-1', count: 1, items: [
  { product_id: 'P0042', quantity: 1, name: 'Trailrunner GTX', brand: 'Alpenpace', size: 'EU 42', colour: 'Forest green', price_eur: 219.9 }] };

// Sibling sizes of P0042 in its colour: 41 in stock, 43 sold out online.
const SIBLING_ROUTES = {
  '/api/availability/P0003': answer({ product_id: 'P0003', sellable: 12 }),
  '/api/availability/P0004': answer({ product_id: 'P0004', status: 'out_of_stock', sellable: 0 }),
};

afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
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
    expect(screen.getByRole('img', { name: 'Alpenpace Trailrunner GTX' })).toHaveAttribute('src', '/img/P0042.jpg');
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
    expect(screen.getByTestId('product-sku')).toHaveTextContent('Item P0042');
    expect(screen.getByTestId('product-attributes')).toHaveTextContent('Trail running shoe · Waterproof');
    expect(screen.getByText('€219,90')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('counts only live stores and shows a quiet store as last seen (Lab 2.1)', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer({ sellable: 9, confirmed_min: 6, at_least: true, feed: 'stale',
        stores: STORES5.map((s) => (s.store_id === 'S03' ? { ...s, feed: 'stale', live: false } : { ...s, live: true })) }),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('At least 6 available online'));
    expect(screen.getByTestId('stock-panel')).not.toHaveTextContent('At least 9');
    expect(screen.getByTestId('stock-stores')).toHaveTextContent('Bologna 3 (last seen, not live)');
  });

  it('adds to cart as ONLINE', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer(),
      '/api/cart': CART,
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('9 available online'));
    screen.getByRole('button', { name: 'Add to cart' }).click();
    await waitFor(() => expect(fetch.mock.calls.some((c) => c[0] === '/api/cart')).toBe(true));
    const call = fetch.mock.calls.find((c) => c[0] === '/api/cart');
    expect(JSON.parse(call[1].body)).toMatchObject({ store_id: 'ONLINE', product_id: 'P0042', event_type: 'ADD' });
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('1'));
    // Only the pointer is kept in the browser; the contents stay server side.
    expect(window.localStorage.getItem('urbanstreet.cart_id')).toBe('cart-0123456789ab');
  });

  it('a reload keeps the cart: it is fetched by the stored id, listed in the drawer, and a line can be removed', async () => {
    window.location.hash = '#/product/P0042';
    window.localStorage.setItem('urbanstreet.cart_id', 'cart-0123456789ab');
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: true, release: 'x' },
      '/api/availability/P0042': answer(),
      '/api/cart/cart-0123456789ab': { ...CART, count: 2, items: [{ ...CART.items[0], quantity: 2 }] },
      '/api/cart': { ...CART, count: 0, items: [] },
      '/api/offers': { offer: null },
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('2'));
    await waitFor(() => expect(screen.getByText('No offer for this product right now.')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    const line = within(screen.getByTestId('cart-drawer')).getByTestId('cart-item');
    expect(line).toHaveTextContent('Alpenpace Trailrunner GTX');
    expect(line).toHaveTextContent('EU 42 · Forest green · Qty 2');
    fireEvent.click(within(line).getByRole('button', { name: 'Remove Alpenpace Trailrunner GTX' }));
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('0'));
    const remove = fetch.mock.calls.find((c) => c[0] === '/api/cart');
    expect(JSON.parse(remove[1].body)).toEqual({ cart_id: 'cart-0123456789ab', store_id: 'ONLINE', product_id: 'P0042', event_type: 'ABANDON' });
    expect(screen.getByTestId('cart-drawer')).toHaveTextContent('Your cart is empty.');
    expect(screen.getByText('Add something to your cart to see offers.')).toBeInTheDocument();
  });

  it('starts a new cart when the stored one is gone (expired or reset)', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', 'cart-0123456789ab');
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      if (url === '/api/cart/cart-0123456789ab') {
        return { ok: false, status: 404, text: async () => JSON.stringify({ error: 'cart_not_found', message: 'no cart' }) };
      }
      const body = { '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' }, '/api/products': [P42] }[url];
      if (body === undefined) throw new Error(`unexpected fetch ${url}`);
      return { ok: true, status: 200, text: async () => JSON.stringify(body) };
    }));
    render(<App />);
    await waitFor(() => expect(window.localStorage.getItem('urbanstreet.cart_id')).toBeNull());
    expect(screen.getByTestId('cart-count')).toHaveTextContent('0');
  });

  it('with offers on and no cart, the offer card asks to add something first', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: true, release: 'x' },
      '/api/availability/P0042': answer(),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText('Add something to your cart to see offers.')).toBeInTheDocument());
    expect(screen.queryByText('No offer for this product right now.')).toBeNull();
    expect(fetch.mock.calls.some((c) => String(c[0]).startsWith('/api/offers'))).toBe(false);
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
        ...SIBLING_ROUTES,
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
    expect(within(screen.getAllByTestId('product-card')[0]).getByRole('img')).toHaveAttribute('src', '/img/P0042.jpg');
    expect(within(screen.getByTestId('hero')).getByRole('img')).toHaveAttribute('src', '/img/P0042.jpg');
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

  it("shows colour swatches and the colour's sizes, with sold-out sibling sizes disabled", async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer(),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'EU 43, sold out online' })).toBeDisabled());
    const picker = screen.getByTestId('variant-picker');
    expect(picker).toHaveTextContent('Colour: Forest green');
    expect(picker).toHaveTextContent('Size: EU 42');
    expect(screen.getAllByTestId('colour-swatch').map((b) => b.getAttribute('aria-label'))).toEqual(['Forest green', 'Slate']);
    expect(screen.getAllByTestId('size-option').map((b) => b.textContent)).toEqual(['EU 41', 'EU 42', 'EU 43']);
    expect(screen.getByRole('button', { name: 'EU 42' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'EU 41' })).toBeEnabled();
    expect(screen.getByText('Crossed-out sizes are sold out online.')).toBeInTheDocument();
    // Sibling lookups are bounded to the colour's other sizes.
    const urls = fetch.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith('/api/availability/'));
    expect(new Set(urls)).toEqual(new Set(['/api/availability/P0042', '/api/availability/P0003', '/api/availability/P0004']));
    fireEvent.click(screen.getByRole('button', { name: 'EU 41' }));
    expect(window.location.hash).toBe('#/product/P0003');
  });

  it('marks the selected size sold out while a sibling size is still available', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x', time_compression: 60 },
      '/api/availability/P0042': answer({ status: 'out_of_stock', sellable: 0 }),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('stock-panel')).toHaveTextContent('Out of stock online'));
    const selected = screen.getByRole('button', { name: 'EU 42, sold out online' });
    expect(selected).toHaveAttribute('aria-pressed', 'true');
    expect(selected).toHaveClass('size-sold-out');
    await waitFor(() => expect(screen.getByRole('button', { name: 'EU 41' })).toHaveAttribute('data-state', 'available'));
    expect(screen.getByRole('button', { name: 'Add to cart' })).toBeDisabled();
  });

  it('a colour swatch opens the same size in that colour, else the nearest size', async () => {
    window.location.hash = '#/product/P0042';
    mockFetch({
      '/api/products/P0042': P42,
      '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' },
      '/api/availability/P0042': answer(),
    });
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Slate' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Slate' }));
    expect(window.location.hash).toBe('#/product/P0007');
  });

  it('home shows one card per model, linking to its lead SKU', async () => {
    const sibling = { ...P42, product_id: 'P0003', size: 'EU 41', variant_rank: 0 };
    mockFetch({ '/config': { poll_ms: 60000, offers_enabled: false, release: 'x' }, '/api/products': [P1, sibling, P42] });
    render(<App />);
    await waitFor(() => expect(screen.getAllByTestId('product-card')).toHaveLength(2));
    expect(screen.getAllByTestId('product-card')[0]).toHaveAttribute('href', '#/product/P0042');
    expect(screen.getAllByTestId('product-card')[1]).toHaveAttribute('href', '#/product/P0101');
  });
});
