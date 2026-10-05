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
  it('renders the presenter decision explanation for a below-threshold Jev suggestion', () => {
    render(<OfferCard offer={offer} error={null} />);

    expect(screen.getByTestId('offer-decision')).toHaveTextContent(
      'Decided by: safe rule — AI suggested notify me at 0.76, below 0.8');
  });

  it('does not imply an AI suggestion when Jev returned no decision data', () => {
    render(<OfferCard offer={{ ...offer, decision_reason: 'timeout', jev_choice: null, jev_confidence: null }} error={null} />);

    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: safe rule — AI did not return a decision');
    expect(screen.getByTestId('offer-decision')).not.toHaveTextContent('suggested');
  });

  it('credits an accepted Jev decision to AI rather than the safe rule', () => {
    render(<OfferCard offer={{ ...offer, decision_route: 'JEV', decision_reason: 'accepted', jev_confidence: 0.86 }} error={null} />);

    expect(screen.getByTestId('offer-decision')).toHaveTextContent('Decided by: AI — selected notify me at 0.86, meeting 0.8');
  });
});
