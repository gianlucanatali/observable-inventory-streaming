# Storefront product-photo sources

The checked-in key photographs and runtime variants are AI-generated studio images of fictional, unbranded outdoor products. Runtime serving uses only `backend/app/product-images`; these source utilities are not installed by the storefront.

## Add a colour from a key photo

Keys use magenta (`#E000E0`) for their main material so the utility can preserve shading while replacing that colour:

    python3 -m pip install numpy pillow
    python3 recolor.py keys/key-trailrunner.jpg '#2f6b4f' ../backend/app/product-images/trailrunner--forest-green.jpg

Use the same lowercase, hyphenated type and colour slugs as the catalogue. Review the resulting 800×800 JPEG before adding it, then run the backend photo-coverage test.

## Regenerate a key photo

`gen_keys.py` contains the portable prompts for all product types and writes JPEGs to its local `keys/` directory. It requires the Codex CLI only when regenerating source keys:

    python3 gen_keys.py

The prompts require a neutral studio background, no people, text, brands, logos, or labels. Regenerating a key does not change the runtime images until a reviewed variant is placed under `backend/app/product-images`.
