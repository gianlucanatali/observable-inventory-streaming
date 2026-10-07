"""Pure evaluation of what the smoke test observed. No I/O here: everything takes already-fetched data."""
from __future__ import annotations

from dataclasses import dataclass, asdict

REQUIRED_CONNECTORS = ("inventory-s01", "inventory-s02", "inventory-s03", "inventory-s04", "inventory-s05",
                       "sellable-redis")
UNKNOWN_TITLE = "can't be confirmed"


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}  {self.detail}"

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_connectors(listing: object, required: tuple[str, ...] = REQUIRED_CONNECTORS) -> Result:
    """`listing` is the body of GET /connectors?expand=status."""
    name = "connect: connectors and tasks RUNNING"
    if not isinstance(listing, dict) or not listing:
        return Result(name, False, f"no connectors listed (body: {str(listing)[:120]})")
    problems = [f"{c} missing" for c in required if c not in listing]
    for cname, info in sorted(listing.items()):
        status = info.get("status", {}) if isinstance(info, dict) else {}
        state = status.get("connector", {}).get("state")
        if state != "RUNNING":
            problems.append(f"{cname} connector {state}")
        tasks = status.get("tasks", [])
        if not tasks:
            problems.append(f"{cname} has no tasks")
        for t in tasks:
            if t.get("state") != "RUNNING":
                problems.append(f"{cname} task {t.get('id')} {t.get('state')}")
    if problems:
        return Result(name, False, "; ".join(problems))
    return Result(name, True, f"{len(listing)} connectors, all tasks RUNNING")


def evaluate_availability(product: str, status_code: int, body: object) -> Result:
    name = f"api: availability {product}"
    if status_code != 200:
        return Result(name, False, f"HTTP {status_code}")
    if not isinstance(body, dict):
        return Result(name, False, "body is not a JSON object")
    problems = []
    if body.get("product_id") != product:
        problems.append(f"product_id {body.get('product_id')!r}")
    if body.get("status") not in ("available", "out_of_stock"):
        problems.append(f"status {body.get('status')!r} (unknown_reason {body.get('unknown_reason')!r})")
    if body.get("feed") != "ok":
        problems.append(f"feed {body.get('feed')!r}")
    stores = body.get("stores")
    if not isinstance(stores, list) or not stores:
        problems.append("no stores in the answer")
    else:
        problems += [f"store {s.get('store_id')} feed {s.get('feed')!r}" for s in stores if s.get("feed") != "ok"]
    if problems:
        return Result(name, False, "; ".join(problems))
    return Result(name, True, f"status {body['status']}, sellable {body.get('sellable')}, confirmed_min {body.get('confirmed_min')}, "
                              f"release {body.get('release')}, {len(stores)} stores feed ok")


def evaluate_product_page(availability_text: str, footer_text: str) -> Result:
    """Texts of the stock panel and the footer after the first availability answer rendered."""
    name = "browser: product page availability"
    problems = []
    low = availability_text.lower()
    if UNKNOWN_TITLE in low:
        problems.append("availability can't be confirmed")
    elif "available online" not in low and "out of stock online" not in low:
        problems.append(f"no availability answer in {availability_text[:100]!r}")
    if "not live" in low:
        problems.append("a store shows 'not live'")
    if "Serving release" not in footer_text:
        problems.append("footer has no 'Serving release'")
    elif footer_text.rstrip().endswith("–"):
        problems.append("footer shows no release")
    if problems:
        return Result(name, False, "; ".join(problems))
    return Result(name, True, f"{' '.join(availability_text.split())[:90]} | {' '.join(footer_text.split())}")


def evaluate_browser_noise(page: str, console_errors: list[str], failed_requests: list[str]) -> Result:
    name = f"browser: {page} console and network clean"
    problems = [f"console error: {m}" for m in console_errors] + [f"request failed: {r}" for r in failed_requests]
    if problems:
        return Result(name, False, "; ".join(problems)[:600])
    return Result(name, True, "no console errors, no failed requests")


def evaluate_catalogue(card_count: int) -> Result:
    name = "browser: home catalogue cards"
    if card_count < 1:
        return Result(name, False, "no product cards rendered")
    return Result(name, True, f"{card_count} product cards")


def evaluate_control(page_status: int, page_text: str, params_status: int, params: object) -> Result:
    name = "control: panel and parameters"
    if page_status != 200:
        return Result(name, False, f"GET /control/ HTTP {page_status}")
    if "Demo control" not in page_text:
        return Result(name, False, "GET /control/ is not the control panel page")
    if params_status != 200 or not isinstance(params, dict):
        return Result(name, False, f"GET /control/api/params HTTP {params_status}")
    rows = params.get("params")
    if not isinstance(rows, list) or not rows:
        return Result(name, False, "no parameters listed")
    bad = [f"{r.get('key')} {r.get('detail')}" for r in rows if r.get("status") == "error"]
    if bad:
        return Result(name, False, "parameters in error: " + "; ".join(bad))
    return Result(name, True, f"{len(rows)} parameters, stack {params.get('stack')}")


def evaluate_cart(step: str, status_code: int, body: object) -> Result:
    name = f"offers: cart {step}"
    if status_code != 201 or not isinstance(body, dict) or not body.get("cart_id"):
        return Result(name, False, f"HTTP {status_code}, body {str(body)[:120]}")
    return Result(name, True, f"201, cart {body['cart_id']}")
