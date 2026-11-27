"""5-level image mode for mcimg.

The source image keeps its resolution. It is converted to five luminance
levels (black, three greys, white) and three pixels are packed into one byte::

    byte = level0 * 25 + level1 * 5 + level2

The packed bytes are wrapped in a small ``MC5F`` container so the receiver
knows the dimensions and can rebuild a palette PNG. This container is sent
with the normal mcimg file transfer, so the radio protocol does not change.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageOps

MAGIC = b"MC5F"
VERSION = 1
HEADER = struct.Struct("<4sBBII")  # magic, version, dither, width, height

LEVELS = (0, 64, 128, 192, 255)
DITHER_MODES = ("none", "floyd", "ordered")

_BAYER = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


class PaletteError(ValueError):
    """Raised for invalid MC5 containers or modes."""


def nearest_level(value: float) -> int:
    """Return the index 0..4 of the closest of the five levels."""
    if value < 32:
        return 0
    if value < 96:
        return 1
    if value < 160:
        return 2
    if value < 224:
        return 3
    return 4


def quantize(gray: bytes, width: int, height: int, dither: str = "floyd") -> list[int]:
    """Map 8-bit greyscale pixels to level indices 0..4."""
    if dither not in DITHER_MODES:
        raise PaletteError(f"unbekannter Dither-Modus: {dither}")

    if dither == "none":
        return [nearest_level(value) for value in gray]

    work = [float(value) for value in gray]
    out = [0] * len(work)
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
                shift = (_BAYER[y & 3][x & 3] - 7.5) * 4
                work[pos] = value + shift
    return out


def pack_indices(indices: list[int]) -> bytes:
    """Pack three level indices into each byte."""
    if any(not 0 <= value < 5 for value in indices):
        raise PaletteError("Level-Index muss zwischen 0 und 4 liegen")
    out = bytearray()
    for start in range(0, len(indices), 3):
        triple = indices[start : start + 3]
        while len(triple) < 3:
            triple.append(0)
        out.append(triple[0] * 25 + triple[1] * 5 + triple[2])
    return bytes(out)


def unpack_indices(data: bytes, count: int) -> list[int]:
    """Unpack ``count`` level indices from packed bytes."""
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


def image_to_container(path: str | Path, dither: str = "floyd") -> bytes:
    """Load an image (any Pillow format) and return the MC5 container."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("L")
        width, height = image.size
        indices = quantize(bytes(image.getdata()), width, height, dither)

    packed = pack_indices(indices)
    mode_index = DITHER_MODES.index(dither)
    return HEADER.pack(MAGIC, VERSION, mode_index, width, height) + packed


def parse_container(container: bytes) -> tuple[int, int, str, bytes]:
    """Return width, height, dither mode and packed pixel bytes."""
    if len(container) < HEADER.size:
        raise PaletteError("MC5-Container zu kurz")
    magic, version, mode, width, height = HEADER.unpack_from(container)
    if magic != MAGIC or version != VERSION:
        raise PaletteError("kein gueltiger MC5-Container")
    if mode >= len(DITHER_MODES) or width == 0 or height == 0:
        raise PaletteError("ungueltige MC5-Kopfzeile")
    needed = (width * height + 2) // 3
    if len(container) - HEADER.size < needed:
        raise PaletteError("MC5-Bilddaten unvollstaendig")
    return width, height, DITHER_MODES[mode], container[HEADER.size :]


def container_to_png(container: bytes) -> bytes:
    """Rebuild a 5-colour palette PNG from an MC5 container."""
    width, height, _mode, packed = parse_container(container)
    indices = unpack_indices(packed, width * height)

    image = Image.new("P", (width, height))
    image.putdata(indices)
    palette: list[int] = []
    for level in LEVELS:
        palette.extend((level, level, level))
    palette.extend([0] * (768 - len(palette)))
    image.putpalette(palette)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
