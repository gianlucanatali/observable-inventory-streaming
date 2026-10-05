# Catalogue fixture

Generated, not committed (`catalogue.json` is git-ignored; the Docker build generates it and verifies the hash).

```
python catalogue/generate.py --seed 42 --products 3000 --out catalogue/catalogue.json
```

| Item | Value |
|---|---|
| Seed / products | 42 / 3000 (`P0001`..`P0200` stocked, `P0201`..`P3000` padding with nested attributes) |
| Size | 6,093,646 bytes |
| SHA-256 | `8ff7374ed39c4c888ecd0a742dc2eba02899995b9c450b05f4fd248d3e7f7357` |

The hash was produced with CPython 3.12.14 and re-verified at image build time on 3.12.15 (`random.Random` sequences are stable across 3.x for the calls used). It is exposed by `/readyz` (`catalogue_sha256`, mode `startup`). Changing the generator, seed or N changes the hash: update this file, `Dockerfile` (`CATALOGUE_SHA256`) and `overlay/contracts/README.md` if it quotes it.

Supported build-time calibration sizes are intentionally allow-listed and hash-checked:

| Products | SHA-256 |
|---:|---|
| 2000 | `e16118e2593a8d33c2021cc0870b946fffd782a92e6e1887335cec2a0e95e8df` |
| 2500 | `1ff388fae2df0f93c693d9a2a68d07ec7d6c857c79d78bdce229162cb4e0fc3c` |
| 3000 | `8ff7374ed39c4c888ecd0a742dc2eba02899995b9c450b05f4fd248d3e7f7357` |
| 3500 | `418b973b8670ddee40db412266bde90e824097decf784b0beed442a86cd18ca1` |
| 4000 | `d2e3e6c869a3d04fc8ff4e198c82b3dd8e31a35de70596fc46af3feba09c4dce` |
| 8000 | `3320f08aaf5f8f1bb4c05ce2e21e4d93fc2d6ab32f7a029bb8984554e699a19f` |

Select one with `CATALOGUE_PRODUCTS`; unsupported values fail the image build rather than bypassing verification.

P0042 is "Trailrunner GTX", brand "Alpenpace", size "EU 42". All names are fictional.

## Calibration (local Mac, not the demo host)

See `../README.md`. Re-run with `uv run --python 3.12 python bench.py 800 1000 1200`; recalibrate on the target EC2 instance before the talk.
