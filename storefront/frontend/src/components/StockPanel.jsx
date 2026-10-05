import React from 'react';
import { describeStock } from '../stockView.js';

export default function StockPanel({ stock, fetchError, compression }) {
  const view = describeStock(stock, fetchError, compression);
  return (
    <div className={`stock stock-${view.tone}`} aria-live="polite" data-testid="stock-panel">
      <div className="stock-title">{view.title}</div>
      {view.qualifier && <div className="stock-qualifier">{view.qualifier}</div>}
      {view.detail && <div className="stock-detail">{view.detail}</div>}
      {view.detail && view.clockNote && <div className="stock-clock muted" data-testid="stock-clock">{view.clockNote}</div>}
      {view.stores.length > 0 && (
        <div className="stock-stores" data-testid="stock-stores">
          By store:{' '}
          {view.stores.map((s, i) => (
            <React.Fragment key={s.id}>
              {i > 0 && ' · '}
              <span className={`store-${s.tone}`}>{s.text}</span>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
