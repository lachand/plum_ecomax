"""Generate the integration's brand images (icon and logo) with Pillow.

    python tools/make_brand_assets.py

Writes custom_components/plum_ecomax/brand/{icon,logo,dark_logo}.png and their
@2x versions, as Home Assistant's local brand images expect (icon: square, logo:
shorter side 128-256 px). The artwork is original -- a flame on a plum-coloured
tile, plus the product name in plain type -- and deliberately does not reproduce
Plum's own logo; this is an independent community integration.

Needs Pillow (not a dependency of the integration): pip install pillow
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "custom_components" / "plum_ecomax" / "brand"
FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

SS = 4  # supersampling factor
TILE_TOP = (142, 36, 170)  # plum
TILE_BOTTOM = (216, 27, 96)  # raspberry
AMBER = (255, 193, 7)
WHITE = (255, 255, 255)


def bezier(p0, p1, p2, p3, steps=40):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def path(start, *curves):
    pts = [start]
    cur = start
    for c1, c2, end in curves:
        pts += bezier(cur, c1, c2, end)[1:]
        cur = end
    return pts


# Flame outlines on a 1024 grid.
OUTER = path(
    (520, 150),
    ((596, 290), (776, 370), (776, 570)),
    ((776, 725), (664, 840), (512, 840)),
    ((360, 840), (248, 725), (248, 590)),
    ((248, 470), (306, 400), (362, 350)),
    ((372, 430), (402, 472), (446, 492)),
    ((428, 390), (452, 250), (520, 150)),
)
INNER = path(
    (512, 470),
    ((566, 545), (640, 596), (640, 676)),
    ((640, 756), (584, 800), (512, 800)),
    ((440, 800), (384, 756), (384, 676)),
    ((384, 606), (434, 560), (512, 470)),
)


def gradient_tile(size: int) -> Image.Image:
    """A vertical plum -> raspberry gradient clipped to a rounded square."""
    grad = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / (size - 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM, strict=True))
        draw.line([(0, y), (size, y)], fill=colour)
    mask = Image.new("L", (size, size), 0)
    margin = round(size * 0.04)
    ImageDraw.Draw(mask).rounded_rectangle(
        [margin, margin, size - margin, size - margin], radius=round(size * 0.22), fill=255
    )
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    return tile


def icon_canvas(size: int = 1024) -> Image.Image:
    tile = gradient_tile(size)
    draw = ImageDraw.Draw(tile)
    scale = size / 1024
    draw.polygon([(x * scale, y * scale) for x, y in OUTER], fill=WHITE)
    draw.polygon([(x * scale, y * scale) for x, y in INNER], fill=AMBER)
    return tile


def resized(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return img.resize(size, Image.Resampling.LANCZOS)


def logo_canvas(height: int, dark: bool) -> Image.Image:
    """Icon on the left, "Plum" over "ecoMAX" on the right (transparent background)."""
    icon = icon_canvas(height)
    small = ImageFont.truetype(FONT_BOLD, round(height * 0.34))
    big = ImageFont.truetype(FONT_BOLD, round(height * 0.44))
    plum_colour = (225, 190, 231) if dark else (142, 36, 170)
    name_colour = (255, 255, 255) if dark else (43, 43, 51)
    gap = round(height * 0.10)
    text_w = max(round(small.getlength("Plum")), round(big.getlength("ecoMAX")))
    width = height + gap + text_w + round(height * 0.06)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(icon, (0, 0))
    draw = ImageDraw.Draw(canvas)
    x = height + gap
    draw.text((x, round(height * 0.17)), "Plum", font=small, fill=plum_colour)
    draw.text((x, round(height * 0.46)), "ecoMAX", font=big, fill=name_colour)
    return canvas


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big_icon = icon_canvas(1024)
    for name, px in (("icon.png", 256), ("icon@2x.png", 512)):
        resized(big_icon, (px, px)).save(OUT / name, optimize=True)

    for dark, prefix in ((False, ""), (True, "dark_")):
        render = logo_canvas(256 * SS, dark)  # 1024 px tall
        width = round(render.width / SS)  # 1x width; @2x is exactly double
        for suffix, factor in (("", 1), ("@2x", 2)):
            size = (width * factor, 256 * factor)
            resized(render, size).save(OUT / f"{prefix}logo{suffix}.png", optimize=True)
    for path_ in sorted(OUT.glob("*.png")):
        with Image.open(path_) as im:
            print(f"{path_.name}: {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()
