import React, { useCallback, useEffect, useState } from 'react';
import { getConfig, getOffer, postCart } from './api.js';
import { usePolling, useToast, useHashRoute } from './hooks.js';
import Home from './components/Home.jsx';
import ProductPage from './components/ProductPage.jsx';
import OfferCard from './components/OfferCard.jsx';
import Toast from './components/Toast.jsx';
import { initRum } from './rum.js';

export default function App() {
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);
  const [cart, setCart] = useState({ id: null, count: 0 });
  const [offer, setOffer] = useState(null);
  const [offerError, setOfferError] = useState(null);
  const { toast, show, clear } = useToast();
  const route = useHashRoute();

  useEffect(() => {
    getConfig().then((c) => {
      try {
        initRum(c.rum);
      } catch (err) {
        console.error('RUM disabled:', err);
      }
      setConfig(c);
    }).catch((err) => setConfigError(err.message));
  }, []);

  const offersOn = Boolean(config && config.offers_enabled);
  usePolling(() => getOffer(cart.id), config ? config.poll_ms : 1000, [cart.id], {
    enabled: offersOn && Boolean(cart.id),
    onResult: (r) => {
      setOffer(r.offer);
      setOfferError(null);
    },
    onError: (err) => {
      console.error(err);
      setOfferError(err.message);
    },
  });

  const sendCart = useCallback(async (eventType, productId) => {
    try {
      const r = await postCart({
        cart_id: cart.id || undefined,
        store_id: 'ONLINE',
        product_id: productId,
        event_type: eventType,
      });
      if (eventType === 'ADD') {
        setCart((c) => ({ id: r.cart_id, count: c.count + 1 }));
        show('Added to cart', 'success');
      } else {
        setCart({ id: null, count: 0 });
        setOffer(null);
        show('Cart abandoned', 'info');
      }
    } catch (err) {
      console.error(err);
      show(err.message, 'error');
    }
  }, [cart.id, show]);

  const offerCard = offersOn ? <OfferCard offer={offer} error={offerError} /> : null;

  return (
    <div className="page">
      <header className="top">
        <a className="brand" href="#/">UrbanStreet<span className="tagline">Outdoor &amp; trail gear · 5 stores in Italy</span></a>
        <div className="top-right">
          <div className="cart-badge" aria-label="Cart">Cart <span className="badge">{cart.count}</span></div>
        </div>
      </header>
      {configError && <div className="banner banner-error">Cannot load configuration: {configError}</div>}
      <main>
        {route.page === 'product'
          ? (
            <ProductPage key={route.productId} productId={route.productId} config={config} cart={cart} onCart={sendCart}>
              {offerCard}
            </ProductPage>
          )
          : <><Home />{offerCard}</>}
      </main>
      {toast && <Toast key={toast.id} message={toast.message} type={toast.type} onClose={clear} />}
    </div>
  );
}
