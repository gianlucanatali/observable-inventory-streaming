"""Background sales on/off from the panel, without docker.

The five jr containers read `demo_setting.sales_per_min_per_store` on every 250 ms tick (overlay/jr/sales.json), so a
rate of 0 stops every background sale at once and a positive rate restarts them. "Off" remembers the rate in Redis
`demo:sales-rate-saved` and writes 0 through the normal parameter path (Control.set: all five sources, read-back, Datadog
"demo config" event); "On" writes the remembered rate back (or the registry default).

Both read back the effect in the sources: the highest `sale` revision per store must stop moving (off) or move (on).
This cannot start or stop the jr containers: when they are stopped (`make sales-off`, `make reset`), "On" fails and says
so; `make sales-on` starts them.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("demo_control")

RATE_KEY = "sales_per_min_per_store"
SAVED_KEY = "demo:sales-rate-saved"
SALE_REVISION_SQL = "SELECT coalesce(max(revision), 0) FROM stock_position WHERE change_reason = 'sale'"


class SalesError(RuntimeError):
    pass


def store_probe(stores: tuple[tuple[str, str], ...], connect: Callable[[str], object]) -> Callable[[], dict[str, int]]:
    """Highest `sale` revision per store source (revisions grow on every change; a sale stamps change_reason 'sale')."""
    def probe() -> dict[str, int]:
        out = {}
        for sid, host in stores:
            try:
                with connect(host) as conn:
                    with conn.cursor() as cur:
                        cur.execute(SALE_REVISION_SQL)
                        out[sid] = int(cur.fetchone()[0])
            except Exception as exc:  # noqa: BLE001 - re-raised with the store named
                raise SalesError(f"cannot read the sale revisions of store {sid} at {host}: {exc}") from exc
        return out
    return probe


class BackgroundSales:
    def __init__(self, read_rate: Callable[[], float], set_rate: Callable[[float], float], default_rate: float,
                 redis_client, probe: Callable[[], dict[str, int]], sleep=time.sleep, clock=time.monotonic,
                 grace_s: float = 1.0, quiet_s: float = 3.0, poll_s: float = 1.0):
        self._read, self._set, self._default = read_rate, set_rate, default_rate
        self._r, self._probe, self._sleep, self._clock = redis_client, probe, sleep, clock
        self._grace, self._quiet, self._poll = grace_s, quiet_s, poll_s

    def saved(self) -> float | None:
        raw = self._r.get(SAVED_KEY)
        if raw is None:
            return None
        try:
            return float(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            raise SalesError(f"Redis {SAVED_KEY} = {raw!r} is not a number; delete it or set the rate in the "
                             "parameter table") from None

    def rate(self) -> float:
        """The live rate in the store sources (raises SalesError when it cannot be read)."""
        return self._read()

    def view(self) -> dict:
        return {"rate": self._read(), "saved": self.saved(), "default": self._default}

    def _write(self, rate: float) -> None:
        got = self._set(rate)
        if got != rate:
            raise SalesError(f"{RATE_KEY} reads back {got} after writing {rate}")

    @staticmethod
    def _moved(before: dict[str, int], after: dict[str, int]) -> list[str]:
        return [s for s in after if after[s] > before.get(s, 0)]

    def off(self, progress: Callable[[str], None]) -> dict:
        rate, saved = self._read(), self.saved()
        if rate > 0:
            self._r.set(SAVED_KEY, repr(rate))
            saved = rate
            self._write(0.0)
            progress(f"Background sales rate set to 0 in every store (was {rate:g}/min); checking that sales stop")
        else:
            progress("Background sales rate was already 0; checking that no sale is recorded")
        self._sleep(self._grace)  # a jr statement already running when the rate changed may still commit
        before = self._probe()
        self._sleep(self._quiet)
        moved = self._moved(before, self._probe())
        if moved:
            raise SalesError(f"rate is 0 but store(s) {', '.join(moved)} still recorded sales "
                             f"{self._quiet:.0f} s later; check the jr-sales containers on the VM")
        log.info("background sales off", extra={"fields": {"event": "demo_sales", "rate": 0, "saved": saved}})
        progress(f"Background sales are off (rate 0; {saved:g}/min remembered for Sales on)" if saved
                 else "Background sales are off (rate 0)")
        return {"rate": 0.0, "saved": saved, "stores_checked": sorted(before)}

    def on(self, progress: Callable[[str], None]) -> dict:
        rate = self._read()
        target = rate if rate > 0 else (self.saved() or self._default)
        if rate <= 0:
            self._write(target)
            progress(f"Background sales rate set to {target:g}/min per store; waiting for a sale")
        else:
            progress(f"Background sales rate is already {rate:g}/min per store; waiting for a sale")
        before = self._probe()
        stores = len(before) or 1
        # Expected gap between sales over all stores is 60/(rate*stores) s; wait about five gaps (10..60 s).
        window = min(60.0, max(10.0, 300.0 / (target * stores)))
        start = self._clock()
        while self._clock() - start < window:
            self._sleep(self._poll)
            moved = self._moved(before, self._probe())
            if moved:
                self._r.delete(SAVED_KEY)
                log.info("background sales on", extra={"fields": {"event": "demo_sales", "rate": target}})
                progress(f"Background sales are on: {target:g}/min per store; sale seen in {', '.join(moved)}")
                return {"rate": target, "sale_seen_in": moved}
        raise SalesError(f"rate is {target:g}/min per store but no background sale was recorded in any store for "
                         f"{window:.0f} s: the jr-sales containers are probably stopped (make sales-on starts them; "
                         "make sales-off and make reset stop them)")
