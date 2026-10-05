"""Deterministic flat SVG illustrations per product, drawn from the display catalogue entry."""
from __future__ import annotations

import hashlib
from xml.sax.saxutils import escape


def _shade(hex_: str, f: float) -> str:
    """Darken (f<1) or lighten (f>1) a #rrggbb colour."""
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    if f <= 1:
        r, g, b = (int(c * f) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * (f - 1)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _shoe(c: str, d: str, l: str, uid: str) -> str:
    lugs = "".join(f'<rect x="{68 + i * 21}" y="283" width="12" height="9" rx="2" fill="#23262b"/>' for i in range(13))
    laces = "".join(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#fff" stroke-width="5" stroke-linecap="round"/>'
        for x1, y1, x2, y2 in [(176, 168, 198, 190), (200, 158, 222, 180), (224, 150, 246, 172)])
    return (
        # upper
        f'<path d="M60 262 C60 214 86 176 128 168 L164 128 C176 112 200 114 212 128 L226 146 '
        f'C262 160 318 196 338 232 C346 246 344 262 340 268 L60 268 Z" fill="{c}"/>'
        f'<path d="M128 168 C150 176 172 178 190 170 L164 128 Z" fill="{d}"/>'
        # heel counter and toe cap
        f'<path d="M60 262 C60 232 66 212 80 196 L104 214 C92 226 90 244 92 268 L60 268 Z" fill="{d}"/>'
        f'<path d="M296 206 C322 220 338 236 340 262 L340 268 L286 268 C300 252 304 230 296 206 Z" fill="{d}"/>'
        f'<path d="M170 128 C180 120 198 120 208 130 L218 142 L180 150 Z" fill="{l}"/>'
        f'{laces}'
        # swoosh stripe
        f'<path d="M112 244 C168 226 232 226 280 246" fill="none" stroke="{l}" stroke-width="9" stroke-linecap="round"/>'
        # contrasting midsole and outsole
        f'<path d="M52 262 L348 262 C352 280 346 286 336 286 L64 286 C52 286 48 276 52 262 Z" fill="#f4f1ea"/>'
        f'<path d="M56 286 L344 286 L340 296 L60 296 Z" fill="#23262b"/>{lugs}'
    )


def _jacket(c: str, d: str, l: str, uid: str) -> str:
    return (
        f'<path d="M150 96 L120 108 L66 170 L78 280 L112 286 L120 196 L128 330 L272 330 L280 196 '
        f'L288 286 L322 280 L334 170 L280 108 L250 96 Z" fill="{c}"/>'
        f'<path d="M120 196 L128 330 L200 330 L200 112 Z" fill="{l}" opacity="0.18"/>'
        f'<path d="M150 96 C160 126 240 126 250 96 L230 90 C214 106 186 106 170 90 Z" fill="{d}"/>'
        f'<rect x="196" y="112" width="8" height="218" fill="{d}"/>'
        f'<rect x="132" y="236" width="48" height="12" rx="6" fill="{d}"/>'
        f'<rect x="220" y="236" width="48" height="12" rx="6" fill="{d}"/>'
        f'<path d="M66 170 L120 196 M334 170 L280 196" stroke="{d}" stroke-width="6" fill="none"/>'
        f'<rect x="78" y="268" width="38" height="12" rx="5" fill="{d}"/><rect x="284" y="268" width="38" height="12" rx="5" fill="{d}"/>'
    )


def _shorts(c: str, d: str, l: str, uid: str) -> str:
    return (
        f'<path d="M112 110 L288 110 L312 310 L222 310 L200 196 L178 310 L88 310 Z" fill="{c}"/>'
        f'<rect x="112" y="110" width="176" height="26" fill="{d}"/>'
        f'<path d="M160 136 L158 168 M240 136 L242 168" stroke="{l}" stroke-width="6" stroke-linecap="round"/>'
        f'<path d="M200 136 L200 196" stroke="{d}" stroke-width="5"/>'
        f'<rect x="98" y="286" width="66" height="10" fill="{l}" opacity="0.5"/><rect x="236" y="286" width="66" height="10" fill="{l}" opacity="0.5"/>'
    )


def _cap(c: str, d: str, l: str, uid: str) -> str:
    return (
        f'<path d="M96 240 C96 150 150 112 208 112 C268 112 308 168 308 240 Z" fill="{c}"/>'
        f'<path d="M208 112 C236 150 240 200 232 240 L250 240 C256 190 246 140 208 112 Z" fill="{d}"/>'
        f'<path d="M150 118 C120 140 106 190 104 240 L124 240 C126 196 140 150 168 124 Z" fill="{l}" opacity="0.35"/>'
        f'<path d="M90 240 L308 240 C350 240 372 262 366 274 C340 262 200 262 90 262 Z" fill="{d}"/>'
        f'<circle cx="208" cy="110" r="9" fill="{d}"/>'
    )


def _beanie(c: str, d: str, l: str, uid: str) -> str:
    ribs = "".join(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2}" stroke="{d}" stroke-width="5" opacity="0.5"/>'
                   for x, y1, y2 in [(130, 130, 220), (160, 118, 220), (190, 112, 220), (220, 112, 220), (250, 118, 220), (280, 130, 220)])
    return (
        f'<path d="M92 252 C88 160 140 100 200 100 C260 100 312 160 308 252 Z" fill="{c}"/>{ribs}'
        f'<rect x="84" y="236" width="232" height="52" rx="14" fill="{d}"/>'
        f'<path d="M96 252 L304 252 M96 268 L304 268" stroke="{l}" stroke-width="3" opacity="0.5"/>'
        f'<circle cx="200" cy="92" r="26" fill="{l}"/><circle cx="192" cy="86" r="8" fill="#fff" opacity="0.3"/>'
    )


def _vest(c: str, d: str, l: str, uid: str) -> str:
    return (
        f'<path d="M132 84 L168 84 L176 150 L224 150 L232 84 L268 84 L300 150 L292 330 L108 330 L100 150 Z" fill="{c}"/>'
        f'<path d="M168 84 L176 150 L168 330 M232 84 L224 150 L232 330" stroke="{d}" stroke-width="10" fill="none"/>'
        f'<rect x="116" y="196" width="50" height="62" rx="10" fill="{d}"/><rect x="234" y="196" width="50" height="62" rx="10" fill="{d}"/>'
        f'<rect x="178" y="190" width="44" height="12" rx="6" fill="{l}"/><rect x="178" y="226" width="44" height="12" rx="6" fill="{l}"/>'
        f'<rect x="124" y="270" width="152" height="16" rx="8" fill="{l}" opacity="0.5"/>'
    )


def _poles(c: str, d: str, l: str, uid: str) -> str:
    def pole(x1, y1, x2, y2, ring):
        return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{d}" stroke-width="9" stroke-linecap="round"/>'
                f'<line x1="{x1}" y1="{y1}" x2="{x1 + (x2 - x1) * 0.28}" y2="{y1 + (y2 - y1) * 0.28}" stroke="{c}" stroke-width="22" stroke-linecap="round"/>'
                f'<circle cx="{x2}" cy="{y2 - 8}" r="{ring}" fill="none" stroke="{l}" stroke-width="5"/>')
    return pole(150, 70, 112, 340, 18) + pole(250, 70, 288, 340, 18)


def _gaiters(c: str, d: str, l: str, uid: str) -> str:
    return (
        f'<path d="M104 90 L180 90 L190 330 L96 330 Z" fill="{c}"/><path d="M220 90 L296 90 L304 330 L210 330 Z" fill="{c}"/>'
        f'<rect x="100" y="90" width="84" height="20" fill="{d}"/><rect x="216" y="90" width="84" height="20" fill="{d}"/>'
        f'<path d="M96 300 L190 300 M210 300 L304 300" stroke="{l}" stroke-width="8"/>'
        f'<path d="M142 120 L146 290 M258 120 L254 290" stroke="{d}" stroke-width="5" stroke-dasharray="10 8"/>'
    )


def _kind(p: dict):
    name = p["name"].lower()
    cat = p["category"]
    if cat == "footwear":
        return _shoe
    if cat == "apparel":
        return _shorts if "shorts" in name else _jacket
    for key, fn in (("cap", _cap), ("beanie", _beanie), ("vest", _vest), ("poles", _poles), ("gaiters", _gaiters)):
        if key in name:
            return fn
    return _vest


def render_svg(p: dict) -> str:
    """Flat illustration on a soft gradient, in the product's colour. No text, no external references."""
    c = p["colour"]["hex"]
    d, l = _shade(c, 0.68), _shade(c, 1.45)
    uid = hashlib.sha256(p["product_id"].encode()).hexdigest()[:6]
    bg1, bg2 = _shade(c, 1.88), _shade(c, 1.72)
    title = escape(f'{p["brand"]} {p["name"]}, {p["colour"]["name"]}')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" role="img" '
        f'aria-label="{escape(title, {chr(34): "&quot;"})}">'
        f'<title>{title}</title>'
        f'<defs><linearGradient id="bg{uid}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/></linearGradient></defs>'
        f'<rect width="400" height="400" fill="url(#bg{uid})"/>'
        f'<ellipse cx="200" cy="344" rx="130" ry="12" fill="#000" opacity="0.12"/>'
        f'{_kind(p)(c, d, l, uid)}'
        '</svg>'
    )
