import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.jsx';
import { datadogRum } from '@datadog/browser-rum';
import { resetRumForTests } from './rum.js';
import { confirmedStock, inStockAlternatives } from './alternatives.js';

vi.mock('@datadog/browser-rum', () => ({
  datadogRum: { init: vi.fn(), setGlobalContext: vi.fn(), setGlobalContextProperty: vi.fn(), addAction: vi.fn() },
}));

const CART_ID = 'cart-0123456789ab';
const line = (product_id, name, brand, colour, price_eur) => ({ product_id, quantity: 1, name, brand, size: 'EU 42', colour, price_eur });
const L42 = line('P0042', 'Trailrunner GTX', 'Alpenpace', 'Forest green', 219.9);
const L48 = line('P0048', 'Dolomia Evo', 'Ridgeline', 'Charcoal', 164.9);
const L92 = line('P0092', 'Pathfinder Lite', 'Alpenpace', 'Navy', 119.9);
const cartBody = (items) => ({ cart_id: CART_ID, scenario_id: 'sc-1', items, count: items.length });
const alt = (product_id, name, colour, kind, price_eur, waterproof = false) => (
  { product_id, name, brand: 'X', size: 'EU 42', colour, kind, waterproof, price_eur });
const P0059 = alt('P0059', 'Dolomia Evo', 'Glacier blue', 'hiking boot', 164.9, true);
const P0079 = alt('P0079', 'Rifugio Pro', 'Slate', 'trekking boot', 149.9);
const P0160 = alt('P0160', 'Brenta Storm', 'Burnt orange', 'approach shoe', 208.9);
const P0061 = alt('P0061', 'Pathfinder Air', 'Ember red', 'hiking shoe', 204.9);
const P0099 = alt('P0099', 'Sold Too', 'Grey', 'hiking shoe', 210);
const offerBase = { scenario_id: 'sc-1', cart_id: CART_ID, original_store_id: 'ONLINE', offer_type: 'ALTERNATIVE_PRODUCT',
  store_id: null, discount_pct: 10, body: 'b', text_route: 'TEMPLATE', min_confidence: 0.8 };
const O42 = { ...offerBase, offer_id: 'o42', original_product_id: 'P0042', product_id: 'P0160', alternative: P0160,
  headline: 'Trailrunner GTX just sold out', decision_route: 'RULE_DEFAULT', decision_reason: 'low_confidence',
  jev_choice: 'alt:P0061', jev_confidence: 0.45, rule_choice: 'alt:P0160', chosen_choice: 'alt:P0160' };
const O48 = { ...offerBase, offer_id: 'o48', original_product_id: 'P0048', product_id: 'P0059', alternative: P0059,
  headline: 'Dolomia Evo just sold out', decision_route: 'JEV', decision_reason: 'accepted',
  jev_choice: 'alt:P0059', jev_confidence: 0.92, rule_choice: 'alt:P0059', chosen_choice: 'alt:P0059' };
// No eligible alternative and no near restock: the worker publishes the case without an Offer.
const O92 = { ...offerBase, offer_id: 'o92', original_product_id: 'P0092', offer_type: 'NOTIFY_ME', product_id: null,
  alternative: null, discount_pct: 0, headline: 'Pathfinder Lite just sold out',
  body: 'Sold out everywhere, and nothing comparable is in stock right now.', decision_route: 'RULE_DEFAULT',
  decision_reason: 'no_alternative', jev_choice: null, jev_confidence: null, rule_choice: null, chosen_choice: null,
  jev_choice_label: null, no_offer: true };
const avail = (product_id, n) => ({ product_id, status: n > 0 ? 'available' : 'out_of_stock', sellable: n, confirmed_min: n,
  at_least: false, stores: [], last_changed_at: null, release: '1.0.0' });

// A fake backend: the cart lives here like in Redis, so ABANDON/ADD and a reload behave as on the real shop.
const RUM = { application_id: 'a', client_token: 't', site: 'datadoghq.eu', service: 'storefront-web', env: 'e', version: 'v' };

function fakeShop(rum = undefined, { items = [L42, L48], offers = [O42, O48] } = {}) {
  const state = { items, posts: [] };
  const routes = {
    '/config': { poll_ms: 60000, offers_enabled: true, release: 'x', rum, time_compression: 60 },
    '/api/products': [],
    '/api/products/P0042/alternatives': { product_id: 'P0042', max_alternatives: 2, candidates: [P0099, P0160, P0061, P0079] },
    '/api/products/P0048/alternatives': { product_id: 'P0048', max_alternatives: 2, candidates: [P0059, P0079] },
    '/api/availability/P0099': avail('P0099', 0),
    '/api/availability/P0160': avail('P0160', 151),
    '/api/availability/P0061': avail('P0061', 99),
    '/api/availability/P0059': avail('P0059', 66),
    '/api/availability/P0079': avail('P0079', 165),
  };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const path = String(url).split('?')[0];
    let body;
    if (path === '/api/cart') {
      const req = JSON.parse(init.body);
      state.posts.push(req);
      if (req.event_type === 'ABANDON') state.items = state.items.filter((i) => i.product_id !== req.product_id);
      else state.items = [...state.items, line(req.product_id, 'Dolomia Evo', 'Ridgeline', 'Glacier blue', 164.9)];
      body = cartBody(state.items);
    } else if (path === `/api/cart/${CART_ID}`) {
      body = cartBody(state.items);
    } else if (path === '/api/offers') {
      body = { offer: offers[offers.length - 1], offers };  // the store keeps offers after a swap; the drawer hides them
    } else {
      body = routes[path];
    }
    if (body === undefined) throw new Error(`unexpected fetch ${url}`);
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  }));
  return state;
}

afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  resetRumForTests();
  vi.unstubAllGlobals();
  window.location.hash = '';
});

describe('alternatives', () => {
  it('keeps the first two in-stock candidates in order and skips unknown or failed stock', async () => {
    const getAvailability = vi.fn(async (id) => {
      if (id === 'P0061') throw new Error('boom');
      return { P0099: avail('P0099', 0), P0160: avail('P0160', 3), P0079: { ...avail('P0079', 0), status: 'unknown', confirmed_min: null } ,
        P0059: avail('P0059', 2) }[id];
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const got = await inStockAlternatives('P0042', {
      getAlternatives: async () => ({ max_alternatives: 2, candidates: [P0099, P0160, P0061, P0079, P0059, P0160] }),
      getAvailability,
    });
    expect(got.map((a) => [a.product_id, a.stock])).toEqual([['P0160', 3], ['P0059', 2]]);
    expect(confirmedStock({ status: 'available', at_least: true, sellable: 9 })).toBe(0);
  });
});

describe('cart drawer with two sold-out items', () => {
  it('shows each item its offer and the other alternative, and swaps one through the cart API', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/';
    const state = fakeShop(RUM);
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('2'));
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    await waitFor(() => expect(screen.getAllByTestId('cart-offer')).toHaveLength(2));
    const [trail, boot] = screen.getAllByTestId('cart-item');

    expect(within(trail).getByTestId('cart-offer-line')).toHaveTextContent(
      'Sold out → Brenta Storm, Burnt orange −10% · Decided by: safe rule');
    await waitFor(() => expect(within(trail).getAllByTestId('cart-alt')).toHaveLength(1));
    expect(within(trail).getByTestId('cart-alt-offered')).toHaveTextContent('Brenta Storm, Burnt orangeOffer −10%');
    expect(within(trail).getByTestId('cart-alt-offered')).toHaveTextContent('approach shoe · €208,90 · 151 in stock');
    // Jev's unaccepted pick carries its score on the plain alternative.
    expect(within(trail).getByTestId('cart-alt')).toHaveTextContent('Pathfinder Air, Ember redAI: 0.45');
    expect(within(trail).getByTestId('cart-alt')).not.toHaveTextContent('−10%');

    expect(within(boot).getByTestId('cart-offer-line')).toHaveTextContent(
      'Sold out → Dolomia Evo, Glacier blue −10% · Decided by: AI');
    await waitFor(() => expect(within(boot).getByTestId('cart-alt')).toHaveTextContent('Rifugio Pro, Slate'));
    expect(within(boot).getByTestId('cart-alt-offered')).toHaveTextContent('AI: 0.92');
    expect(within(boot).getByTestId('cart-alt')).toHaveTextContent('trekking boot · €149,90 · 165 in stock');

    fireEvent.click(within(boot).getByRole('button', { name: 'Swap to Dolomia Evo, Glacier blue' }));
    await waitFor(() => expect(state.posts).toHaveLength(2));
    expect(state.posts).toEqual([
      { cart_id: CART_ID, store_id: 'ONLINE', product_id: 'P0048', event_type: 'ABANDON' },
      { cart_id: CART_ID, store_id: 'ONLINE', product_id: 'P0059', event_type: 'ADD' },
    ]);
    await waitFor(() => expect(screen.getAllByTestId('cart-offer')).toHaveLength(1));
    expect(screen.getByTestId('cart-drawer')).toHaveTextContent('Glacier blue');
    expect(datadogRum.addAction).toHaveBeenCalledWith('offer_accepted',
      { product_id: 'P0048', alternative_id: 'P0059', decided_by: 'ai', discount_pct: 10, restock_offered: false });

    // Swapping to the other option (full price) is not an accepted Offer.
    fireEvent.click(within(screen.getAllByTestId('cart-item')[0]).getByRole('button', { name: 'Swap to Pathfinder Air, Ember red' }));
    await waitFor(() => expect(state.posts).toHaveLength(4));
    expect(datadogRum.addAction.mock.calls.filter((c) => c[0] === 'offer_accepted')).toHaveLength(1);
  });

  it('the product page card shows only that product\'s offer', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/product/P0042';
    fakeShop();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('offer-card')).toHaveTextContent('Trailrunner GTX just sold out'));
    expect(screen.getByTestId('offer-card')).not.toHaveTextContent('Dolomia');
  });
});

describe('Swap persists through the cart API', () => {
  it('offer_accepted is recorded only after the server cart took both the ABANDON and the ADD', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/';
    const state = fakeShop(RUM);
    const realFetch = fetch;
    // The ADD fails: the swap did not reach the server cart, so no Offer accepted.
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (String(url) === '/api/cart' && JSON.parse(init.body).event_type === 'ADD') {
        return { ok: false, status: 502, text: async () => JSON.stringify({ error: 'publish_failed', message: 'down' }) };
      }
      return realFetch(url, init);
    }));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('2'));
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    const boot = (await screen.findAllByTestId('cart-item'))[1];
    fireEvent.click(await within(boot).findByRole('button', { name: 'Swap to Dolomia Evo, Glacier blue' }));
    await waitFor(() => expect(state.posts).toHaveLength(1));  // the ABANDON reached the fake server; the ADD failed
    await screen.findByText(/POST \/api\/cart returned HTTP 502/);
    expect(datadogRum.addAction.mock.calls.filter((c) => c[0] === 'offer_accepted')).toHaveLength(0);
  });
});

describe('a sold-out item with a near restock and an alternative', () => {
  it('the drawer shows the restock date with Notify me under the offer line, and Notify me is a RUM action', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/';
    const eta = new Date(Date.now() + 17 * 60_000).toISOString();  // 17 business hours with the demo clock at 60
    const both = { ...O42, decision_route: 'JEV', decision_reason: 'accepted', product_id: 'P0061', alternative: P0061,
      jev_choice: 'alt:P0061', jev_confidence: 0.86, chosen_choice: 'alt:P0061', restock_eta: eta, restock_notice: true,
      no_offer: false };
    const state = fakeShop(RUM, { items: [L42], offers: [both] });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('1'));
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    const item = await screen.findByTestId('cart-item');
    await waitFor(() => expect(within(item).getByTestId('cart-offer-line')).toHaveTextContent(
      'Sold out → Pathfinder Air, Ember red −10% · Decided by: AI'));
    expect(within(item).getByTestId('cart-offer-restock')).toHaveTextContent(/^Back in about 1[67] hours/);

    fireEvent.click(within(item).getByRole('button', { name: 'Notify me' }));
    await waitFor(() => expect(within(item).getByTestId('cart-offer-restock')).toHaveTextContent('We will let you know.'));
    expect(datadogRum.addAction).toHaveBeenCalledWith('restock_notice_confirmed',
      { product_id: 'P0042', restock_eta: eta, alternative_offered: true });
    expect(state.posts).toEqual([]);  // Notify me changes nothing in the cart
  });

  it('a confident "none" with a near restock lists the alternatives at full price and offers to wait', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/';
    const eta = new Date(Date.now() + 17 * 60_000).toISOString();
    const wait = { ...O42, offer_type: 'NOTIFY_ME', product_id: null, alternative: null, discount_pct: 0,
      decision_route: 'JEV', decision_reason: 'no_good_substitute', jev_choice: 'none', jev_confidence: 0.9,
      chosen_choice: null, restock_eta: eta, restock_notice: true, no_offer: false };
    fakeShop(undefined, { items: [L42], offers: [wait] });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('1'));
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    const item = await screen.findByTestId('cart-item');
    await waitFor(() => expect(within(item).getByTestId('cart-offer-line')).toHaveTextContent(
      'Sold out → No good substitute · Decided by: AI'));
    expect(within(item).getByRole('button', { name: 'Notify me' })).toBeInTheDocument();
    await waitFor(() => expect(within(item).getAllByTestId('cart-alt')).toHaveLength(2));
    expect(within(item).queryByTestId('cart-alt-offered')).toBeNull();
  });
});

describe('a sold-out item with nothing comparable', () => {
  it('the drawer says so instead of listing alternatives, next to the two offers', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/';
    fakeShop(undefined, { items: [L42, L48, L92], offers: [O42, O48, O92] });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('cart-count')).toHaveTextContent('3'));
    fireEvent.click(screen.getByRole('button', { name: /^Cart/ }));
    await waitFor(() => expect(screen.getAllByTestId('cart-offer')).toHaveLength(3));
    const lite = screen.getAllByTestId('cart-item')[2];
    expect(within(lite).getByTestId('cart-offer-line')).toHaveTextContent(
      'Sold out → No comparable product in stock · Decided by: safe rule');
    expect(within(lite).queryAllByTestId('cart-alt')).toHaveLength(0);
    expect(within(lite).queryByRole('button', { name: /^Swap/ })).toBeNull();
    expect(fetch.mock.calls.map((c) => String(c[0]))).not.toContain('/api/products/P0092/alternatives');
  });

  it('the product page card is honest: no offer type, no discount, the safe rule with no eligible alternative', async () => {
    window.localStorage.setItem('urbanstreet.cart_id', CART_ID);
    window.location.hash = '#/product/P0092';
    fakeShop(undefined, { items: [L42, L48, L92], offers: [O42, O48, O92] });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<App />);
    const card = await screen.findByTestId('offer-card');
    await waitFor(() => expect(card).toHaveTextContent('Pathfinder Lite just sold out'));
    expect(card).toHaveTextContent('Sold out everywhere, and nothing comparable is in stock right now.');
    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: safe rule, no eligible alternative');
    expect(card).toHaveTextContent('None: nothing comparable in stock');
    expect(card).not.toHaveTextContent('Discount');
    expect(screen.queryByTestId('offer-product')).toBeNull();
  });
});
