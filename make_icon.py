#!/usr/bin/env python3
"""
Draw the application icon: a trading vessel, in the style of a Final Fantasy
item icon.

The subject is a ship because that is what the app is actually about -- carrying
goods between two markets. The treatment follows the game's own icon language:
a jewel-tone ground, an ornate gold frame, and soft painterly shading rather
than flat vector fills.

Small sizes are drawn differently, not shrunk. Below 48px the frame filigree and
hull shading stop being detail and become noise, so those sizes keep only what
still reads at that scale: the ground, a plain gold edge, and the silhouette.

Run:  python make_icon.py     -> icon.ico (+ icon-preview.png)
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

# A Final Fantasy palette: deep indigo sky, warm gold metal, parchment canvas.
SKY_TOP = (40, 54, 98)
SKY_LOW = (18, 24, 48)
SEA_LIGHT = (46, 86, 124)
SEA_DARK = (12, 28, 54)
GOLD = (214, 172, 94)
GOLD_BRIGHT = (246, 220, 152)
GOLD_DEEP = (138, 100, 44)
SAIL = (238, 226, 198)
SAIL_SHADE = (192, 174, 138)
HULL = (104, 64, 36)
HULL_LIGHT = (158, 106, 58)

SIZES = [256, 128, 64, 48, 32, 16]
SUPER = 8


def vertical_gradient(size, top, bottom):
    """A soft top-to-bottom wash -- the base of the painterly look."""
    strip = Image.new("RGB", (1, size))
    px = strip.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return strip.resize((size, size), Image.BICUBIC)


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=radius, fill=255)
    return mask


def waves(d, s):
    """Gold crests under the hull, so it reads as sailing rather than parked."""
    for i, (y, width, colour) in enumerate(((0.815, 0.32, GOLD),
                                            (0.885, 0.24, GOLD_DEEP))):
        cx, span, steps = s * 0.5, s * width, 30
        pts = []
        for k in range(steps + 1):
            t = k / steps
            pts.append((cx - span + span * 2 * t,
                        s * y + math.sin(t * math.pi * 2 + i * 1.6) * s * 0.015))
        d.line(pts, fill=colour, width=max(int(s * 0.020), 1), joint="curve")


def ship(d, s, small):
    """Hull, mast and sails, centred on a square of side `s`."""
    cx = s * 0.50
    waterline = s * 0.715

    mast_w = max(int(s * (0.040 if small else 0.024)), 1)
    d.line([(cx - s * 0.045, s * 0.185), (cx - s * 0.045, waterline)],
           fill=GOLD_DEEP if small else HULL, width=mast_w)

    # Main sail: a bowed triangle, belly to the right, as if under wind.
    top = (cx - s * 0.045, s * 0.185)
    foot = (cx - s * 0.045, waterline - s * 0.045)
    belly = (cx + s * (0.26 if small else 0.29), s * 0.455)
    d.polygon([top, belly, foot], fill=SAIL)
    if not small:
        # Shade the lee side so the canvas reads as curved cloth. There was a
        # fore sail here too; it was too small to register at any size that
        # mattered and only crowded the mainsail.
        d.polygon([top, (cx + s * 0.135, s * 0.44), foot], fill=SAIL_SHADE)
        d.line([top, belly], fill=GOLD_BRIGHT, width=max(int(s * 0.011), 1))

    # Hull: a shallow trapezoid sitting on the waterline.
    left, right = cx - s * (0.26 if small else 0.30), cx + s * (0.26 if small else 0.30)
    d.polygon([(left, waterline - s * 0.030),
               (right, waterline - s * 0.030),
               (right - s * 0.075, waterline + s * 0.085),
               (left + s * 0.075, waterline + s * 0.085)],
              fill=GOLD_DEEP if small else HULL)
    if not small:
        d.polygon([(left, waterline - s * 0.030), (right, waterline - s * 0.030),
                   (right - s * 0.018, waterline + s * 0.014),
                   (left + s * 0.018, waterline + s * 0.014)], fill=HULL_LIGHT)
        d.line([(left, waterline - s * 0.028), (right, waterline - s * 0.028)],
               fill=GOLD, width=max(int(s * 0.013), 1))


def frame(d, s, small):
    """The gold border. Final Fantasy item icons are always framed."""
    radius = int(s * (0.18 if small else 0.20))
    outer = max(int(s * (0.050 if small else 0.030)), 1)
    d.rounded_rectangle([outer // 2, outer // 2,
                         s - 1 - outer // 2, s - 1 - outer // 2],
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


def draw(px):
    small = px < 48
    s = px * SUPER

    base = vertical_gradient(s, SKY_TOP, SKY_LOW).convert("RGBA")
    sea = vertical_gradient(s, SEA_LIGHT, SEA_DARK).convert("RGBA")
    band = Image.new("L", (s, s), 0)
    ImageDraw.Draw(band).rectangle([0, int(s * 0.665), s, s], fill=255)
    base.paste(sea, (0, 0), band)

    d = ImageDraw.Draw(base)
    if not small:
        waves(d, s)
    ship(d, s, small)
    frame(d, s, small)

    if not small:
        # A whisper of bloom on the gold: what makes it look painted rather
        # than drawn. Too much and the small sizes turn to soup.
        base = Image.blend(base, base.filter(ImageFilter.GaussianBlur(s * 0.010)),
                           0.25)

    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(base, (0, 0), rounded_mask(s, int(s * (0.18 if small else 0.20))))
    return out.resize((px, px), Image.LANCZOS)


def main():
    frames = [draw(px) for px in SIZES]
    ico = os.path.join(HERE, "icon.ico")
    frames[0].save(ico, format="ICO", sizes=[(p, p) for p in SIZES],
                   append_images=frames[1:])

    pad, gap = 16, 18
    sheet = Image.new("RGBA",
                      (pad * 2 + sum(SIZES) + gap * (len(SIZES) - 1),
                       pad * 2 + max(SIZES)), (40, 44, 56, 255))
    x = pad
    for px, img in zip(SIZES, frames):
        sheet.paste(img, (x, pad + (max(SIZES) - px) // 2), img)
        x += px + gap
    sheet.save(os.path.join(HERE, "icon-preview.png"))
    print(f"wrote {ico} ({os.path.getsize(ico) / 1024:.0f} KB, {len(SIZES)} sizes)")


if __name__ == "__main__":
    main()
