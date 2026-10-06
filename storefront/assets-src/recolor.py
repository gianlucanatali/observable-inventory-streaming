#!/usr/bin/env python3
"""Recolour magenta key areas of a product photo to a target hex, keeping shading."""
import sys, colorsys
import numpy as np
from PIL import Image

def recolor(src, hex_, dst):
    im = np.asarray(Image.open(src).convert("RGB")).astype(np.float32) / 255
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    mx, mn = im.max(-1), im.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    # magenta: red and blue high, green low
    key = np.clip(((np.minimum(r, b) - g) / np.maximum(mx, 1e-6) - 0.15) / 0.35, 0, 1) * (sat > 0.18)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    ref = np.median(lum[key > 0.9]) if (key > 0.9).any() else 0.5     # typical key brightness
    tr, tg, tb = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    shade = (lum / max(ref, 1e-3))[..., None]                          # 1.0 = mid-tone of the key area
    target = np.array([tr, tg, tb])
    col = np.where(shade <= 1, target * shade, target + (1 - target) * np.clip(shade - 1, 0, 1) * 0.6)
    out = im * (1 - key[..., None]) + np.clip(col, 0, 1) * key[..., None]
    Image.fromarray((out * 255).astype(np.uint8)).save(dst)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: recolor.py INPUT.jpg '#rrggbb' OUTPUT.jpg")
    recolor(*sys.argv[1:4])
