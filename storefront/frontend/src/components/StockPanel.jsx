import React from 'react';
import { describeStock } from '../stockView.js';

export default function StockPanel({ stock, fetchError, compression }) {
  const view = describeStock(stock, fetchError, compression);
  return (
    <div className={`stock stock-${view.tone}${view.warning ? ' stock-warning' : ''}`} aria-live="polite" data-testid="stock-panel">
      <div className="stock-title">{view.title}</div>
      {view.qualifier && <div className="stock-qualifier">{view.qualifier}</div>}
      {view.detail && <div className="stock-detail">{view.detail}</div>}
      {view.detail && view.clockNote && <div className="stock-clock muted" data-testid="stock-clock">{view.clockNote}</div>}
      {view.stores.length > 0 && (
        <div className="stock-stores" data-testid="stock-stores">
          <span className="store-label">By store:</span>{' '}
          {view.stores.map((s, i) => (
            <React.Fragment key={s.id}>
              {' '}
              <span className={`store-pill store-${s.tone}`}>{s.text}</span>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
