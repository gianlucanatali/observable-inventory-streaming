import React, { useCallback, useEffect, useState } from 'react';
import { getCart, getConfig, getOffer, postCart } from './api.js';
import { usePolling, useToast, useHashRoute } from './hooks.js';
import Home from './components/Home.jsx';
import ProductPage from './components/ProductPage.jsx';
import OfferCard from './components/OfferCard.jsx';
import CartDrawer from './components/CartDrawer.jsx';
import { EMPTY_CART, readCartPointer, toCart, writeCartPointer } from './cart.js';
import Toast from './components/Toast.jsx';
import { initRum, recordOfferAccepted } from './rum.js';

export default function App() {
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);
  const [cart, setCart] = useState(EMPTY_CART);
  const [cartOpen, setCartOpen] = useState(false);
  const [offer, setOffer] = useState(null);  // the most recent offer of the cart (home page)
  const [offers, setOffers] = useState([]);  // one per sold-out cart item (product page, cart drawer)
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

  // The browser keeps only the cart id; the contents come from the backend (Redis), so a reload keeps the cart.
  useEffect(() => {
    const cartId = readCartPointer();
    if (!cartId) return;
    getCart(cartId).then((r) => setCart(toCart(r))).catch((err) => {
      if (err.status === 404) {
        // Expired, emptied or from before a reset: the next Add to cart starts a new cart.
        writeCartPointer(null);
        return;
      }
      console.error(err);
      show(`Cannot load your cart: ${err.message}`, 'error');
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hasCart = Boolean(cart.id) && cart.count > 0;
  useEffect(() => {
    if (!hasCart) {
      setOffer(null);
      setOffers([]);
      setOfferError(null);
    }
  }, [hasCart]);

  const offersOn = Boolean(config && config.offers_enabled);
  usePolling(() => getOffer(cart.id), config ? config.poll_ms : 1000, [cart.id], {
    enabled: offersOn && hasCart,
    onResult: (r) => {
      setOffer(r.offer);
      // An older backend sends only `offer`.
      setOffers(Array.isArray(r.offers) ? r.offers : r.offer ? [r.offer] : []);
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
        setCart(toCart(r));
        writeCartPointer(r.cart_id);
        show('Added to cart', 'success');
      } else {
        setCart(EMPTY_CART);
        writeCartPointer(null);
        setOffer(null);
        setOffers([]);
        show('Cart abandoned', 'info');
      }
    } catch (err) {
      console.error(err);
      show(err.message, 'error');
    }
  }, [cart.id, show]);

  // Remove one product line: an ABANDON cart event for that product (what Flink reads as "no longer in the cart").
  const removeItem = useCallback(async (productId) => {
    try {
      const r = await postCart({ cart_id: cart.id, store_id: 'ONLINE', product_id: productId, event_type: 'ABANDON' });
      setCart(toCart(r));
      show('Removed from cart', 'info');
    } catch (err) {
      console.error(err);
      show(err.message, 'error');
    }
  }, [cart.id, show]);

  // Swap a sold-out item for an alternative: the usual cart events, ABANDON the sold-out product then ADD the other.
  // `accepted` is the Offer when the shopper takes its alternative ("Offer accepted", a RUM action).
  const swapItem = useCallback(async (fromId, toId, accepted = null) => {
    try {
      const removed = await postCart({ cart_id: cart.id, store_id: 'ONLINE', product_id: fromId, event_type: 'ABANDON' });
      setCart(toCart(removed));  // shown even if the ADD below fails
      const r = await postCart({ cart_id: cart.id, store_id: 'ONLINE', product_id: toId, event_type: 'ADD' });
      setCart(toCart(r));
      writeCartPointer(r.cart_id);
      recordOfferAccepted(accepted);
      show('Swapped in your cart', 'success');
    } catch (err) {
      console.error(err);
      show(err.message, 'error');
    }
  }, [cart.id, show]);

  const productOffer = route.page === 'product'
    ? offers.find((o) => o.original_product_id === route.productId) || null : null;
  const offerCard = offersOn
    ? <OfferCard offer={route.page === 'product' ? productOffer : offer} error={offerError} hasCart={hasCart} /> : null;

  return (
    <div className="page">
      <header className="top">
        <a className="brand" href="#/">UrbanStreet<span className="tagline">Outdoor &amp; trail gear · 5 stores in Italy</span></a>
        <div className="top-right">
          <button className="cart-badge" aria-expanded={cartOpen} onClick={() => setCartOpen((o) => !o)}>
            Cart <span className="badge" data-testid="cart-count">{cart.count}</span>
          </button>
          {cartOpen && <CartDrawer cart={cart} offers={offersOn ? offers : []} onRemove={removeItem}
            onSwap={swapItem} onClose={() => setCartOpen(false)} />}
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
          : <><Home />{(offer || offerError) && offerCard}</>}
      </main>
      {toast && <Toast key={toast.id} message={toast.message} type={toast.type} onClose={clear} />}
    </div>
  );
}
