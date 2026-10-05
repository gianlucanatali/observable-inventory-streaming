"""Display-delay beacon validation: source change time -> backend receive time of the beacon (server clock)."""
from __future__ import annotations

from datetime import datetime, timezone


class BeaconRejected(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _parse(name: str, raw: object) -> datetime:
    if not isinstance(raw, str):
        raise BeaconRejected("invalid", f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BeaconRejected("invalid", f"{name} is not ISO-8601: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BeaconRejected("invalid", f"{name} has no timezone: {raw!r}")
    return parsed.astimezone(timezone.utc)


def display_delay_seconds(body: object, max_delay_s: int, received_at: datetime) -> float:
    """Delay = received_at (backend clock, tz-aware) - body.last_changed_at. The browser clock is never used."""
    if not isinstance(body, dict):
        raise BeaconRejected("invalid", "body must be a JSON object")
    if not isinstance(body.get("product_id"), str):
        raise BeaconRejected("invalid", "product_id must be a string")
    changed = _parse("last_changed_at", body.get("last_changed_at"))
    delay = (received_at.astimezone(timezone.utc) - changed).total_seconds()
    if delay < 0:
        raise BeaconRejected("negative", f"last_changed_at is {-delay:.3f}s after the backend receive time (clock skew?)")
    if delay > max_delay_s:
        raise BeaconRejected("too_large", f"delay {delay:.1f}s exceeds the {max_delay_s}s sanity limit")
    return delay
