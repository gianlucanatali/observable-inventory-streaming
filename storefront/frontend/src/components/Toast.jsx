import React, { useEffect, useRef } from 'react';

// Adapted from https://github.com/ifnesi/retail-ai-demo/frontend/src/components/Toast.js (Apache-2.0); see NOTICE.
// Changes: JSX, no dangerouslySetInnerHTML option, new markup/CSS.
export default function Toast({ message, type = 'info', onClose, duration = 3000 }) {
  // The parent re-renders on every poll and passes a new onClose each time; keep it in a ref so the
  // timer is not restarted by those renders. A new toast remounts this component (key), which resets it.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const timer = setTimeout(() => onCloseRef.current(), duration);
    return () => clearTimeout(timer);
  }, [duration]);
  return (
    <div className={`toast toast-${type}`} role="status">
      <span>{message}</span>
      <button className="toast-close" onClick={onClose} aria-label="Dismiss">×</button>
    </div>
  );
}
