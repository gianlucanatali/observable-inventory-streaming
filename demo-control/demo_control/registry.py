"""The parameter registry (contracts/demo-params.json) and value validation."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

STORES = ("redis", "kafka_config", "procurement_db", "store_dbs", "flink_statement")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    unit: str
    default: float
    min: float
    max: float
    store: str
    consumers: tuple[str, ...]
    apply: str
    layer: str
    doc: str

    @property
    def stores(self) -> tuple[str, ...]:
        """`store` may list several comma-separated stores; writes go to all, the first is the read-back truth."""
        return tuple(x.strip() for x in self.store.split(","))

    @property
    def read_only(self) -> bool:
        return self.apply == "restart"

    def validate(self, value) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{self.key}: value must be a number, got {value!r}")
        v = float(value)
        if not math.isfinite(v):
            raise ValidationError(f"{self.key}: value must be finite, got {value!r}")
        if self.read_only:
            raise ValidationError(f"{self.key}: read-only (apply: {self.apply}); change it where it is defined and re-apply")
        if self.unit == "bool" and v not in (0.0, 1.0):
            raise ValidationError(f"{self.key}: must be 0 or 1, got {value!r}")
        if not self.min <= v <= self.max:
            raise ValidationError(f"{self.key}: {value!r} is outside the allowed range {fmt(self.min)}..{fmt(self.max)} {self.unit}")
        return v


def fmt(v: float) -> str:
    """Canonical text: integers without a trailing .0."""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def load_registry(path: str) -> dict[str, Param]:
    try:
        doc = json.loads(Path(path).read_text())
        params = {}
        for p in doc["params"]:
            for st in p["store"].split(","):
                if st.strip() not in STORES:
                    raise ValueError(f"{p['key']}: unknown store {st.strip()!r} in {p['store']!r}")
            params[p["key"]] = Param(p["key"], p["label"], p["unit"], p["default"], p["min"], p["max"], p["store"],
                                     tuple(p["consumers"]), p["apply"], p["layer"], p["doc"])
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit(f"demo-control: cannot load registry {path}: {type(exc).__name__}: {exc}") from exc
    if not params:
        raise SystemExit(f"demo-control: registry {path} has no params")
    return params
