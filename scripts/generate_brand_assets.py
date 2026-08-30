#!/usr/bin/env python3
"""Generate final Ambilight Hue Sync branding assets."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "assets" / "branding"
BRAND_DIR = ROOT / "custom_components" / "hue_entertainment" / "brand"

BASE = 256
SCALE = 6
MAGENTA = (221, 30, 135, 255)
CORAL = (255, 78, 93, 255)
AMBER = (255, 168, 13, 255)
YELLOW = (255, 220, 19, 255)
CYAN = (9, 177, 210, 255)
MAGENTA_HIGHLIGHT = (255, 103, 194, 255)
AMBER_HIGHLIGHT = (255, 210, 91, 255)
CYAN_HIGHLIGHT = (100, 231, 242, 255)
SCREEN = (5, 17, 30, 255)
TEXT_LIGHT = (24, 34, 53, 255)
TEXT_DARK = (237, 243, 250, 255)
TEXT_LIGHT_SECONDARY = (70, 86, 111, 255)
TEXT_DARK_SECONDARY = (184, 198, 217, 255)


def _scaled(values: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(value * SCALE for value in values)


def _interpolate(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
    amount: float,
) -> tuple[int, int, int, int]:
    return tuple(round(left[index] + (right[index] - left[index]) * amount) for index in range(4))


def _spectrum(width: int, height: int) -> Image.Image:
    stops = ((0.0, MAGENTA), (0.28, CORAL), (0.56, YELLOW), (1.0, CYAN))
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    for x in range(width):
        position = x / max(width - 1, 1)
        color = stops[-1][1]
        for left, right in zip(stops, stops[1:], strict=True):
            if left[0] <= position <= right[0]:
                amount = (position - left[0]) / (right[0] - left[0])
                color = _interpolate(left[1], right[1], amount)
                break
        draw.line((x, 0, x, height), fill=color)
    return image


def _placed_spectrum(box: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", (BASE * SCALE, BASE * SCALE))
    x1, y1, x2, y2 = box
    gradient = _spectrum((x2 - x1) * SCALE, (y2 - y1) * SCALE)
    layer.alpha_composite(gradient, (x1 * SCALE, y1 * SCALE))
    return layer


def _quadratic_points(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    count: int = 40,
) -> list[tuple[int, int]]:
    points = []
    for step in range(count + 1):
        position = step / count
        inverse = 1 - position
        x = (
            inverse * inverse * start[0]
            + 2 * inverse * position * control[0]
            + position * position * end[0]
        )
        y = (
            inverse * inverse * start[1]
            + 2 * inverse * position * control[1]
            + position * position * end[1]
        )
        points.append((round(x * SCALE), round(y * SCALE)))
    return points


def _rounded_line(
    layer: Image.Image,
    points: list[tuple[int, int]],
    width: int,
    fill: tuple[int, int, int, int],
) -> None:
    draw = ImageDraw.Draw(layer)
    draw.line(points, width=width * SCALE, fill=fill, joint="curve")
    radius = width * SCALE // 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _solid_glow(
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    blur: int,
    opacity: int,
) -> Image.Image:
    size = (BASE * SCALE, BASE * SCALE)
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).ellipse(_scaled(box), fill=opacity)
    mask = mask.filter(ImageFilter.GaussianBlur(blur * SCALE))
    glow = Image.new("RGBA", size, (*color, 255))
    glow.putalpha(mask)
    return glow


def _colored_glows(dark: bool) -> Image.Image:
    result = Image.new("RGBA", (BASE * SCALE, BASE * SCALE))
    ambient = 175 if dark else 112
    active = 195 if dark else 128

    result.alpha_composite(_solid_glow((43, 75, 105, 190), MAGENTA[:3], 17, ambient))
    result.alpha_composite(_solid_glow((72, 53, 187, 108), AMBER[:3], 18, ambient))
    result.alpha_composite(_solid_glow((151, 73, 217, 191), CYAN[:3], 17, ambient))

    result.alpha_composite(_solid_glow((23, 66, 61, 190), MAGENTA[:3], 9, active))
    result.alpha_composite(_solid_glow((80, 27, 174, 71), AMBER[:3], 9, active))
    result.alpha_composite(_solid_glow((194, 66, 231, 191), CYAN[:3], 9, active))
    result.alpha_composite(_solid_glow((70, 180, 118, 222), MAGENTA[:3], 9, active))
    result.alpha_composite(_solid_glow((103, 180, 153, 222), AMBER[:3], 9, active))
    result.alpha_composite(_solid_glow((137, 180, 186, 222), CYAN[:3], 9, active))
    return result


def _add_highlight(
    layer: Image.Image,
    center: tuple[int, int],
    color: tuple[int, int, int, int],
) -> None:
    x, y = center
    glow = _solid_glow((x - 5, y - 5, x + 5, y + 5), color[:3], 3, 205)
    layer.alpha_composite(glow)
    ImageDraw.Draw(layer).ellipse(
        _scaled((x - 2, y - 2, x + 2, y + 2)),
        fill=color,
    )


def _screen_layer() -> Image.Image:
    size = (BASE * SCALE, BASE * SCALE)
    outer_mask = Image.new("L", size)
    inner_mask = Image.new("L", size)
    ImageDraw.Draw(outer_mask).rounded_rectangle(
        _scaled((54, 72, 202, 176)), radius=25 * SCALE, fill=255
    )
    ImageDraw.Draw(inner_mask).rounded_rectangle(
        _scaled((57, 75, 199, 173)), radius=22 * SCALE, fill=255
    )
    ring_mask = ImageChops.subtract(outer_mask, inner_mask)
    ring = Image.composite(
        _placed_spectrum((54, 72, 202, 176)),
        Image.new("RGBA", size),
        ring_mask,
    )

    surface = Image.new("RGBA", size)
    ImageDraw.Draw(surface).rounded_rectangle(
        _scaled((57, 75, 199, 173)), radius=22 * SCALE, fill=SCREEN
    )
    surface.alpha_composite(ring)
    return surface.rotate(
        -4,
        resample=Image.Resampling.BICUBIC,
        center=(128 * SCALE, 124 * SCALE),
    )


def _physical_lights() -> Image.Image:
    size = (BASE * SCALE, BASE * SCALE)
    lights = Image.new("RGBA", size)

    _rounded_line(
        lights,
        _quadratic_points((38, 79), (27, 126), (37, 168)),
        15,
        MAGENTA,
    )
    _add_highlight(lights, (38, 87), MAGENTA_HIGHLIGHT)

    top = Image.new("RGBA", size)
    ImageDraw.Draw(top).rounded_rectangle(_scaled((91, 40, 163, 57)), radius=9 * SCALE, fill=AMBER)
    _add_highlight(top, (100, 48), AMBER_HIGHLIGHT)
    _add_highlight(top, (153, 49), AMBER_HIGHLIGHT)
    lights.alpha_composite(
        top.rotate(
            -4,
            resample=Image.Resampling.BICUBIC,
            center=(127 * SCALE, 49 * SCALE),
        )
    )

    _rounded_line(
        lights,
        _quadratic_points((211, 79), (221, 127), (211, 171)),
        15,
        CYAN,
    )
    _add_highlight(lights, (211, 162), CYAN_HIGHLIGHT)

    bottom_mask = Image.new("L", size)
    ImageDraw.Draw(bottom_mask).rounded_rectangle(
        _scaled((84, 194, 172, 211)), radius=9 * SCALE, fill=255
    )
    bottom = Image.composite(
        _placed_spectrum((84, 194, 172, 211)),
        Image.new("RGBA", size),
        bottom_mask,
    )
    _add_highlight(bottom, (95, 202), MAGENTA_HIGHLIGHT)
    _add_highlight(bottom, (128, 203), AMBER_HIGHLIGHT)
    _add_highlight(bottom, (162, 203), CYAN_HIGHLIGHT)
    lights.alpha_composite(
        bottom.rotate(
            4,
            resample=Image.Resampling.BICUBIC,
            center=(128 * SCALE, 203 * SCALE),
        )
    )
    return lights


def _draw_icon(size: int, dark: bool) -> Image.Image:
    master = Image.new("RGBA", (BASE * SCALE, BASE * SCALE))
    master.alpha_composite(_colored_glows(dark))
    master.alpha_composite(_screen_layer())
    master.alpha_composite(_physical_lights())
    return master.resize((size, size), Image.Resampling.LANCZOS)


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _draw_logo(width: int, height: int, dark: bool) -> Image.Image:
    factor = height / 224
    oversample = 2
    canvas = Image.new("RGBA", (width * oversample, height * oversample))
    icon_size = round(208 * factor * oversample)
    icon = _draw_icon(icon_size, dark)
    canvas.alpha_composite(icon, (round(8 * factor * oversample), round(8 * factor * oversample)))

    draw = ImageDraw.Draw(canvas)
    primary = TEXT_DARK if dark else TEXT_LIGHT
    secondary = TEXT_DARK_SECONDARY if dark else TEXT_LIGHT_SECONDARY
    x = round(230 * factor * oversample)
    draw.text(
        (x, round(50 * factor * oversample)),
        "Ambilight",
        font=_font(round(62 * factor * oversample), bold=True),
        fill=primary,
        anchor="lm",
    )
    draw.text(
        (x, round(136 * factor * oversample)),
        "Hue Sync",
        font=_font(round(52 * factor * oversample), bold=False),
        fill=secondary,
        anchor="lm",
    )
    return canvas.resize((width, height), Image.Resampling.LANCZOS)


def _svg_defs(dark: bool) -> str:
    ambient = ".70" if dark else ".45"
    active = ".78" if dark else ".52"
    return f'''<defs>
  <linearGradient id="spectrum" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#DD1E87"/>
    <stop offset=".28" stop-color="#FF4E5D"/>
    <stop offset=".56" stop-color="#FFDC13"/>
    <stop offset="1" stop-color="#09B1D2"/>
  </linearGradient>
  <filter id="ambient" x="-70%" y="-70%" width="240%" height="240%">
    <feGaussianBlur stdDeviation="13"/>
    <feComponentTransfer><feFuncA type="linear" slope="{ambient}"/></feComponentTransfer>
  </filter>
  <filter id="active" x="-100%" y="-100%" width="300%" height="300%">
    <feGaussianBlur stdDeviation="7"/>
    <feComponentTransfer><feFuncA type="linear" slope="{active}"/></feComponentTransfer>
  </filter>
  <filter id="highlight" x="-200%" y="-200%" width="500%" height="500%">
    <feGaussianBlur stdDeviation="3"/>
  </filter>
</defs>'''


def _highlight_svg(x: int, y: int, color: str) -> str:
    return f'''<circle cx="{x}" cy="{y}" r="5" fill="{color}" opacity=".8" filter="url(#highlight)"/>
<circle cx="{x}" cy="{y}" r="2" fill="{color}"/>'''


def _icon_svg_content(dark: bool) -> str:
    return f"""{_svg_defs(dark)}
<g opacity=".9" filter="url(#ambient)">
  <ellipse cx="74" cy="132" rx="31" ry="58" fill="#DD1E87"/>
  <ellipse cx="130" cy="80" rx="58" ry="28" fill="#FFA80D"/>
  <ellipse cx="184" cy="132" rx="33" ry="59" fill="#09B1D2"/>
</g>
<g filter="url(#active)" opacity=".9">
  <path d="M38 79 Q27 126 37 168" fill="none" stroke="#DD1E87" stroke-width="15" stroke-linecap="round"/>
  <rect x="91" y="40" width="72" height="17" rx="9" fill="#FFA80D" transform="rotate(-4 127 49)"/>
  <path d="M211 79 Q221 127 211 171" fill="none" stroke="#09B1D2" stroke-width="15" stroke-linecap="round"/>
  <rect x="84" y="194" width="88" height="17" rx="9" fill="url(#spectrum)" transform="rotate(4 128 203)"/>
</g>
<g transform="rotate(-4 128 124)">
  <rect x="54" y="72" width="148" height="104" rx="25" fill="url(#spectrum)"/>
  <rect x="57" y="75" width="142" height="98" rx="22" fill="#05111E"/>
</g>
<path d="M38 79 Q27 126 37 168" fill="none" stroke="#DD1E87" stroke-width="15" stroke-linecap="round"/>
<g transform="rotate(-4 127 49)">
  <rect x="91" y="40" width="72" height="17" rx="9" fill="#FFA80D"/>
  {_highlight_svg(100, 48, "#FFCF5B")}
  {_highlight_svg(153, 49, "#FFCF5B")}
</g>
<path d="M211 79 Q221 127 211 171" fill="none" stroke="#09B1D2" stroke-width="15" stroke-linecap="round"/>
{_highlight_svg(38, 87, "#FF67C2")}
{_highlight_svg(211, 162, "#64E7F2")}
<g transform="rotate(4 128 203)">
  <rect x="84" y="194" width="88" height="17" rx="9" fill="url(#spectrum)"/>
  {_highlight_svg(95, 202, "#FF67C2")}
  {_highlight_svg(128, 203, "#FFCF5B")}
  {_highlight_svg(162, 203, "#64E7F2")}
</g>"""


def _icon_svg(dark: bool) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<title>Ambilight Hue Sync icon</title>
{_icon_svg_content(dark)}
</svg>
"""


def _logo_svg(dark: bool) -> str:
    primary = "#EDF3FA" if dark else "#182235"
    secondary = "#B8C6D9" if dark else "#46566F"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 768 224">
<title>Ambilight Hue Sync logo</title>
<g transform="translate(8 8) scale(.8125)">{_icon_svg_content(dark)}</g>
<text x="230" y="104" fill="{primary}" font-family="Arial, DejaVu Sans, sans-serif" font-size="62" font-weight="700">Ambilight</text>
<text x="230" y="166" fill="{secondary}" font-family="Arial, DejaVu Sans, sans-serif" font-size="52">Hue Sync</text>
</svg>
'''


def _save_png(image: Image.Image, path: Path) -> None:
    image.save(path, format="PNG", optimize=True, compress_level=9)


def generate() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for dark, prefix in ((False, ""), (True, "dark_")):
        (SOURCE_DIR / f"{prefix}icon.svg").write_text(_icon_svg(dark), encoding="utf-8")
        (SOURCE_DIR / f"{prefix}logo.svg").write_text(_logo_svg(dark), encoding="utf-8")
        _save_png(_draw_icon(256, dark), BRAND_DIR / f"{prefix}icon.png")
        _save_png(_draw_icon(512, dark), BRAND_DIR / f"{prefix}icon@2x.png")
        _save_png(_draw_logo(768, 224, dark), BRAND_DIR / f"{prefix}logo.png")
        _save_png(_draw_logo(1536, 448, dark), BRAND_DIR / f"{prefix}logo@2x.png")


if __name__ == "__main__":
    generate()
