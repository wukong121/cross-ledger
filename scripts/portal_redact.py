"""Redact and annotate a portal screenshot (crop the browser chrome, mask / replace text, add red marks).

Originals live in docs/images/poc/portal/_raw/ (git-ignored); results go to docs/images/poc/portal/.
Coordinates are given in the coordinate space the image was *viewed* in and scaled by `scale`
(e.g. a 2496-px-wide screenshot viewed at 2000 px wide -> scale 2496 / 2000).

    from portal_redact import process, WIDE, ACCOUNT_WIDE
    process("p6.png", WIDE, crop_top=145, mask=[ACCOUNT_WIDE], boxes=[(345, 455, 840, 490)],
            labels=[((870, 760), "public access governed by the perimeter")])

Requires Pillow: `uv run --project app --with pillow python ...`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "docs/images/poc/portal/_raw"
OUT = ROOT / "docs/images/poc/portal"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
RED = (220, 30, 30)
WHITE, BLACK, ROWGREY, MASK = (255, 255, 255), (30, 30, 30), (237, 235, 233), (205, 205, 205)
WIDE = 2496 / 2000  # 2496-px screenshots viewed at 2000 px
ACCOUNT_WIDE = (1730, 150, 1995, 192)  # portal header: signed-in account + tenant
TENANT_LEFT = (20, 274, 345, 294)  # "Contoso (<tenant domain>)" under a blade title


def process(name, scale, crop_top, mask=(), replace=(), boxes=(), labels=()):
    """mask: grey blocks; replace: ((x0,y0,x1,y1), text, background); boxes: red frames; labels: red callouts."""
    im = Image.open(RAW / name).convert("RGB")
    d = ImageDraw.Draw(im)

    def s(v):
        return int(round(v * scale))

    for x0, y0, x1, y1 in mask:
        d.rectangle([s(x0), s(y0), s(x1), s(y1)], fill=MASK)
    for (x0, y0, x1, y1), text, bg in replace:
        d.rectangle([s(x0), s(y0), s(x1), s(y1)], fill=bg)
        f = ImageFont.truetype(FONT, s((y1 - y0) * 0.6))
        d.text((s(x0) + s(4), s(y0) + s((y1 - y0) * 0.17)), text, fill=BLACK, font=f)
    for x0, y0, x1, y1 in boxes:
        d.rectangle([s(x0), s(y0), s(x1), s(y1)], outline=RED, width=max(4, s(4)))
    for (x, y), text in labels:
        f = ImageFont.truetype(FONT, s(20))
        tw = d.textlength(text, font=f)
        d.rectangle([s(x) - s(6), s(y) - s(4), s(x) + tw + s(6), s(y) + s(28)], fill=RED)
        d.text((s(x), s(y)), text, fill="white", font=f)
    im = im.crop((0, s(crop_top), im.width, im.height))
    im.save(OUT / name, optimize=True)
    print(name, im.size)
