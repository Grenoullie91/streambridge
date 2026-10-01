#!/usr/bin/env python3
"""Build the launcher icon, the status-bar icon and the web favicon.

The source is the master brand artwork the project owner supplies; the measured
crop below is the central "S with a play button" glyph. Only the glyph is used
for the icons: at 48 dp the equalizer bars that flank it in the wordmark lockup
are barely a pixel wide each, and an icon that cannot be picked out of a
launcher grid is not an icon. The bars are decoration for the icon, not the
icon.

    python3 scripts/make-android-icons.py <res-root> <web-assets-dir> <logo.png>

    <res-root>      android/app/src/main/res
    <web-assets-dir> src/streambridge/web/assets

Outputs:

    drawable-<density>/ic_launcher_foreground.png   adaptive foreground, 108dp
    drawable-<density>/ic_launcher_monochrome.png   flat shape for themed icons
    drawable-<density>/ic_notification.png          status-bar icon, 24dp
    drawable/ic_launcher_background.xml             flat tile colour
    mipmap-<density>/ic_launcher.png                legacy square icon
    mipmap-<density>/ic_launcher_round.png          legacy round icon
    <web-assets-dir>/favicon-<n>.png                browser favicon
    <web-assets-dir>/logo.png                      the mark on transparent

Requires Pillow. The adaptive background is the one layer that stays a vector,
because a flat colour has no reason to be a bitmap.
"""

from __future__ import annotations

import io
import pathlib
import sys

try:
    from PIL import Image, ImageDraw
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    raise SystemExit(
        "Pillow is not installed. This script is the only thing that needs it:\n"
        "    pip install -e '.[android]'\n"
        "The runtime itself has no dependencies; see docs/development.md."
    ) from None

# --- source artwork --------------------------------------------------------
#
# Measured off the supplied master (1280x640), not guessed: the glow-lit glyph
# occupies x 506..784, y 52..391.
MARK_BOX = (506, 52, 784, 391)

# #0B0B10 - the same tile the desktop UI and the Android theme use, so the
# icon does not read as a black hole on a dark launcher.
TILE = (11, 11, 16)

# --- geometry --------------------------------------------------------------
# An adaptive icon's outer eighths per side may be masked away. The glyph is
# taller than wide, so it is sized by its height: 68 of 108 units sits inside
# the 72-unit safe zone with a little air on either side.
ADAPTIVE_CANVAS_DP = 108
ADAPTIVE_MARK_FRACTION = 0.63  # of the 108dp canvas
# Legacy icons carry more of the tile, because there is no safe-zone inset to
# respect and the launcher adds its own mask.
LEGACY_MARK_FRACTION = 0.70
# The status bar gives a 24dp icon with no padding of its own.
NOTIFICATION_MARK_FRACTION = 0.86

DENSITIES = [("mdpi", 1), ("hdpi", 1.5), ("xhdpi", 2), ("xxhdpi", 3), ("xxxhdpi", 4)]

# Alpha keying. The artwork sits on pure black with a soft blue glow around
# the glyph, and the counters inside the "S" are black too - so alpha comes
# from brightness. The ramp starts above the glow (which peaks around 30 of
# 765) and finishes well below the darkest body colour, which keeps the
# anti-aliased edge from being chewed off.
ALPHA_FLOOR = 40
ALPHA_CEIL = 150


def load_mark(source: pathlib.Path) -> Image.Image:
    """The glyph, cropped, keyed to an alpha channel and trimmed.

    Brightness becomes alpha, and the colour is left exactly as it is. The
    artwork already sits on pure black, so compositing the source colour over
    any dark tile reproduces it unchanged - there is nothing to
    un-premultiply, and trying to "correct" for the key would darken every
    colour by the same factor, turning the white play button grey.
    """
    art = Image.open(source).convert("RGB").crop(MARK_BOX)
    out = Image.new("RGBA", art.size)
    source_pixels = art.load()
    pixels = out.load()
    for y in range(art.height):
        for x in range(art.width):
            r, g, b = source_pixels[x, y]
            luminance = r + g + b
            if luminance <= ALPHA_FLOOR:
                alpha = 0
            elif luminance >= ALPHA_CEIL:
                alpha = 255
            else:
                t = (luminance - ALPHA_FLOOR) / (ALPHA_CEIL - ALPHA_FLOOR)
                alpha = round(255 * t * t * (3 - 2 * t))
            pixels[x, y] = (r, g, b, alpha)
    return trim(out)


def trim(image: Image.Image) -> Image.Image:
    """Cut to the alpha bounding box, so all placement maths is about the mark."""
    bbox = image.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    return image.crop(bbox) if bbox else image


def fit(mark: Image.Image, box: int) -> Image.Image:
    """The mark scaled to fit a square *box*, aspect preserved."""
    scale = box / max(mark.width, mark.height)
    return mark.resize(
        (max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
        Image.LANCZOS,
    )


def centre(mark: Image.Image, canvas: int) -> Image.Image:
    """The mark centred on a transparent square canvas."""
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.alpha_composite(mark, ((canvas - mark.width) // 2, (canvas - mark.height) // 2))
    return out


def silhouette(mark: Image.Image) -> Image.Image:
    """A flat white shape carrying only the mark's alpha.

    For the themed-icon layer and the status bar, both of which are tinted by
    the system and must not carry their own colour.
    """
    out = Image.new("RGBA", mark.size, (255, 255, 255, 0))
    out.putalpha(mark.getchannel("A"))
    return out


def render(mark: Image.Image, size: int, fraction: float, round_icon: bool = False) -> Image.Image:
    """The mark on a tile, at *size* pixels, for a plain square canvas."""
    scale = 4  # supersample, then downscale: the edges matter more than the size
    out = size * scale
    tile = Image.new("RGBA", (out, out), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    if round_icon:
        draw.ellipse([0, 0, out - 1, out - 1], fill=(*TILE, 255))
    else:
        draw.rounded_rectangle([0, 0, out - 1, out - 1], radius=out * 0.22, fill=(*TILE, 255))
    tile.alpha_composite(centre(fit(mark, round(out * fraction)), out))
    return tile.resize((size, size), Image.LANCZOS)


def write_adaptive_layers(res: pathlib.Path, mark: Image.Image) -> None:
    """Foreground and monochrome layers, one 108dp bitmap per density."""
    for density, scale in DENSITIES:
        folder = res / f"drawable-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        side = round(ADAPTIVE_CANVAS_DP * scale)
        glyph = centre(fit(mark, round(side * ADAPTIVE_MARK_FRACTION)), side)
        glyph.save(folder / "ic_launcher_foreground.png")
        silhouette(glyph).save(folder / "ic_launcher_monochrome.png")


def write_notification(res: pathlib.Path, mark: Image.Image) -> None:
    """The status-bar icon: a flat 24dp silhouette, no tile behind it."""
    for density, scale in DENSITIES:
        folder = res / f"drawable-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        side = round(24 * scale)
        centre(fit(silhouette(mark), round(side * NOTIFICATION_MARK_FRACTION)), side).save(
            folder / "ic_notification.png"
        )


def write_background(res: pathlib.Path) -> None:
    """The adaptive background layer, kept as a vector because it is one colour."""
    folder = res / "drawable"
    folder.mkdir(parents=True, exist_ok=True)
    tile = "#%02X%02X%02X" % TILE  # noqa: UP031 - a fixed three-part format
    side = f"{ADAPTIVE_CANVAS_DP:g}"
    (folder / "ic_launcher_background.xml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<!--
  Flat, not a gradient: a launcher masks this layer to its own shape, and a
  gradient here reads as a smudge on some of them rather than as depth. It
  matches the desktop UI's tile colour so the icon does not look like a hole.
-->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="{side}dp"
    android:height="{side}dp"
    android:viewportWidth="{side}"
    android:viewportHeight="{side}">
    <path
        android:fillColor="{tile}"
        android:pathData="M0,0h{side}v{side}h-{side}z" />
</vector>
"""
    )


def write_legacy(res: pathlib.Path, mark: Image.Image) -> None:
    """Pre-26 launcher icons: square and round, at every density."""
    for density, scale in DENSITIES:
        folder = res / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        size = round(48 * scale)
        render(mark, size, LEGACY_MARK_FRACTION).save(folder / "ic_launcher.png")
        # roundIcon is requested by launchers that mask to a circle themselves.
        # Drawing the circle here rather than shipping a copy of the square
        # keeps the two genuinely different, which they should be.
        render(mark, size, LEGACY_MARK_FRACTION, round_icon=True).save(
            folder / "ic_launcher_round.png"
        )


def write_web_assets(assets: pathlib.Path, mark: Image.Image) -> None:
    """Favicon and brand mark for the bundled web interface.

    The page already asks for `/assets/logo.svg` for both the favicon and the
    header mark, and the web manifest asks for icon-192.svg and icon-512.svg.
    Those three URLs keep working and keep their content type; what changes is
    the artwork inside them, which is why nothing in the HTML had to move.

    Each SVG wraps the real rendered mark as an embedded PNG rather than
    tracing it into paths. The mark is a gradient ribbon; hand-tracing it would
    produce a second, worse version of the logo, and the one place a vector
    would win - a few kilobytes - is worth less than it looks like on a server
    that only ever answers from localhost.
    """
    assets.mkdir(parents=True, exist_ok=True)

    # Plain PNGs too, for the sizes a browser or a site actually requests.
    # 512 is not among them: the manifest asks for icon-512.svg, and a second
    # 512px raster of the same drawing is a hundred kilobytes for nothing.
    for size in (16, 32, 48, 96, 192):
        _save_png(render(mark, size, LEGACY_MARK_FRACTION), assets / f"favicon-{size}.png")

    for name, size in (("logo.svg", 256), ("icon-192.svg", 192), ("icon-512.svg", 512)):
        (assets / name).write_text(_svg_with_embedded_png(mark, size, name))


def _as_png_bytes(image: Image.Image) -> bytes:
    """PNG bytes, palette-quantised once the image is big enough for it to matter.

    The mark is a smooth gradient, which resamples into per-pixel noise that a
    24-bit PNG cannot compress. Dropping to 256 colours is invisible on a
    gradient and takes the 512px icon from 111 kB to a fraction of that.
    """
    if max(image.size) >= 96:
        image = image.convert("RGBA").quantize(colors=256, method=Image.FASTOCTREE)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _save_png(image: Image.Image, path: pathlib.Path) -> None:
    path.write_bytes(_as_png_bytes(image))


def _svg_with_embedded_png(mark: Image.Image, size: int, name: str) -> str:
    import base64

    legacy = _as_png_bytes(render(mark, size, LEGACY_MARK_FRACTION))
    encoded = base64.b64encode(legacy).decode("ascii")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!--
  StreamBridge brand mark. The artwork is the project's logo, rendered and
  embedded rather than traced into paths, so this file and the PNGs beside it
  are the same drawing. Regenerate with:

      python3 scripts/make-android-icons.py \
          android/app/src/main/res src/streambridge/web/assets <logo.png>
-->
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img"
     aria-label="StreamBridge">
  <image x="0" y="0" width="{size}" height="{size}" preserveAspectRatio="xMidYMid meet"
         xlink:href="data:image/png;base64,{encoded}" />
</svg>
"""


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    res = pathlib.Path(sys.argv[1])
    assets = pathlib.Path(sys.argv[2])
    source = pathlib.Path(sys.argv[3])
    if not source.is_file():
        print(f"source logo not found: {source}")
        return 2

    mark = load_mark(source)
    write_adaptive_layers(res, mark)
    write_notification(res, mark)
    write_background(res)
    write_legacy(res, mark)
    write_web_assets(assets, mark)

    print(f"mark: {mark.width}x{mark.height}")
    print(f"android resources: {res}")
    print(f"web assets:       {assets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
