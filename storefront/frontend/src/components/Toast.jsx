import React, { useEffect } from 'react';

// Adapted from https://github.com/ifnesi/retail-ai-demo/frontend/src/components/Toast.js (Apache-2.0); see NOTICE.
// Changes: JSX, no dangerouslySetInnerHTML option, new markup/CSS.
export default function Toast({ message, type = 'info', onClose, duration = 4000 }) {
  useEffect(() => {
    const timer = setTimeout(onClose, duration);
    return () => clearTimeout(timer);
  }, [onClose, duration]);
  return (
    <div className={`toast toast-${type}`} role="status">
      <span>{message}</span>
      <button className="toast-close" onClick={onClose} aria-label="Dismiss">×</button>
    </div>
  );
}
