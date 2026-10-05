"""Cart event construction and validation (contracts §2 carts.events, avro/cart_event.avsc)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

ONLINE = "ONLINE"  # the shop is online; every cart event carries this store_id
PRODUCT_RE = re.compile(r"^P\d{4}$")
EVENT_TYPES = ("ADD", "ABANDON")


class ValidationError(ValueError):
    pass


def _shopper_signals(cart_id: str) -> dict:
    """Stable synthetic signals derived from the opaque cart id; no real shopper data enters the demo."""
    digest = hashlib.sha256(cart_id.encode()).digest()
    return {
        "cart_value_eur": float(50 + int.from_bytes(digest[:2], "big") % 450),
        "returning_shopper": bool(digest[2] & 1),
        "item_count": 1 + digest[3] % 5,
    }


def build_cart_event(body: object, scenario_id: str, now_ms: int | None = None) -> tuple[dict, dict]:
    """Return (key, value) for carts.events. Raises ValidationError with a client-readable message."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    store_id = body.get("store_id", ONLINE)
    product_id = body.get("product_id")
    event_type = body.get("event_type")
    cart_id = body.get("cart_id")
    if store_id != ONLINE:
        raise ValidationError(f"store_id must be {ONLINE} (or omitted): the shop is online")
    if not isinstance(product_id, str) or not PRODUCT_RE.match(product_id):
        raise ValidationError("product_id must look like P0042")
    if event_type not in EVENT_TYPES:
        raise ValidationError("event_type must be ADD or ABANDON")
    if cart_id is not None:
        if not isinstance(cart_id, str) or not re.match(r"^cart-[0-9a-f]{12}$", cart_id):
            raise ValidationError("cart_id must be a value previously returned by this API")
    elif event_type == "ABANDON":
        raise ValidationError("ABANDON requires cart_id")
    else:
        cart_id = f"cart-{uuid.uuid4().hex[:12]}"
    # Synthetic shopper: derived from the cart so it is stable but never a real person.
    shopper_id = "shopper-" + hashlib.sha256(cart_id.encode()).hexdigest()[:8]
    value = {
        "event_id": str(uuid.uuid4()),
        "scenario_id": scenario_id,
        "cart_id": cart_id,
        "shopper_id": shopper_id,
        "store_id": ONLINE,
        "product_id": product_id,
        "event_type": event_type,
        "event_time": now_ms if now_ms is not None else int(time.time() * 1000),
        **_shopper_signals(cart_id),
    }
    return {"cart_id": cart_id}, value
