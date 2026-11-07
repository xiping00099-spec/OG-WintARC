"""imaging.py - shared look & feel (colours, fonts, status squares) for chart.py and dashboard.py."""
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import scoring

BG = (10, 10, 10)
PANEL = (20, 20, 20)
PANEL_ALT = (15, 15, 15)
GRID = (40, 40, 40)
WHITE = (245, 245, 245)
MUTED = (140, 140, 140)
LIME = (140, 235, 55)
LIME_DIM = (70, 110, 35)
RED = (235, 50, 50)
AMBER = (255, 176, 32)
HEALTH_BLUE = (64, 170, 245)
OUTLINE = (85, 85, 85)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
_REGULAR = ["DejaVuSans.ttf", "NotoSans-Regular.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf",
            "SegoeUI.ttf", "segoeui.ttf", "Helvetica.ttc", "FreeSans.ttf"]
_BOLD = ["DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf",
         "segoeuib.ttf", "FreeSansBold.ttf"]


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False):
    """
    Font lookup order: FONT_PATH env var -> fonts/ folder in the project -> common system fonts ->
    Pillow's built-in scalable font. We never assume a specific system font exists.
    """
    size = max(8, int(size))
    candidates = []
    env = os.environ.get("FONT_PATH", "").strip()
    if env:
        candidates.append(env)
    if FONT_DIR.is_dir():
        files = sorted(FONT_DIR.glob("*.ttf")) + sorted(FONT_DIR.glob("*.otf"))
        pref = [f for f in files if ("bold" in f.name.lower()) == bold]
        candidates += [str(f) for f in pref + files]
    candidates += _BOLD if bold else _REGULAR
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1 ships a scalable fallback font
    except TypeError:
        return ImageFont.load_default()


def text_w(draw, text, fnt) -> float:
    return draw.textlength(text, font=fnt)


def fit(draw, text, fnt, max_w) -> str:
    """Shorten with an ellipsis so text never overflows its column."""
    if text_w(draw, text, fnt) <= max_w:
        return text
    while text and text_w(draw, text + "…", fnt) > max_w:
        text = text[:-1]
    return (text.rstrip() + "…") if text else "…"


def draw_status_square(d: ImageDraw.ImageDraw, box, status, scale=1.0, dim=False):
    x0, y0, x1, y1 = box
    r = max(2, int(4 * scale))
    w = max(1, int(1.5 * scale))
    if status == scoring.GREEN:
        d.rounded_rectangle(box, r, fill=LIME)
    elif status == scoring.HALF:
        d.rounded_rectangle(box, r, outline=LIME_DIM, width=w)
        d.polygon([(x1 - w, y0 + w), (x1 - w, y1 - w), (x0 + w, y1 - w)], fill=LIME)  # diagonal half
    elif status == scoring.RED:
        d.rounded_rectangle(box, r, fill=RED)
    elif status == scoring.PENDING:
        d.rounded_rectangle(box, r, fill=AMBER)
    elif status == scoring.HEALTH:
        d.rounded_rectangle(box, r, fill=HEALTH_BLUE)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        arm = (x1 - x0) * 0.24
        t = max(2, int((x1 - x0) * 0.12))
        d.rectangle([cx - arm, cy - t / 2, cx + arm, cy + t / 2], fill=WHITE)
        d.rectangle([cx - t / 2, cy - arm, cx + t / 2, cy + arm], fill=WHITE)
    else:  # EMPTY
        d.rounded_rectangle(box, r, outline=GRID if dim else OUTLINE, width=w)


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
