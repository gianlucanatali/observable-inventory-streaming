import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import OfferCard from './OfferCard.jsx';

const offer = {
  headline: 'A comparable option is ready', body: 'A safe alternative is available.',
  offer_type: 'ALTERNATIVE_PRODUCT', product_id: 'P0160', store_id: null, discount_pct: 10,
  decision_route: 'RULE_DEFAULT', decision_reason: 'low_confidence',
  jev_choice: 'notify_me', jev_confidence: 0.76, min_confidence: 0.8,
  rule_choice: 'alt:P0160', chosen_choice: 'alt:P0160',
};

describe('OfferCard', () => {
  it('renders the decision explanation for a below-threshold Jev suggestion', () => {
    render(<OfferCard offer={offer} error={null} />);

    expect(screen.getByRole('img', { name: 'Alternative product P0160' })).toHaveAttribute('src', '/img/P0160.jpg');
    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: safe rule — AI suggested a restock notice (confidence 0.76, below threshold 0.8)');
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

  it('says no AI choice was needed when only one option was left (no_choice)', () => {
    const noChoice = { ...offer, decision_reason: 'no_choice', jev_choice: null, jev_confidence: null };
    render(<OfferCard offer={noChoice} error={null} />);
    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: safe rule — only one in-stock alternative, no AI choice needed');
  });

  it('a Restock notice with no eligible alternative is an offer decided by the safe rule', () => {
    render(<OfferCard offer={{ ...offer, offer_type: 'NOTIFY_ME', product_id: null, discount_pct: 0,
      decision_reason: 'no_alternative', jev_choice: null, jev_confidence: null, chosen_choice: 'notify_me',
      no_offer: false, body: 'Back in about 3 days. We will let you know as soon as it is back in stock.' }} error={null} />);
    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: safe rule, no eligible alternative');
    expect(screen.getByTestId('offer-details')).toHaveTextContent('Restock notice');
    expect(screen.getByTestId('offer-details')).toHaveTextContent('Back in about 3 days.');
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
