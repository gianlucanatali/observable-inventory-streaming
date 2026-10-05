"""Demo control parameters read from Redis hash `demo:config` (contracts section 12), with registry defaults."""
from __future__ import annotations

import sys

CONFIG_KEY = "demo:config"
_PARAMS = {  # key -> (default, min, max); mirrors contracts/demo-params.json
    "demand_skew": (1.0, 0.0, 3.0),
    "sell_out_gap_s": (1.5, 0.0, 30.0),
}


def read_param(r, key: str) -> float:
    """Value of `key` in demo:config; the registry default when the field is absent (said on stderr).
    A present but invalid or out-of-range value is an error, never silently replaced."""
    default, lo, hi = _PARAMS[key]
    raw = r.hget(CONFIG_KEY, key)
    if raw is None:
        print(f"scenario: {CONFIG_KEY} has no {key}, using the default {default}", file=sys.stderr)
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"{CONFIG_KEY}.{key} = {raw!r} is not a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{CONFIG_KEY}.{key} = {v} is outside {lo}..{hi}")
    return v
