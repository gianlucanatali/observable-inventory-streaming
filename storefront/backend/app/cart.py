"""Cart event construction and validation (contracts §2 carts.events, avro/cart_event.avsc), and the
cart contents read model in Redis (contracts §4 `cart:{scenario_id}:{cart_id}`)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

ONLINE = "ONLINE"  # the shop is online; every cart event carries this store_id
PRODUCT_RE = re.compile(r"^P\d{4}$")
EVENT_TYPES = ("ADD", "ABANDON")
CART_ID_RE = re.compile(r"^cart-[0-9a-f]{12}$")
# Cart contents live this long after the last change; a scenario reset changes scenario_id, which also starts new carts.
CART_TTL_S = 2 * 3600


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
        if not isinstance(cart_id, str) or not CART_ID_RE.match(cart_id):
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


def cart_key(scenario_id: str, cart_id: str) -> str:
    return f"cart:{scenario_id}:{cart_id}"


def apply_cart_event(redis, value: dict, ttl_s: int = CART_TTL_S) -> dict[str, int]:
    """Update the cart contents with a published cart event and return them ({product_id: quantity}).

    ADD adds one of the product; ABANDON removes the product's line, as Flink's latest event per cart item does.
    Same handler as the Kafka publish, so Kafka is never read back in the request path.
    """
    key = cart_key(value["scenario_id"], value["cart_id"])
    pipe = redis.pipeline()
    if value["event_type"] == "ADD":
        pipe.hincrby(key, value["product_id"], 1)
    else:
        pipe.hdel(key, value["product_id"])
    pipe.expire(key, ttl_s)
    pipe.hgetall(key)
    return _quantities(pipe.execute()[-1])


def read_cart(redis, scenario_id: str, cart_id: str) -> dict[str, int]:
    """Cart contents ({product_id: quantity}); empty when the cart expired, was emptied or belongs to another scenario."""
    return _quantities(redis.hgetall(cart_key(scenario_id, cart_id)))


def _quantities(raw: dict) -> dict[str, int]:
    return {str(product_id): int(qty) for product_id, qty in sorted(raw.items())}
