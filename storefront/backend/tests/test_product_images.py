import re
from pathlib import Path

import app.catalogue as catalogue

from app.catalogue import load_products


IMAGE_DIR = Path(__file__).parents[1] / "app" / "product-images"


def asset_name(product: dict) -> str:
    product_type = " ".join(product["name"].split()[:-1])
    slug = lambda value: re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return f"{slug(product_type)}--{slug(product['colour']['name'])}.jpg"


def test_every_catalogue_sku_has_a_photo():
    # Photos exist per (product type, colour); the model catalogue uses a subset of them.
    products = load_products()
    expected_assets = {asset_name(product) for product in products}
    runtime_assets = {path.name for path in IMAGE_DIR.glob("*.jpg")}

    assert len(products) == 200
    assert expected_assets <= runtime_assets


def test_jpeg_route_serves_catalogue_photo_with_cache_headers(make):
    client, _ = make()

    response = client.get("/img/P0042.jpg")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert "max-age" in response.headers["Cache-Control"]
    assert response.data == (IMAGE_DIR / asset_name(next(p for p in load_products() if p["product_id"] == "P0042"))).read_bytes()


def test_jpeg_route_rejects_invalid_unknown_and_missing_photo_mappings(make, monkeypatch, tmp_path):
    client, _ = make()

    for product_id in ("evil", "P9999"):
        response = client.get(f"/img/{product_id}.jpg")
        assert response.status_code == 404
        assert response.get_json()["error"] == "not_found"

    monkeypatch.setattr(catalogue, "PRODUCT_IMAGE_DIR", tmp_path)
    response = client.get("/img/P0042.jpg")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not_found"
