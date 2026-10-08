import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import OfferCard from './OfferCard.jsx';

const offer = {
  headline: 'A comparable option is ready', body: 'A safe alternative is available.',
  offer_type: 'ALTERNATIVE_PRODUCT', product_id: 'P0160', store_id: null, discount_pct: 10,
  decision_route: 'RULE_DEFAULT', decision_reason: 'low_confidence',
  jev_choice: 'none', jev_confidence: 0.76, min_confidence: 0.8,
  rule_choice: 'alt:P0160', chosen_choice: 'alt:P0160',
};

describe('OfferCard', () => {
  it('renders the decision explanation for a below-threshold Jev suggestion', () => {
    render(<OfferCard offer={offer} error={null} />);

    expect(screen.getByRole('img', { name: 'Alternative product P0160' })).toHaveAttribute('src', '/img/P0160.jpg');
    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: safe rule — AI suggested no substitute (confidence 0.76, below threshold 0.8)');
  });

  it('groups the complete offer narrative and facts beside its alternative product image', () => {
    render(<OfferCard offer={offer} error={null} />);

    const product = screen.getByTestId('offer-product');
    const details = screen.getByTestId('offer-details');
    expect(product).toContainElement(screen.getByRole('img', { name: 'Alternative product P0160' }));
    expect(details).toHaveTextContent('A comparable option is ready');
    expect(details).toHaveTextContent('A safe alternative is available.');
    expect(details).toContainElement(screen.getByTestId('offer-decision'));
    expect(details).toHaveTextContent('Offer type');
    expect(details).toHaveTextContent('Product');
    expect(details).toHaveTextContent('Discount');
  });

  it('does not imply an AI suggestion when Jev returned no decision data', () => {
    render(<OfferCard offer={{ ...offer, decision_reason: 'timeout', jev_choice: null, jev_confidence: null }} error={null} />);

    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: safe rule — AI did not return a decision');
    expect(screen.getByTestId('offer-decision')).not.toHaveTextContent('suggested');
  });

  it('credits a confident "none" to the AI: no good substitute', () => {
    render(<OfferCard offer={{ ...offer, decision_route: 'JEV', decision_reason: 'no_good_substitute', jev_choice: 'none',
      jev_confidence: 0.9, product_id: null, chosen_choice: null, restock_notice: false, no_offer: true,
      body: 'Sold out everywhere. We found similar items, but none is a good match for this one.' }} error={null} />);
    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: AI — no good substitute (confidence 0.90, threshold 0.8)');
    expect(screen.getByTestId('offer-card')).toHaveTextContent('We found similar items, but none is a good match');
    expect(screen.getByTestId('offer-details')).toHaveTextContent('None: no good substitute');
    expect(screen.getByTestId('offer-details')).not.toHaveTextContent('nothing comparable');
  });

  it('credits an accepted Jev decision to AI and names the product, not its id', () => {
    render(<OfferCard offer={{ ...offer, decision_route: 'JEV', decision_reason: 'accepted', jev_choice: 'alt:P0061',
      jev_choice_label: 'Pathfinder Air', jev_confidence: 0.9 }} error={null} />);

    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: AI — chose Pathfinder Air (confidence 0.90, threshold 0.8)');
    expect(screen.getByTestId('offer-decision')).not.toHaveTextContent('alt:');
  });

  it('without a cart (or with an empty one) asks to add something, and keeps "no offer" for a real cart', () => {
    const { rerender } = render(<OfferCard offer={null} error={null} hasCart={false} />);
    expect(screen.getByText('Add something to your cart to see offers.')).toBeInTheDocument();
    rerender(<OfferCard offer={null} error={null} hasCart />);
    expect(screen.getByText('No offer for this product right now.')).toBeInTheDocument();
  });
});

// The four card states. Restock due in 17 real minutes = 17 business hours with the demo clock at 60.
const NOW = Date.parse('2026-10-09T10:00:00.000Z');
const ETA = '2026-10-09T10:17:00.000Z';
const P0061 = { product_id: 'P0061', name: 'Pathfinder Air', brand: 'Alpenpace', size: 'EU 42', colour: 'Ember red' };
const P0042_AI = {
  offer_id: 'o42', original_product_id: 'P0042', headline: 'Trailrunner GTX just sold out',
  body: 'Pathfinder Air by Alpenpace, Ember red, is in stock and similar. Take 10% off if you switch.',
  offer_type: 'ALTERNATIVE_PRODUCT', product_id: 'P0061', alternative: P0061, store_id: null, discount_pct: 10,
  decision_route: 'JEV', decision_reason: 'accepted', jev_choice: 'alt:P0061', jev_choice_label: 'Pathfinder Air',
  jev_confidence: 0.86, min_confidence: 0.8, rule_choice: 'alt:P0160', chosen_choice: 'alt:P0061',
};
const STATES = {
  both: { ...P0042_AI, restock_eta: ETA, restock_notice: true, no_offer: false },
  alternative: { ...P0042_AI, restock_eta: null, restock_notice: false, no_offer: false },
  restock: { ...P0042_AI, offer_type: 'NOTIFY_ME', product_id: null, alternative: null, discount_pct: 0,
    decision_route: 'RULE_DEFAULT', decision_reason: 'no_alternative', jev_choice: null, jev_confidence: null,
    jev_choice_label: null, rule_choice: null, chosen_choice: null, restock_eta: ETA, restock_notice: true,
    no_offer: false, body: 'Back in about 17 hours. We will let you know as soon as it is back in stock.' },
  neither: { ...P0042_AI, offer_type: 'NOTIFY_ME', product_id: null, alternative: null, discount_pct: 0,
    decision_route: 'RULE_DEFAULT', decision_reason: 'no_alternative', jev_choice: null, jev_confidence: null,
    jev_choice_label: null, rule_choice: null, chosen_choice: null, restock_eta: null, restock_notice: false,
    no_offer: true, body: 'Sold out everywhere, and nothing comparable is in stock right now.' },
};

describe('OfferCard states', () => {
  const renderState = (state, props = {}) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const view = render(<OfferCard offer={STATES[state]} error={null} compression={60} {...props} />);
    vi.useRealTimers();
    return view;
  };

  it('alternative and restock: both, and the shopper chooses', () => {
    const onSwap = vi.fn();
    const onNotify = vi.fn();
    renderState('both', { onSwap, onNotify });
    expect(screen.getByTestId('offer-restock')).toHaveTextContent('Back in about 17 hours');
    fireEvent.click(screen.getByRole('button', { name: 'Notify me' }));
    expect(onNotify).toHaveBeenCalledWith(STATES.both);
    fireEvent.click(screen.getByRole('button', { name: 'Or switch to Pathfinder Air in Ember red, 10% off' }));
    expect(onSwap).toHaveBeenCalledWith('P0042', 'P0061', STATES.both);
    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: AI — chose Pathfinder Air (confidence 0.86, threshold 0.8)');
    expect(screen.getByTestId('offer-details')).toHaveTextContent('Alternative or restock notice');
    expect(screen.getByTestId('offer-product')).toBeInTheDocument();
  });

  it('alternative only: no restock line, a plain switch', () => {
    renderState('alternative');
    expect(screen.queryByTestId('offer-restock')).toBeNull();
    expect(screen.getByRole('button', { name: 'Switch to Pathfinder Air in Ember red, 10% off' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Notify me' })).toBeNull();
    expect(screen.getByTestId('offer-details')).toHaveTextContent('Alternative product');
  });

  it('restock only: the restock date and Notify me, no product, no discount', () => {
    renderState('restock');
    expect(screen.getByTestId('offer-restock')).toHaveTextContent('Back in about 17 hours');
    expect(screen.getByRole('button', { name: 'Notify me' })).toBeInTheDocument();
    expect(screen.queryByTestId('offer-switch')).toBeNull();
    expect(screen.queryByTestId('offer-product')).toBeNull();
    expect(screen.getByTestId('offer-details')).not.toHaveTextContent('Discount');
    expect(screen.getByTestId('offer-details')).toHaveTextContent('Restock notice');
    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: safe rule, no eligible alternative');
  });

  it('neither: sold out everywhere, nothing comparable', () => {
    renderState('neither');
    expect(screen.getByTestId('offer-card')).toHaveTextContent(
      'Sold out everywhere, and nothing comparable is in stock right now.');
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByTestId('offer-details')).toHaveTextContent('None: nothing comparable in stock');
  });

  it('after Notify me the card confirms it instead of the button', () => {
    renderState('both', { notified: true });
    expect(screen.queryByRole('button', { name: 'Notify me' })).toBeNull();
    expect(screen.getByTestId('offer-restock')).toHaveTextContent('We will let you know.');
  });
});

