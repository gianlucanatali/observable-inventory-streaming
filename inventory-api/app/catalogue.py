"""Catalogue preparation: parse the JSON file and build the product index."""
import hashlib
import json
import re

from ddtrace import tracer

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s):
    return _NORM.sub(" ", s.lower()).strip()


def build_index(path):
    """Parse the catalogue and build lookup structures. Deliberately plain work."""
    with open(path, "rb") as f:
        doc = json.load(f)
    by_id = {}
    brands = {}
    categories = {}
    search = {}
    skus = {}
    related = {}
    for p in doc["products"]:
        pid = p["product_id"]
        by_id[pid] = p
        brands.setdefault(_norm(p["brand"]), []).append(pid)
        categories.setdefault(_norm(p["category"]), []).append(pid)
        for tok in set(_norm(p["name"]).split()):
            search.setdefault(tok, []).append(pid)
        for t in p.get("tags", ()):
            search.setdefault(_norm(t), []).append(pid)
        for tok in set(_norm(p.get("description", "")).split()):
            search.setdefault(tok, []).append(pid)
        for v in p.get("variants", ()):
            skus[v["sku"]] = (pid, _norm(v["colour"]), v["price_eur"])
        for r in p.get("related", ()):
            related.setdefault(r, []).append(pid)
    return {"by_id": by_id, "brands": brands, "categories": categories, "search": search,
            "skus": skus, "related": related}


class Catalogue:
    """Holds the index per mode. `loads` counts file reads (asserted in tests)."""

    def __init__(self, path, mode):
        self.path = path
        self.mode = mode
        self.loads = 0
        self.index = None
        self.sha256 = None

    def _prepare(self):
        with tracer.trace("catalogue.prepare") as span:
            span.set_tag("catalogue.mode", self.mode)
            self.loads += 1
            idx = build_index(self.path)
            span.set_metric("catalogue.products", len(idx["by_id"]))
            return idx

    def prepare_startup(self):
        self.sha256 = file_sha256(self.path)
        self.index = self._prepare()

    def product(self, product_id):
        """Return the product card for product_id, or None."""
        if self.mode == "none":
            return None
        idx = self._prepare() if self.mode == "per_request" else self.index
        if idx is None:
            raise RuntimeError("catalogue index requested before startup preparation")
        p = idx["by_id"].get(product_id)
        if p is None:
            return None
        return {"name": p["name"], "brand": p["brand"], "size": p["size"], "image_url": p["image_url"]}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
