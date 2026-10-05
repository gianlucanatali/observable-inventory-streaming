# Storefront

Fictional outdoor/trail retailer UrbanStreet (Vite + React frontend, Flask backend).

`backend/app/products.json` is the storefront's own display catalogue (name, brand, price, colour, description for P0001..P0200). It is generated from `inventory-api/catalogue/generate.py` (seed 42) and checked in; regenerate it with `python3 storefront/scripts/export_products.py` from `overlay/`.
