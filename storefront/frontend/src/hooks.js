import { useEffect, useRef, useState } from 'react';

// Calls `fn` now and every `ms` while `enabled`. Overlapping calls are skipped, so a slow
// backend never piles up requests. Results and errors are delivered to the callbacks.
export function usePolling(fn, ms, deps, { onResult, onError, enabled = true }) {
  const handlers = useRef({ onResult, onError });
  handlers.current = { onResult, onError };
  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        const result = await fn();
        if (!stopped) handlers.current.onResult(result);
      } catch (err) {
        if (!stopped) handlers.current.onError(err);
      } finally {
        busy = false;
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => {
      stopped = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled, ...deps]);
}

export function useToast() {
  const [toast, setToast] = useState(null);
  return { toast, show: (message, type = 'info') => setToast({ message, type, id: Date.now() }), clear: () => setToast(null) };
}

// Hash routing: '#/' is the home page, '#/product/<id>' a product page. Returns the current route.
export function parseHash(hash) {
  const m = /^#\/product\/(P\d{4})$/.exec(hash);
  return m ? { page: 'product', productId: m[1] } : { page: 'home' };
}

export function useHashRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return route;
}
