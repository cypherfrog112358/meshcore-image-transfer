"""4-colour image mode for mcimg.

The source image keeps its resolution. It is converted to four luminance
levels (black, two greys, white) and four pixels are packed into one byte::

    byte = p0 << 6 | p1 << 4 | p2 << 2 | p3

Two variants of the packed image are built:

* ``RAW`` - the packed bytes above (2 bits per pixel, 4 pixels per byte)
* ``PNG`` - the same pixels as an indexed PNG with four palette entries

The smaller variant is wrapped in an ``MC4F`` container. PNG filter + zlib
compress large flat areas far better than raw packing, while raw packing wins
for heavily dithered noise. The receiver can rebuild a PNG from either variant.

Old ``MC5F`` containers from previous transfers can still be read.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageOps

MAGIC = b"MC4F"
LEGACY_MAGIC = b"MC5F"
VERSION = 1
#: magic, version, payload mode, dither, width, height
HEADER = struct.Struct("<4sBBBII")

MODE_RAW = 0
MODE_PNG = 1
PAYLOAD_MODES = ("raw", "png")

LEVELS = (0, 85, 170, 255)
DITHER_MODES = ("none", "floyd", "ordered")

_BAYER = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PaletteError(ValueError):
    """Raised for invalid MC4 containers or modes."""


def nearest_level(value: float) -> int:
    """Return the index 0..3 of the closest of the four levels."""
    index = int(value / (LEVELS[1] - LEVELS[0]) + 0.5)
    return max(0, min(len(LEVELS) - 1, index))


def quantize(gray: bytes, width: int, height: int, dither: str = "floyd") -> list[int]:
    """Map 8-bit greyscale pixels to level indices 0..3."""
    if dither not in DITHER_MODES:
        raise PaletteError(f"unbekannter Dither-Modus: {dither}")

    if dither == "none":
        return [nearest_level(value) for value in gray]

    work = [float(value) for value in gray]
    out = [0] * len(work)
    step = LEVELS[1] - LEVELS[0]
    for y in range(height):
        row = y * width
        for x in range(width):
            pos = row + x
            value = min(255.0, max(0.0, work[pos]))
            idx = nearest_level(value)
            out[pos] = idx
            error = value - LEVELS[idx]
            if dither == "floyd":
                if x + 1 < width:
                    work[pos + 1] += error * 7 / 16
                if y + 1 < height:
                    below = pos + width
                    if x > 0:
                        work[below - 1] += error * 5 / 16
                    work[below] += error * 3 / 16
                    if x + 1 < width:
                        work[below + 1] += error / 16
            else:  # ordered Bayer dithering
                shift = (_BAYER[y & 3][x & 3] - 7.5) * step / 16
                work[pos] = value + shift
    return out


def pack_indices(indices: list[int]) -> bytes:
    """Pack four level indices into each byte."""
    if any(not 0 <= value < len(LEVELS) for value in indices):
        raise PaletteError("Level-Index muss zwischen 0 und 3 liegen")
    out = bytearray()
    for start in range(0, len(indices), 4):
        quad = indices[start : start + 4]
        while len(quad) < 4:
            quad.append(0)
        out.append(
            (quad[0] << 6) | (quad[1] << 4) | (quad[2] << 2) | quad[3]
        )
    return bytes(out)


def unpack_indices(data: bytes, count: int) -> list[int]:
    """Unpack ``count`` level indices from packed bytes."""
    out: list[int] = []
    for byte in data:
        if len(out) >= count:
            break
        out.extend(((byte >> 6) & 3, (byte >> 4) & 3, (byte >> 2) & 3, byte & 3))
    return out[:count]


def _png_for(indices: list[int], width: int, height: int) -> bytes:
    image = Image.new("P", (width, height))
    image.putdata(indices)
    palette_data: list[int] = []
    for level in LEVELS:
        palette_data.extend((level, level, level))
    palette_data.extend([0] * (768 - len(palette_data)))
    image.putpalette(palette_data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def image_to_container(path: str | Path, dither: str = "floyd") -> bytes:
    """Load an image and return the smaller raw/PNG ``MC4F`` container."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("L")
        width, height = image.size
        indices = quantize(bytes(image.getdata()), width, height, dither)

    mode_index = DITHER_MODES.index(dither)
    raw = HEADER.pack(MAGIC, VERSION, MODE_RAW, mode_index, width, height)
    raw += pack_indices(indices)
    png = HEADER.pack(MAGIC, VERSION, MODE_PNG, mode_index, width, height)
    png += _png_for(indices, width, height)
    # On a tie prefer PNG - the receiver can write it out byte for byte.
    return png if len(png) <= len(raw) else raw


def is_container(data: bytes) -> bool:
    """True for a new MC4 or a legacy MC5 container."""
    return data.startswith((MAGIC, LEGACY_MAGIC))


def parse_container(container: bytes) -> tuple[int, int, str, int, bytes]:
    """Return width, height, dither, payload mode and payload bytes."""
    if len(container) < HEADER.size:
        raise PaletteError("MC4-Container zu kurz")
    magic, version, mode, dither_index, width, height = HEADER.unpack_from(container)
    if magic != MAGIC or version != VERSION:
        raise PaletteError("kein gueltiger MC4-Container")
    if mode not in (MODE_RAW, MODE_PNG):
        raise PaletteError("unbekannter MC4-Payload-Modus")
    if dither_index >= len(DITHER_MODES) or width == 0 or height == 0:
        raise PaletteError("ungueltige MC4-Kopfzeile")
    payload = container[HEADER.size :]
    if mode == MODE_RAW:
        needed = (width * height + 3) // 4
        if len(payload) < needed:
            raise PaletteError("MC4-Bilddaten unvollstaendig")
    elif not payload.startswith(_PNG_SIGNATURE):
        raise PaletteError("MC4-Payload ist kein PNG")
    return width, height, DITHER_MODES[dither_index], mode, payload


def container_to_png(container: bytes) -> bytes:
    """Return a palette PNG for an MC4 container (or legacy MC5)."""
    if container.startswith(LEGACY_MAGIC):
        return _legacy_container_to_png(container)

    width, height, _dither, mode, payload = parse_container(container)
    if mode == MODE_PNG:
        return payload
    indices = unpack_indices(payload, width * height)
    return _png_for(indices, width, height)


def container_info(container: bytes) -> str:
    """Short human-readable description for logs."""
    if container.startswith(LEGACY_MAGIC):
        return f"5-Farben-Bild (alt), {len(container)} Bytes"
    width, height, dither, mode, _payload = parse_container(container)
    return (
        f"{width}x{height} Pixel, {len(LEVELS)} Farben, Dither {dither}, "
        f"Payload {PAYLOAD_MODES[mode]}, {len(container)} Bytes"
    )


# --------------------------------------------------------------------------- #
# Legacy MC5F containers (3 pixels per byte, five levels)
# --------------------------------------------------------------------------- #

_LEGACY_HEADER = struct.Struct("<4sBBII")
_LEGACY_LEVELS = (0, 64, 128, 192, 255)


def _legacy_unpack(data: bytes, count: int) -> list[int]:
    out: list[int] = []
    for byte in data:
        if len(out) >= count:
            break
        if byte > 124:
            raise PaletteError(f"ungueltiges gepacktes Byte: {byte}")
        first, rest = divmod(byte, 25)
        second, third = divmod(rest, 5)
        out.extend((first, second, third))
    return out[:count]


def _legacy_container_to_png(container: bytes) -> bytes:
    if len(container) < _LEGACY_HEADER.size:
        raise PaletteError("MC5-Container zu kurz")
    magic, version, mode, width, height = _LEGACY_HEADER.unpack_from(container)
    if magic != LEGACY_MAGIC or version != 1 or mode >= len(DITHER_MODES):
        raise PaletteError("kein gueltiger MC5-Container")
    indices = _legacy_unpack(container[_LEGACY_HEADER.size :], width * height)
    image = Image.new("P", (width, height))
    image.putdata(indices)
    palette_data: list[int] = []
    for level in _LEGACY_LEVELS:
        palette_data.extend((level, level, level))
    palette_data.extend([0] * (768 - len(palette_data)))
    image.putpalette(palette_data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
