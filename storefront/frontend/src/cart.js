// The browser keeps only a pointer to the cart (its id); the contents live server side in Redis
// (GET /api/cart/<id>), so a reload shows the same cart.

export const CART_POINTER_KEY = 'urbanstreet.cart_id';
export const EMPTY_CART = { id: null, items: [], count: 0 };

export function readCartPointer() {
  try {
    return window.localStorage.getItem(CART_POINTER_KEY);
  } catch (err) {
    console.error('cart pointer: localStorage is not readable, starting a new cart', err);
    return null;
  }
}

export function writeCartPointer(cartId) {
  try {
    if (cartId) window.localStorage.setItem(CART_POINTER_KEY, cartId);
    else window.localStorage.removeItem(CART_POINTER_KEY);
  } catch (err) {
    console.error('cart pointer: localStorage is not writable, the cart will not survive a reload', err);
  }
}

// Cart response of POST /api/cart and GET /api/cart/<id> -> UI state. Throws when the shape is wrong.
export function toCart(body) {
  if (!body || typeof body.cart_id !== 'string' || !Array.isArray(body.items) || typeof body.count !== 'number') {
    throw new Error('cart response has no cart_id, items list and count');
  }
  return { id: body.cart_id, items: body.items, count: body.count };
}
