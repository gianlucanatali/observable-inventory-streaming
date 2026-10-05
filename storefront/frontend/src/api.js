// Thin fetch wrappers. Every non-2xx response throws an Error that says which call failed and why.

async function request(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error(`${method} ${url} failed: network error (${err.message})`);
  }
  let payload = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`${method} ${url} returned HTTP ${res.status} with a non-JSON body`);
    }
  }
  if (!res.ok) {
    const detail = payload && (payload.message || payload.error) ? `: ${payload.message || payload.error}` : '';
    const err = new Error(`${method} ${url} returned HTTP ${res.status}${detail}`);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

export const getConfig = () => request('GET', '/config');
export const getAvailability = (product) => request('GET', `/api/availability/${encodeURIComponent(product)}`);
export const getProducts = () => request('GET', '/api/products');
export const getProduct = (id) => request('GET', `/api/products/${encodeURIComponent(id)}`);
export const postCart = (body) => request('POST', '/api/cart', body);
export const getOffer = (cartId) => request('GET', `/api/offers?cart_id=${encodeURIComponent(cartId)}`);
export const postDisplayBeacon = (body) => request('POST', '/api/beacon/display', body);
