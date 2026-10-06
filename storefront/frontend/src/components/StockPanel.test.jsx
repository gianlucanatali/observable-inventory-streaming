import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import StockPanel from './StockPanel.jsx';

describe('StockPanel', () => {
  it('keeps an at-least answer in the available state but marks stale stock as amber', () => {
    render(<StockPanel stock={{
      status: 'available', sellable: 3, at_least: true,
      stores: [
        { store_id: 'S01', status: 'available', quantity: 2, feed: 'ok' },
        { store_id: 'S02', status: 'available', quantity: 1, feed: 'stale' },
      ],
    }} fetchError={null} compression={null} />);

    expect(screen.getByTestId('stock-panel')).toHaveClass('stock-available', 'stock-warning');
    expect(screen.getByTestId('stock-panel')).toHaveTextContent('At least 3 available online');
  });

  it('keeps each per-store availability value as a readable pill', () => {
    render(<StockPanel stock={{
      status: 'available', sellable: 3, at_least: false,
      stores: [
        { store_id: 'S01', status: 'available', quantity: 2, feed: 'ok' },
        { store_id: 'S02', status: 'available', quantity: 1, feed: 'stale' },
      ],
    }} fetchError={null} compression={null} />);

    const stores = screen.getByTestId('stock-stores');
    expect(stores).toHaveTextContent('By store: Milano 2 Torino 1 (not live)');
    expect(stores.querySelectorAll('.store-pill')).toHaveLength(2);
  });
});
