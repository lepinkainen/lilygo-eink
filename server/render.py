"""Pillow layout + 4-bit grayscale packer for the LilyGo T5 4.7" panel.

The panel is 960 wide × 540 tall, 16-level grayscale, with the e-paper-native
nibble layout: two pixels per byte, low nibble = even column. The on-device
firmware passes the bytes straight to `epd_draw_grayscale_image(epd_full_screen(), buf)`.

Pure rendering — no I/O here beyond loading bundled fonts/icons from disk.
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
from functools import lru_cache
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ha import IndoorReading
from metno import WeatherSnapshot


log = logging.getLogger(__name__)

WIDTH = 960
HEIGHT = 540
PACKED_SIZE = WIDTH * HEIGHT // 2

ROOT = pathlib.Path(__file__).resolve().parent
FONTS = ROOT / "fonts"
ICONS = ROOT / "icons"

# Resting Li-ion voltage -> approximate charge. Voltage is the only signal the
# board offers (no fuel gauge), so treat the result as an estimate.
BATTERY_CURVE = [
    (3.30, 0),
    (3.50, 5),
    (3.60, 10),
    (3.70, 30),
    (3.80, 50),
    (3.90, 65),
    (4.00, 80),
    (4.10, 90),
    (4.20, 100),
]
# At or above this the charger may be holding the rail (about 4.2 V even with
# no battery fitted), so a percentage would be meaningless.
BATTERY_EXTERNAL_V = 4.19
BATTERY_LOW_PERCENT = 15


@lru_cache(maxsize=8)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


@lru_cache(maxsize=64)
def _icon(code: str, size: int) -> Optional[Image.Image]:
    """Load a met.no PNG icon, resize, and convert to grayscale L mode.

    PNGs have alpha. We composite onto white before converting so transparent
    background reads as panel-white, not as black.
    """
    path = ICONS / f"{code}.png"
    if not path.is_file():
        log.warning("missing icon %s, falling back", code)
        return None
    raw = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", raw.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(bg, raw).convert("L")
    return flat.resize((size, size), Image.LANCZOS)


def _icon_for(symbol: str, size: int) -> Optional[Image.Image]:
    """Resolve a met.no symbol code to a loaded icon, with sensible fallbacks."""
    if not symbol:
        return _icon("cloudy", size)
    candidates = [symbol]
    # Some symbols include the polartwilight variant we don't have art for —
    # fall back to the day version, then to the bare symbol.
    if "_polartwilight" in symbol:
        candidates.append(symbol.replace("_polartwilight", "_day"))
        candidates.append(symbol.replace("_polartwilight", ""))
    for c in candidates:
        ic = _icon(c, size)
        if ic is not None:
            return ic
    return _icon("cloudy", size)


def _pretty(sym: str) -> str:
    if not sym:
        return ""
    for tag in ("_day", "_night", "_polartwilight"):
        if tag in sym:
            sym = sym.split(tag, 1)[0]
            break
    return sym.replace("_", " ")


def _format_temp(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "--"
    if digits == 0:
        return f"{value:.0f}"
    return f"{value:.1f}"


def render(
    snap: Optional[WeatherSnapshot],
    indoor: IndoorReading,
    location_name: str,
    timezone: dt.tzinfo,
    error: Optional[str] = None,
    battery_volts: Optional[float] = None,
) -> bytes:
    """Return 259200 bytes of 4-bit packed grayscale ready for the EPD."""
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)

    now_local = dt.datetime.now(timezone)

    _draw_header(draw, location_name, now_local, battery_volts)
    if snap is not None:
        _draw_current(img, draw, snap)
        _draw_stats(draw, snap)
        _draw_forecast(img, draw, snap, timezone)
    else:
        draw.text(
            (WIDTH // 2, HEIGHT // 2),
            "no weather data",
            font=_font("FiraSans-Regular.ttf", 48),
            fill=0,
            anchor="mm",
        )
    _draw_indoor(draw, indoor)
    _draw_footer(draw, snap, timezone)
    if error:
        _draw_error_banner(draw, error)

    return _pack_4bit(img)


# ---------- regions ----------

def battery_percent(volts: float) -> int:
    """Estimated charge, rounded to 5% because voltage can't resolve finer."""
    points = BATTERY_CURVE
    if volts <= points[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(points, points[1:]):
        if volts <= v1:
            percent = p0 + (p1 - p0) * (volts - v0) / (v1 - v0)
            return int(5 * round(percent / 5))
    return 100


def battery_key(volts: Optional[float]) -> Optional[str]:
    """What the panel shows for this reading; equal keys draw identically."""
    if volts is None:
        return None
    if volts >= BATTERY_EXTERNAL_V:
        return "external"
    return str(battery_percent(volts))


def _draw_battery(draw: ImageDraw.ImageDraw, volts: float, right: int) -> None:
    """Battery gauge whose right edge sits at `right`, in the header band."""
    w, h, nub = 52, 26, 5
    top = 21
    body_right = right - nub
    left = body_right - w
    draw.rectangle([(body_right, top + 7), (right, top + h - 7)], fill=0)
    draw.rectangle([(left, top), (body_right, top + h)], outline=0, width=3)
    inner_left, inner_right = left + 6, body_right - 6
    inner_top, inner_bottom = top + 6, top + h - 6

    key = battery_key(volts)
    if key == "external":
        # Filled gauge with a white bolt: charger attached or just topped up.
        draw.rectangle([(inner_left, inner_top), (inner_right, inner_bottom)], fill=0)
        cx = (left + body_right) // 2
        draw.polygon(
            [(cx + 5, top + 4), (cx - 9, top + 15), (cx - 1, top + 15),
             (cx - 5, top + h - 4), (cx + 9, top + 11), (cx + 1, top + 11)],
            fill=255,
        )
        return

    percent = int(key)
    fill_w = round((inner_right - inner_left) * percent / 100)
    if fill_w > 0:
        draw.rectangle(
            [(inner_left, inner_top), (inner_left + fill_w, inner_bottom)], fill=0
        )
    low = percent <= BATTERY_LOW_PERCENT
    label = f"LOW {percent}%" if low else f"{percent}%"
    font = _font("FiraSans-Bold.ttf" if low else "FiraSans-Regular.ttf", 30)
    draw.text((left - 8, top + h // 2), label, font=font, fill=0, anchor="rm")


def _draw_header(
    draw: ImageDraw.ImageDraw,
    location: str,
    now: dt.datetime,
    battery_volts: Optional[float] = None,
) -> None:
    font = _font("FiraSans-Bold.ttf", 40)
    draw.text((24, 8), location, font=font, fill=0)
    stamp = now.strftime("%a %d.%m  %H:%M")
    draw.text((WIDTH - 24, 8), stamp, font=font, fill=0, anchor="ra")
    if battery_volts is not None:
        _draw_battery(draw, battery_volts, WIDTH - 24 - int(font.getlength(stamp)) - 24)
    draw.line([(0, 64), (WIDTH, 64)], fill=0, width=2)


def _draw_current(img: Image.Image, draw: ImageDraw.ImageDraw, snap: WeatherSnapshot) -> None:
    big = _font("FiraSans-Bold.ttf", 200)
    unit = _font("FiraSans-Regular.ttf", 56)
    sub = _font("FiraSans-Regular.ttf", 36)

    temp_text = _format_temp(snap.temp)
    draw.text((48, 74), temp_text, font=big, fill=0)

    # "°C" placed right after the temperature digits.
    bbox = draw.textbbox((48, 74), temp_text, font=big)
    draw.text((bbox[2] + 8, 110), "°C", font=unit, fill=0)

    pretty = _pretty(snap.symbol_now)
    if pretty:
        draw.text((48, 270), pretty, font=sub, fill=80)

    icon = _icon_for(snap.symbol_now, 220)
    if icon is not None:
        img.paste(icon, (WIDTH - 220 - 32, 74), icon.convert("L"))


def _draw_indoor(draw: ImageDraw.ImageDraw, indoor: IndoorReading) -> None:
    label = _font("FiraSans-Regular.ttf", 28)
    value_font = _font("FiraSans-Bold.ttf", 48)

    box_x = WIDTH - 320
    box_y = 285
    draw.text((box_x, box_y), "Indoor", font=label, fill=80)
    if indoor.value is None:
        draw.text((box_x, box_y + 28), "--", font=value_font, fill=0)
    else:
        unit = indoor.unit or "°C"
        draw.text(
            (box_x, box_y + 28),
            f"{indoor.value:.1f} {unit}",
            font=value_font,
            fill=0,
        )


def _draw_stats(draw: ImageDraw.ImageDraw, snap: WeatherSnapshot) -> None:
    font = _font("FiraSans-Regular.ttf", 28)
    y = 350
    wind = snap.wind_speed or 0.0
    rh = snap.humidity or 0.0
    rain = snap.precip_1h

    draw.text((48, y), f"Wind {wind:.1f} m/s", font=font, fill=0)
    draw.text((280, y), f"RH {rh:.0f}%", font=font, fill=0)
    draw.text((460, y), f"Rain {rain:.1f} mm", font=font, fill=0)


def _draw_forecast(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    snap: WeatherSnapshot,
    tz: dt.tzinfo,
) -> None:
    top = 400
    bottom = 525
    draw.line([(0, top), (WIDTH, top)], fill=0, width=2)

    col_w = WIDTH // 4
    hour_font = _font("FiraSans-Bold.ttf", 32)
    temp_font = _font("FiraSans-Bold.ttf", 40)
    sym_font = _font("FiraSans-Regular.ttf", 22)

    for i, slot in enumerate(snap.next[:4]):
        x = i * col_w
        if i > 0:
            draw.line([(x, top), (x, bottom)], fill=128, width=1)

        local_ts = slot.ts.astimezone(tz)
        draw.text((x + 24, top + 10), local_ts.strftime("%H:%M"), font=hour_font, fill=0)

        icon = _icon_for(slot.symbol, 80)
        if icon is not None:
            img.paste(icon, (x + col_w - 100, top + 8), icon.convert("L"))

        temp_str = _format_temp(slot.temp, digits=0)
        draw.text((x + 24, top + 55), f"{temp_str}°", font=temp_font, fill=0)

        pretty = _pretty(slot.symbol)
        if pretty:
            draw.text((x + 24, top + 100), pretty, font=sym_font, fill=80)


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    snap: Optional[WeatherSnapshot],
    tz: dt.tzinfo,
) -> None:
    font = _font("FiraSans-Regular.ttf", 18)
    y = HEIGHT - 22
    if snap is not None:
        local = snap.updated.astimezone(tz)
        draw.text((24, y), f"met.no {local.strftime('%H:%M')}", font=font, fill=80)


def _draw_error_banner(draw: ImageDraw.ImageDraw, text: str) -> None:
    """Solid black bar across the bottom; stale data stays visible above it."""
    h = 40
    draw.rectangle([(0, HEIGHT - h), (WIDTH, HEIGHT)], fill=0)
    draw.text(
        (WIDTH // 2, HEIGHT - h // 2),
        text,
        font=_font("FiraSans-Bold.ttf", 28),
        fill=255,
        anchor="mm",
    )


# ---------- packing ----------

def _pack_4bit(img: Image.Image) -> bytes:
    """8-bit grayscale (mode L) → 4-bit packed bytes, low nibble = even col."""
    if img.size != (WIDTH, HEIGHT):
        raise ValueError(f"image size {img.size} != ({WIDTH}, {HEIGHT})")
    arr = np.asarray(img, dtype=np.uint8) >> 4
    even = arr[:, 0::2] & 0x0F
    odd = arr[:, 1::2] & 0x0F
    packed = ((odd << 4) | even).astype(np.uint8)
    assert packed.size == PACKED_SIZE
    return packed.tobytes()


def unpack_4bit(data: bytes) -> Image.Image:
    """Inverse of _pack_4bit, for debugging previews."""
    if len(data) != PACKED_SIZE:
        raise ValueError(f"expected {PACKED_SIZE} bytes, got {len(data)}")
    arr = np.frombuffer(data, dtype=np.uint8).reshape((HEIGHT, WIDTH // 2))
    even = (arr & 0x0F) << 4
    odd = (arr & 0xF0)
    out = np.empty((HEIGHT, WIDTH), dtype=np.uint8)
    out[:, 0::2] = even
    out[:, 1::2] = odd
    return Image.fromarray(out, mode="L")
