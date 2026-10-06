import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ProductCard from './ProductCard.jsx';

const model = {
  model_id: 'alpenpace-trailrunner-gtx', name: 'Trailrunner GTX', brand: 'Alpenpace', price_eur: 219.9,
  lead: { product_id: 'P0042' },
  colours: [{ name: 'Forest green', hex: '#2f6b4f' }, { name: 'Slate', hex: '#5a6678' }],
};

describe('ProductCard', () => {
  it('uses the JPEG route then switches once to the SVG fallback when loading fails', () => {
    render(<ProductCard model={model} />);

    const image = screen.getByRole('img', { name: 'Alpenpace Trailrunner GTX' });
    expect(image).toHaveAttribute('src', '/img/P0042.jpg');
    fireEvent.error(image);
    expect(image).toHaveAttribute('src', '/img/P0042.svg');
    fireEvent.error(image);
    expect(image).toHaveAttribute('src', '/img/P0042.svg');
  });

  it('shows the model once with its colour swatches and links to the lead SKU', () => {
    render(<ProductCard model={model} />);
    expect(screen.getByTestId('product-card')).toHaveAttribute('href', '#/product/P0042');
    expect(screen.getByTestId('card-swatches')).toHaveAttribute('aria-label', '2 colours: Forest green, Slate');
    expect(screen.getByText('€219,90')).toBeInTheDocument();
  });
});
