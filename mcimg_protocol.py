"""Wire format for sending byte-exact files over MeshCore text messages.

Every message is plain ASCII and stays within MeshCore's 133 character limit.
The original file bytes are never resized, re-encoded or compressed: they are
only base64-encoded for transport.

Message types
-------------

Manifest (sent several times by the sender, de-duplicated by the receiver)::

    I1:M:<id>:<size36>:<chunks36>:<sha256>:<ext>:<name_b64>

Data chunk::

    I1:D:<id>:<seq36>:<chunk_b64>

``id`` is the first 8 hex chars of the file's SHA-256, so it is stable across
restarts. Integers are encoded base36 without leading zeros. Base64 is standard
base64 with ``=`` padding removed (add it again when decoding).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
from dataclasses import dataclass

#: MeshCore's documented limit for text messages.
MAX_CHARS = 133

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_MANIFEST_RE = re.compile(
    r"^I1:M:([0-9a-f]{8}):([0-9a-z]+):([0-9a-z]+):"
    r"([0-9a-f]{64}):([a-z0-9]{1,8}):([A-Za-z0-9+/]*)$"
)
_DATA_RE = re.compile(r"^I1:D:([0-9a-f]{8}):([0-9a-z]+):([A-Za-z0-9+/]*)$")


class ProtocolError(ValueError):
    """Raised when a received message does not match the expected format."""


@dataclass(frozen=True)
class Manifest:
    transfer_id: str
    size: int
    chunks: int
    sha256: str
    ext: str
    name: str

    @property
    def temp_name(self) -> str:
        return f".{self.transfer_id}.{self.ext}.part"

    @property
    def progress_name(self) -> str:
        return f".{self.transfer_id}.progress.json"


@dataclass(frozen=True)
class DataChunk:
    transfer_id: str
    seq: int
    data: bytes


def b36(value: int) -> str:
    """Encode a non-negative integer in compact base36."""
    if value < 0:
        raise ValueError("base36 needs a non-negative integer")
    if value == 0:
        return "0"
    out = []
    while value:
        value, rest = divmod(value, 36)
        out.append(_B36[rest])
    return "".join(reversed(out))


def unb36(text: str) -> int:
    """Decode a base36 integer."""
    if not text or any(c not in _B36 for c in text):
        raise ProtocolError(f"ungueltige Base36-Zahl: {text!r}")
    value = 0
    for c in text:
        value = value * 36 + _B36.index(c)
    return value


def b64encode(data: bytes) -> str:
    """Standard base64 without padding (saves up to two characters)."""
    return base64.b64encode(data).decode("ascii").rstrip("=")


def b64decode(text: str) -> bytes:
    """Decode unpadded (or padded) base64."""
    if len(text) % 4 == 1:
        raise ProtocolError("ungueltige Base64-Laenge")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except binascii.Error as exc:  # pragma: no cover - defensive
        raise ProtocolError(f"ungueltiges Base64: {exc}") from exc


def safe_extension(filename: str) -> str:
    """Return a short, alphanumeric extension for the manifest."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ext = re.sub(r"[^a-z0-9]", "", ext)[:8]
    return ext or "bin"


def transfer_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def choose_chunk_size(total_bytes: int, transfer: str, max_chars: int) -> int:
    """Find the largest chunk that fits, accounting for sequence digits."""
    if not 20 < max_chars <= 512:
        raise ValueError("max_chars muss zwischen 21 und 512 liegen")
    size = max(1, (max_chars - 24) * 3 // 4)
    for _ in range(10):
        count = max(1, math.ceil(total_bytes / size))
        # Use the worst-case sequence number when measuring the header.
        header = f"I1:D:{transfer}:{b36(count - 1)}:"
        new_size = ((max_chars - len(header)) * 3) // 4
        new_size = max(1, new_size)
        if new_size == size:
            break
        size = new_size
    return size


def make_manifest(data: bytes, name: str, max_chars: int = MAX_CHARS) -> Manifest:
    digest = hashlib.sha256(data).hexdigest()
    transfer = digest[:8]
    chunk_size = choose_chunk_size(len(data), transfer, max_chars)
    chunks = max(1, math.ceil(len(data) / chunk_size))
    short_name = name.replace("\\", "/").rsplit("/", 1)[-1]
    ext = safe_extension(short_name)

    manifest = Manifest(transfer, len(data), chunks, digest, ext, short_name)
    text = encode_manifest(manifest, max_chars)
    if len(text) > max_chars:  # pragma: no cover - defensive
        raise ValueError("Manifest passt nicht in eine Nachricht")
    return manifest


def encode_manifest(manifest: Manifest, max_chars: int = MAX_CHARS) -> str:
    fixed = (
        f"I1:M:{manifest.transfer_id}:{b36(manifest.size)}:"
        f"{b36(manifest.chunks)}:{manifest.sha256}:{manifest.ext}:"
    )
    room = max_chars - len(fixed)
    if room < 0:
        raise ValueError("Manifest-Header ist zu lang")
    name_bytes = manifest.name.encode("utf-8")
    # Leave room for base64 expansion.
    name_bytes = name_bytes[: max(0, room * 3 // 4)]
    return fixed + b64encode(name_bytes)


def make_data_messages(data: bytes, manifest: Manifest, max_chars: int = MAX_CHARS) -> list[str]:
    chunk_size = choose_chunk_size(len(data), manifest.transfer_id, max_chars)
    messages = []
    for seq, start in enumerate(range(0, len(data), chunk_size)):
        body = b64encode(data[start : start + chunk_size])
        text = f"I1:D:{manifest.transfer_id}:{b36(seq)}:{body}"
        if len(text) > max_chars:  # pragma: no cover - defensive
            raise AssertionError(f"Nachricht zu lang: {len(text)}")
        messages.append(text)
    if not messages:
        # An empty file still gets one empty chunk so the transfer completes.
        text = f"I1:D:{manifest.transfer_id}:0:"
        if len(text) > max_chars:  # pragma: no cover - defensive
            raise AssertionError("Leere Nachricht zu lang")
        messages.append(text)
    return messages


def parse_message(text: str) -> Manifest | DataChunk | None:
    """Parse a MeshCore text message. Returns None for unrelated messages."""
    if not isinstance(text, str) or not text.startswith("I1:"):
        return None

    match = _MANIFEST_RE.fullmatch(text)
    if match:
        tid, size, chunks, digest, ext, name_b64 = match.groups()
        try:
            return Manifest(
                tid,
                unb36(size),
                unb36(chunks),
                digest,
                ext,
                b64decode(name_b64).decode("utf-8", "replace"),
            )
        except (ProtocolError, ValueError) as exc:
            raise ProtocolError(f"ungueltiges Manifest: {exc}") from exc

    match = _DATA_RE.fullmatch(text)
    if match:
        tid, seq, body = match.groups()
        try:
            return DataChunk(tid, unb36(seq), b64decode(body))
        except (ProtocolError, ValueError) as exc:
            raise ProtocolError(f"ungueltiger Datensatz: {exc}") from exc

    if text.startswith("I1:"):
        raise ProtocolError("I1-Nachricht mit unbekanntem Typ")
    return None  # pragma: no cover


def verify(data: bytes, manifest: Manifest) -> bool:
    return len(data) == manifest.size and hashlib.sha256(data).hexdigest() == manifest.sha256
