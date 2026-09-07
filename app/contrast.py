"""WCAG contrast arithmetic.

Exists so the model never does this arithmetic itself (design spec §6.2). A wrong
contrast ratio is the perfect wrong-but-plausible failure: it looks like a fact,
it is stated with confidence, and nobody checks it.

Implements the relative-luminance and contrast-ratio definitions from WCAG 2.x.
"""
from __future__ import annotations

import re
from typing import Iterable

_RGB_RE = re.compile(
    r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+))?\s*\)")
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_color(value: str) -> tuple[float, float, float, float] | None:
    """Parse 'rgb(1,2,3)', 'rgba(1,2,3,0.5)' or '#aabbcc' -> (r, g, b, alpha)."""
    if not value:
        return None
    value = value.strip()
    m = _RGB_RE.match(value)
    if m:
        r, g, b = (float(m.group(i)) for i in (1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return r, g, b, a
    m = _HEX_RE.match(value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16)), 1.0
    return None


def _channel(c: float) -> float:
    cs = c / 255.0
    return cs / 12.92 if cs <= 0.04045 else ((cs + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Iterable[float]) -> float:
    r, g, b = list(rgb)[:3]
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float | None:
    f, b = parse_color(fg), parse_color(bg)
    if not f or not b:
        return None
    # Composite a translucent foreground over the background before comparing;
    # otherwise text at opacity < 1 reports a ratio it does not actually have.
    if f[3] < 1.0:
        f = tuple(f[i] * f[3] + b[i] * (1 - f[3]) for i in range(3)) + (1.0,)
    l1, l2 = relative_luminance(f), relative_luminance(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


def is_large_text(font_size_px: float, font_weight: str | int) -> bool:
    """WCAG 'large text': >= 18pt (24px), or >= 14pt (18.66px) when bold."""
    try:
        weight = int(str(font_weight).strip())
    except (TypeError, ValueError):
        weight = 700 if str(font_weight).strip().lower() in {"bold", "bolder"} else 400
    if font_size_px >= 24.0:
        return True
    return weight >= 700 and font_size_px >= 18.66


def required_ratio(font_size_px: float, font_weight: str | int,
                   level: str = "AA") -> float:
    large = is_large_text(font_size_px, font_weight)
    if level.upper() == "AAA":
        return 4.5 if large else 7.0
    return 3.0 if large else 4.5


def px(value: str | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"([\d.]+)", str(value or "").strip())
    return float(m.group(1)) if m else 16.0


def assess(fg: str, bg: str, font_size: str | float,
           font_weight: str | int, level: str = "AA") -> dict:
    """Full verdict: ratio, applicable threshold, and why that threshold."""
    size = px(font_size)
    ratio = contrast_ratio(fg, bg)
    needed = required_ratio(size, font_weight, level)
    large = is_large_text(size, font_weight)
    return {
        "foreground": fg,
        "background": bg,
        "ratio": ratio,
        "required": needed,
        "level": level.upper(),
        "font_size_px": size,
        "font_weight": str(font_weight),
        "is_large_text": large,
        "threshold_reason": (
            f"{'Large' if large else 'Normal'} text at {size:g}px weight "
            f"{font_weight} requires {needed}:1 at level {level.upper()}."
        ),
        "passes": (ratio is not None and ratio >= needed),
    }
