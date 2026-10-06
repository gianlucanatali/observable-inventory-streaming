import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ProductImage, { productImageUrls } from './ProductImage.jsx';

describe('ProductImage', () => {
  it('centralizes JPEG and SVG URLs and falls back only once', () => {
    expect(productImageUrls('P0042')).toEqual({ photo: '/img/P0042.jpg', fallback: '/img/P0042.svg' });
    render(<ProductImage productId="P0042" alt="Trailrunner GTX" />);

    const image = screen.getByRole('img', { name: 'Trailrunner GTX' });
    fireEvent.error(image);
    fireEvent.error(image);
    expect(image).toHaveAttribute('src', '/img/P0042.svg');
  });
});
