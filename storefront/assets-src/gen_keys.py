#!/usr/bin/env python3
"""Generate one product photo per (product type, colour) used by the storefront catalogue, via Codex image generation."""
import collections, json, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent
OUT = ASSETS_DIR / "keys"
OUT.mkdir(exist_ok=True)
CAT = ASSETS_DIR.parent / "backend" / "app" / "products.json"

STYLE = ("Square 1:1 e-commerce product photo, photorealistic studio shot, the single product centred with generous margin "
         "on a seamless very light neutral grey background (#EEF0EE), soft natural shadow, premium outdoor and trail-running "
         "brand look. Absolutely no text, no logos, no brand marks, no labels, no people.")
TYPES = {
    "Trailrunner": "a low-cut trail-running shoe, side view facing left, mesh upper, cushioned midsole, aggressive outsole lugs",
    "Pathfinder": "a lightweight low-cut hiking shoe, side view facing left, synthetic upper with protective toe cap",
    "Dolomia": "a mid-cut waterproof hiking boot, side view facing left, padded ankle collar, rugged lugged sole",
    "Brenta": "an approach shoe for rocky trails, side view facing left, suede-look upper, sticky rubber rand around the toe",
    "Cresta": "a stiff high-cut mountaineering boot, side view facing left, rubber rand, crampon-compatible welt",
    "Rifugio": "a classic leather trekking boot, side view facing left, mid-high cut, metal lace hooks",
    "Sentinel": "an insulated winter hiking boot, side view facing left, high cut, fleece-lined collar, deep lugs",
    "Base Layer": "a long-sleeve technical base layer top, front view on an invisible mannequin, fine knit fabric",
    "Fleece Midlayer": "a zip-neck fleece midlayer pullover, front view on an invisible mannequin",
    "Rain Jacket": "a lightweight hooded waterproof rain jacket, front view on an invisible mannequin, taped seams, matte fabric",
    "Softshell": "a softshell jacket with a stand-up collar, front view on an invisible mannequin, stretch woven fabric",
    "Windshell": "an ultralight packable wind jacket with hood, front view on an invisible mannequin, thin ripstop fabric",
    "Trek Shorts": "a pair of trekking shorts, front view laid flat, stretch fabric, zipped side pockets",
    "Gaiters": "a pair of trail gaiters standing upright side by side, with underfoot strap and front hook",
    "Hydration Vest": "a trail-running hydration vest, front view, with two soft water flasks in the chest pockets",
    "Merino Beanie": "a merino wool beanie hat, front view, fine rib knit",
    "Trail Cap": "a lightweight running cap, three-quarter view, perforated side panels",
    "Trekking Poles": "a pair of collapsible trekking poles crossed diagonally, black foam grips and wrist straps",
}

def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def jobs():
    for base, desc in TYPES.items():
        yield f"key-{slug(base)}.jpg", (f"{desc}. COLOUR RULE (important, it will be recoloured later): the main material is ONE flat saturated "
            "magenta (#E000E0) with natural shading; all secondary parts (soles, zips, straps, grips, trims, laces) are plain white, "
            "black or neutral grey; NO other colours or accents anywhere, nothing else magenta.")

def run(job):
    name, product = job
    if (OUT / name).exists():
        return name, "skip"
    prompt = f"Generate ONE image with your image generation tool and save it in the current directory as {name}. {STYLE} Product: {product} After saving, print the path."
    for attempt in range(2):
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", prompt],
                           cwd=OUT, capture_output=True, text=True, timeout=600)
        if (OUT / name).exists():
            return name, "ok"
    return name, f"FAILED rc={r.returncode} {r.stderr[-200:]!r}"

if __name__ == "__main__":
    js = list(jobs())
    print(f"{len(js)} images", flush=True)
    with ThreadPoolExecutor(6) as ex:
        for name, status in ex.map(run, js):
            print(name, status, flush=True)
