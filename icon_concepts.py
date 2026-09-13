#!/usr/bin/env python3
"""
Candidate marks for the app icon, drawn in one shared Final Fantasy treatment
so they can be judged against each other rather than against their own framing.

Every concept gets the same ground, gold frame and shading; only the subject
changes. Run this, look at concepts.png, then the winner moves into
make_icon.py.

Run:  python icon_concepts.py
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

SKY_TOP, SKY_LOW = (40, 54, 98), (18, 24, 48)
SEA_LIGHT, SEA_DARK = (46, 86, 124), (12, 28, 54)
GOLD, GOLD_BRIGHT, GOLD_DEEP = (214, 172, 94), (246, 220, 152), (138, 100, 44)
SAIL, SAIL_SHADE = (238, 226, 198), (192, 174, 138)
HULL, HULL_LIGHT = (104, 64, 36), (158, 106, 58)
AETHER, AETHER_HOT = (108, 196, 226), (206, 244, 255)
SUPER = 6


def grad(size, top, bottom):
    strip = Image.new("RGB", (1, size))
    px = strip.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return strip.resize((size, size), Image.BICUBIC)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def frame(d, s, small):
    radius = int(s * (0.18 if small else 0.20))
    outer = max(int(s * (0.050 if small else 0.030)), 1)
    d.rounded_rectangle([outer // 2, outer // 2, s - 1 - outer // 2,
                         s - 1 - outer // 2],
                        radius=radius, outline=GOLD, width=outer)
    if small:
        return
    pad = outer + int(s * 0.024)
    d.rounded_rectangle([pad, pad, s - 1 - pad, s - 1 - pad],
                        radius=int(radius * 0.78), outline=GOLD_DEEP,
                        width=max(int(s * 0.010), 1))
    gem = s * 0.030
    for gx, gy in ((pad, pad), (s - pad, pad), (pad, s - pad), (s - pad, s - pad)):
        d.polygon([(gx, gy - gem), (gx + gem, gy), (gx, gy + gem), (gx - gem, gy)],
                  fill=GOLD_BRIGHT)


# ---------------------------------------------------------------- subjects

def subject_ship(d, s, small):
    """A trading vessel: what the app is about, carrying goods between markets."""
    cx, water = s * 0.5, s * 0.715
    d.line([(cx, s * 0.185), (cx, water)], fill=GOLD_DEEP if small else HULL,
           width=max(int(s * (0.040 if small else 0.024)), 1))
    top, foot = (cx + s * 0.010, s * 0.185), (cx + s * 0.010, water - s * 0.045)
    belly = (cx + s * (0.26 if small else 0.30), s * 0.455)
    d.polygon([top, belly, foot], fill=SAIL)
    if not small:
        d.polygon([top, (cx + s * 0.135, s * 0.44), foot], fill=SAIL_SHADE)
        d.polygon([(cx - s * 0.010, s * 0.305), (cx - s * 0.185, s * 0.545),
                   (cx - s * 0.010, water - s * 0.045)], fill=SAIL_SHADE)
    half = s * (0.26 if small else 0.30)
    d.polygon([(cx - half, water - s * 0.03), (cx + half, water - s * 0.03),
               (cx + half - s * 0.075, water + s * 0.085),
               (cx - half + s * 0.075, water + s * 0.085)],
              fill=GOLD_DEEP if small else HULL)
    if not small:
        d.line([(cx - half, water - s * 0.028), (cx + half, water - s * 0.028)],
               fill=GOLD, width=max(int(s * 0.013), 1))


def subject_airship(d, s, small):
    """The signature Final Fantasy transport, and how you cross a region."""
    cx, cy = s * 0.5, s * 0.52
    # Envelope
    env = [cx - s * 0.33, cy - s * 0.30, cx + s * 0.33, cy - s * 0.02]
    d.ellipse(env, fill=SAIL)
    if not small:
        d.ellipse([env[0], env[1], env[2], env[1] + (env[3] - env[1]) * 0.55],
                  fill=(250, 242, 222))
        d.line([(cx - s * 0.30, cy - s * 0.16), (cx + s * 0.30, cy - s * 0.16)],
               fill=GOLD, width=max(int(s * 0.016), 1))
    # Gondola
    d.polygon([(cx - s * 0.20, cy + s * 0.06), (cx + s * 0.20, cy + s * 0.06),
               (cx + s * 0.13, cy + s * 0.20), (cx - s * 0.13, cy + s * 0.20)],
              fill=GOLD_DEEP if small else HULL)
    if not small:
        d.line([(cx - s * 0.20, cy + s * 0.068), (cx + s * 0.20, cy + s * 0.068)],
               fill=GOLD, width=max(int(s * 0.013), 1))
        # Tail fin and a propeller, the bits that say "airship" not "balloon"
        d.polygon([(cx + s * 0.30, cy - s * 0.20), (cx + s * 0.40, cy - s * 0.30),
                   (cx + s * 0.38, cy - s * 0.10)], fill=GOLD)
        d.line([(cx - s * 0.36, cy + s * 0.02), (cx - s * 0.36, cy + s * 0.18)],
               fill=GOLD_BRIGHT, width=max(int(s * 0.016), 1))


def subject_aetheryte(d, s, small):
    """An aetheryte: the crystal you touch to travel between worlds."""
    cx, cy = s * 0.5, s * 0.50
    h, w = s * 0.30, s * 0.19
    body = [(cx, cy - h), (cx + w, cy), (cx, cy + h), (cx - w, cy)]
    if not small:
        halo = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(halo).polygon(
            [(cx, cy - h * 1.5), (cx + w * 1.5, cy), (cx, cy + h * 1.5),
             (cx - w * 1.5, cy)], fill=AETHER + (90,))
        d._image.alpha_composite(halo.filter(ImageFilter.GaussianBlur(s * 0.035)))
    d.polygon(body, fill=AETHER)
    d.polygon([(cx, cy - h), (cx + w, cy), (cx, cy + h)], fill=(78, 160, 196))
    if not small:
        d.line([(cx, cy - h), (cx, cy + h)], fill=AETHER_HOT,
               width=max(int(s * 0.012), 1))
        for dx, dy, sc in ((-0.30, -0.22, 0.30), (0.31, 0.20, 0.26),
                           (0.26, -0.30, 0.20)):
            sx, sy = cx + s * dx, cy + s * dy
            sh, sw = h * sc, w * sc
            d.polygon([(sx, sy - sh), (sx + sw, sy), (sx, sy + sh), (sx - sw, sy)],
                      fill=AETHER_HOT)


def subject_coin(d, s, small):
    """A gil coin with a rising line: the money, plainly."""
    cx, cy, r = s * 0.5, s * 0.5, s * 0.28
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD)
    if not small:
        d.arc([cx - r, cy - r, cx + r, cy + r], 135, 315, fill=GOLD_BRIGHT,
              width=max(int(s * 0.022), 1))
        d.arc([cx - r, cy - r, cx + r, cy + r], 315, 135, fill=GOLD_DEEP,
              width=max(int(s * 0.022), 1))
    w = max(int(s * (0.075 if small else 0.055)), 1)
    pts = ([(cx - r * 0.55, cy + r * 0.45), (cx + r * 0.50, cy - r * 0.45)]
           if small else
           [(cx - r * 0.60, cy + r * 0.34), (cx - r * 0.14, cy - r * 0.12),
            (cx + r * 0.14, cy + r * 0.06), (cx + r * 0.58, cy - r * 0.50)])
    d.line(pts, fill=SKY_LOW, width=w, joint="curve")
    tip, head = pts[-1], s * (0.11 if small else 0.085)
    d.polygon([tip, (tip[0] - head, tip[1] + head * 0.20),
               (tip[0] - head * 0.20, tip[1] + head)], fill=SKY_LOW)


def subject_route(d, s, small):
    """Two coins and an arc between them: the trade itself."""
    ly, ry = s * 0.66, s * 0.42
    lx, rx = s * 0.30, s * 0.70
    r1 = s * (0.15 if small else 0.13)
    r2 = s * (0.18 if small else 0.17)
    if not small:
        arc = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(arc).arc([lx - s * 0.02, ry - s * 0.22,
                                 rx + s * 0.02, ly + s * 0.10],
                                200, 340, fill=GOLD_BRIGHT + (255,),
                                width=max(int(s * 0.028), 1))
        d._image.alpha_composite(arc)
    d.ellipse([lx - r1, ly - r1, lx + r1, ly + r1], fill=GOLD_DEEP)
    d.ellipse([rx - r2, ry - r2, rx + r2, ry + r2], fill=GOLD)
    if not small:
        d.arc([rx - r2, ry - r2, rx + r2, ry + r2], 135, 315,
              fill=GOLD_BRIGHT, width=max(int(s * 0.020), 1))
        head = s * 0.075
        d.polygon([(rx + r2 * 0.2, ry - r2 * 1.15),
                   (rx + r2 * 0.2 - head, ry - r2 * 1.15 + head * 0.25),
                   (rx + r2 * 0.2 - head * 0.25, ry - r2 * 1.15 + head)],
                  fill=GOLD_BRIGHT)


CONCEPTS = [
    ("Trading ship", subject_ship, True),
    ("Airship", subject_airship, False),
    ("Aetheryte", subject_aetheryte, False),
    ("Gil coin", subject_coin, False),
    ("Trade route", subject_route, False),
]


def draw(subject, px, sea=True):
    small = px < 48
    s = px * SUPER
    base = grad(s, SKY_TOP, SKY_LOW).convert("RGBA")
    if sea:
        band = Image.new("L", (s, s), 0)
        ImageDraw.Draw(band).rectangle([0, int(s * 0.665), s, s], fill=255)
        base.paste(grad(s, SEA_LIGHT, SEA_DARK).convert("RGBA"), (0, 0), band)
    d = ImageDraw.Draw(base)
    if sea and not small:
        for i, (y, wdt, col) in enumerate(((0.815, 0.32, GOLD),
                                           (0.885, 0.24, GOLD_DEEP))):
            cx, span, steps = s * 0.5, s * wdt, 30
            pts = [(cx - span + span * 2 * (k / steps),
                    s * y + math.sin(k / steps * math.pi * 2 + i * 1.6) * s * 0.015)
                   for k in range(steps + 1)]
            d.line(pts, fill=col, width=max(int(s * 0.020), 1), joint="curve")
    subject(d, s, small)
    frame(d, s, small)
    if not small:
        base = Image.blend(base, base.filter(ImageFilter.GaussianBlur(s * 0.010)),
                           0.22)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(base, (0, 0), rounded_mask(s, int(s * (0.18 if small else 0.20))))
    return out.resize((px, px), Image.LANCZOS)


def main():
    hero, smalls = 150, (48, 32, 16)
    row_h = hero + 58
    sheet = Image.new("RGBA", (980, 40 + row_h * len(CONCEPTS)), (28, 31, 40, 255))
    d = ImageDraw.Draw(sheet)
    for i, (label, fn, sea) in enumerate(CONCEPTS):
        y = 30 + i * row_h
        d.text((30, y - 18), f"{chr(65 + i)}.  {label.upper()}",
               fill=(170, 180, 195, 255))
        big = draw(fn, hero, sea)
        sheet.paste(big, (30, y), big)
        x = 30 + hero + 40
        for px in smalls:
            f = draw(fn, px, sea)
            sheet.paste(f, (x, y + (hero - px) // 2), f)
            x += px + 26
        x += 20
        for px, factor in ((16, 6), (32, 4)):
            f = draw(fn, px, sea).resize((px * factor, px * factor), Image.NEAREST)
            sheet.paste(f, (x, y + (hero - px * factor) // 2), f)
            x += px * factor + 24
    out = os.path.join(HERE, "concepts.png")
    sheet.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
