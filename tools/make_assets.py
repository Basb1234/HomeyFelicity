"""Render the app and driver artwork at the sizes Homey requires."""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(r"C:\Users\Bas\PycharmProjects\HomeyFelicity")

TOP = (24, 46, 38)
BOTTOM = (31, 157, 85)
GLYPH = (255, 255, 255)
BOLT = (255, 209, 102)


def gradient(size):
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    return img.resize(size, Image.BICUBIC).convert("RGBA")


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def battery(draw, cx, cy, unit):
    """A battery outline with a lightning bolt, centred on (cx, cy)."""
    w = 22 * unit
    h = 34 * unit
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    cap_w = 9 * unit
    cap_h = 3.2 * unit

    rounded(draw, (x0 - cap_w / 2 + w / 2, y0 - cap_h, x0 + cap_w / 2 + w / 2, y0 + unit),
            radius=1.4 * unit, fill=GLYPH)
    rounded(draw, (x0, y0, x1, y1), radius=3.4 * unit, fill=GLYPH)
    inset = 2.6 * unit
    rounded(draw, (x0 + inset, y0 + inset, x1 - inset, y1 - inset),
            radius=1.8 * unit, fill=(0, 0, 0, 0))

    bolt = [
        (cx + 3.4 * unit, cy - 11 * unit),
        (cx - 5.2 * unit, cy + 1.6 * unit),
        (cx - 0.4 * unit, cy + 1.6 * unit),
        (cx - 3.0 * unit, cy + 11 * unit),
        (cx + 5.6 * unit, cy - 1.8 * unit),
        (cx + 0.8 * unit, cy - 1.8 * unit),
    ]
    draw.polygon(bolt, fill=BOLT)


def sun(draw, cx, cy, r):
    """A soft warm disc with rays, suggesting the PV side of the system."""
    for i in range(8):
        a = i * math.pi / 4
        x, y = cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45
        rr = r * 0.17
        draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(255, 209, 102, 90))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 209, 102, 110))


def app_image(size):
    w, h = size
    img = gradient(size)
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    sun(draw, w * 0.80, h * 0.26, min(w, h) * 0.15)
    battery(draw, w * 0.38, h * 0.50, min(w, h) / 50)
    img.alpha_composite(overlay)
    return img.convert("RGB")


def driver_image(size):
    w, h = size
    img = gradient(size)
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    sun(draw, w * 0.77, h * 0.23, min(w, h) * 0.12)
    battery(draw, w * 0.5, h * 0.52, min(w, h) / 46)
    img.alpha_composite(overlay)
    return img.convert("RGB")


def main():
    for name, size in (("small", (250, 175)), ("large", (500, 350)), ("xlarge", (1000, 700))):
        path = ROOT / "assets" / "images" / f"{name}.png"
        app_image(size).save(path)
        print("wrote", path, size)

    for name, size in (("small", (75, 75)), ("large", (500, 500)), ("xlarge", (1000, 1000))):
        path = ROOT / "drivers" / "battery" / "assets" / "images" / f"{name}.png"
        driver_image(size).save(path)
        print("wrote", path, size)


if __name__ == "__main__":
    main()
