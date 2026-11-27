#!/usr/bin/env python3
"""Send byte-exact images (or any file) over MeshCore in small DM chunks.

See README.md. Requires Python 3.10+ and ``pip install meshcore`` for real
radios. The ``mock`` transport and ``selftest`` need no hardware and no deps.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Callable

import mcimg_palette as palette
import mcimg_protocol as proto

BANNER = "mcimg - MeshCore Bild-Transfer (bytegenau)"

_log_sink: Callable[[str], None] | None = None


def set_log_sink(sink: Callable[[str], None] | None) -> None:
    """Redirect log output, e.g. into the GUI."""
    global _log_sink
    _log_sink = sink


def log(msg: str) -> None:
    if _log_sink is not None:
        _log_sink(msg)
    else:
        print(msg, flush=True)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def human_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}Tagen"


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


async def connect(args: argparse.Namespace):
    """Create a MeshCore connection. Returns None for the mock transport."""
    if args.transport == "mock":
        return None

    try:
        from meshcore import MeshCore
    except ImportError:
        raise SystemExit(
            "meshcore ist nicht installiert. Bitte im Projektordner ausfuehren:\n"
            "  .venv\\Scripts\\python.exe -m pip install meshcore"
        )

    common = dict(
        debug=args.debug,
        auto_reconnect=args.auto_reconnect,
    )
    if args.transport == "ble":
        mc = await MeshCore.create_ble(args.target or None, pin=args.pin or None, **common)
    elif args.transport == "serial":
        mc = await MeshCore.create_serial(args.target, 115200, **common)
    elif args.transport == "tcp":
        mc = await MeshCore.create_tcp(args.target, args.tcp_port, **common)
    else:  # pragma: no cover
        raise ValueError(f"unbekannter Transport: {args.transport}")

    if mc is None:
        raise RuntimeError("Verbindung zum MeshCore-Knoten fehlgeschlagen")
    return mc


async def resolve_contact(mc, dest: str):
    from meshcore import EventType

    res = await mc.commands.get_contacts()
    if res.type == EventType.ERROR:
        raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {res.payload}")

    contact = mc.get_contact_by_name(dest) or mc.get_contact_by_key_prefix(dest)
    if contact is None:
        names = [
            c.get("adv_name", "?")
            for c in (mc.contacts or {}).values()
            if isinstance(c, dict)
        ]
        raise RuntimeError(
            f"Kontakt {dest!r} nicht gefunden. Bekannte Kontakte: {', '.join(names) or '(keine)'}"
        )
    return contact


async def send_text(mc, contact, args: argparse.Namespace, text: str) -> bool:
    """Send one text message. Returns True on success."""
    if args.transport == "mock":
        with open(args.target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "text": text}) + "\n")
        return True

    from meshcore import EventType

    try:
        if args.ack:
            result = await mc.commands.send_msg_with_retry(
                contact, text, max_attempts=args.retries
            )
            return result is not None and not result.is_error()
        result = await mc.commands.send_msg(contact, text)
        if result.type == EventType.ERROR:
            log(f"  FEHLER beim Senden: {result.payload}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - radio APIs raise many types
        log(f"  FEHLER beim Senden: {exc}")
        return False


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #


async def cmd_send(
    args: argparse.Namespace, cancelled: Callable[[], bool] | None = None
) -> int:
    _data, _transfer_name, manifest, messages = prepare_transfer(args)

    mc = await connect(args)
    contact = None
    if mc is not None:
        contact = await resolve_contact(mc, args.dest)

    try:
        return await send_prepared(mc, contact, args, manifest, messages, cancelled)
    finally:
        if mc is not None:
            await mc.disconnect()


def prepare_transfer(
    args: argparse.Namespace,
) -> tuple[bytes, str, proto.Manifest, list[str]]:
    """Read/quantize the file and build the manifest plus all messages."""
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Datei nicht gefunden: {path}")

    transfer_name = path.name
    if args.palette5:
        data = palette.image_to_container(path, args.dither)
        width, height, dither, _packed = palette.parse_container(data)
        transfer_name = path.stem + ".mc5"
        log(
            f"5-Farben-Modus: {width}x{height} Pixel, Dither {dither}, "
            f"{len(data)} Bytes gepackt (3 Pixel/Byte)"
        )
    else:
        data = path.read_bytes()

    manifest = proto.make_manifest(data, transfer_name, args.max_chars)
    messages = proto.make_data_messages(data, manifest, args.max_chars)
    if args.limit is not None:
        messages = messages[: args.limit]

    chunk_size = math.ceil(len(data) / manifest.chunks)
    log(f"{BANNER}")
    log(f"Datei:      {path}")
    log(f"Groesse:    {human_size(len(data))} ({len(data)} Bytes)")
    log(f"SHA-256:    {manifest.sha256}")
    log(f"Transfer:   {manifest.transfer_id}")
    log(f"Pakete:     {len(messages)} + {args.manifest_repeat}x Manifest")
    log(f"Nutzdaten:  ~{chunk_size} Bytes/Paket, {args.max_chars} Zeichen/Paket")
    estimate = len(messages) * (args.gap_ms / 1000)
    log(f"Mindestzeit durch Pausen: {human_time(estimate)} (Funkzeit kommt dazu)")
    if len(messages) > 5000:
        log("Hinweis: Bei LoRa kann das sehr lange dauern. Siehe README.md.")

    return data, transfer_name, manifest, messages


async def send_prepared(
    mc,
    contact,
    args: argparse.Namespace,
    manifest: proto.Manifest,
    messages: list[str],
    cancelled: Callable[[], bool] | None = None,
) -> int:
    """Send prepared messages over an existing connection (no disconnect)."""
    started = time.monotonic()
    failed = 0
    stopped = False
    for _ in range(args.manifest_repeat):
        if cancelled and cancelled():
            stopped = True
            break
        text = proto.encode_manifest(manifest, args.max_chars)
        if not await send_text(mc, contact, args, text):
            failed += 1
        if args.gap_ms:
            await asyncio.sleep(args.gap_ms / 1000)

    for seq, text in enumerate(messages):
        if cancelled and cancelled():
            stopped = True
            break
        if not await send_text(mc, contact, args, text):
            failed += 1
        if args.gap_ms:
            await asyncio.sleep(args.gap_ms / 1000)

        if (seq + 1) % args.report_every == 0 or seq + 1 == len(messages):
            elapsed = time.monotonic() - started
            rate = (seq + 1) / elapsed if elapsed else 0
            eta = (len(messages) - seq - 1) / rate if rate else float("inf")
            log(
                f"  {seq + 1}/{len(messages)} Pakete  "
                f"({rate:.2f}/s, verbleibend ~{human_time(eta)}, fehlgeschlagen: {failed})"
            )

    if stopped:
        log("Senden abgebrochen.")
        return 130
    if args.limit is None and failed == 0:
        log(f"Fertig. Empfaenger: Datei {manifest.name or 'image.bin'} (ID {manifest.transfer_id})")
    else:
        log(f"Beendet mit {failed} fehlgeschlagenen Nachrichten.")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Receiver
# --------------------------------------------------------------------------- #


class Receiver:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out_dir = Path(args.out).expanduser().resolve()
        self.chunks: dict[int, bytes] = {}
        self.manifest: proto.Manifest | None = None
        self.sender_filter: str | None = None
        self.completed_ids: set[str] = set()

    def _progress_path(self, manifest: proto.Manifest) -> Path:
        return self.out_dir / manifest.progress_name

    def _load_progress(self, manifest: proto.Manifest) -> None:
        path = self._progress_path(manifest)
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("sha256") != manifest.sha256:
                return
            self.chunks = {
                int(seq): base64.b64decode(body)
                for seq, body in state.get("chunks", {}).items()
            }
            log(f"  Fortschritt geladen: {len(self.chunks)}/{manifest.chunks} Pakete")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"  Progress-Datei ignoriert: {exc}")

    def _save_progress(self) -> None:
        if self.manifest is None:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self._progress_path(self.manifest)
        tmp = path.with_suffix(".tmp")
        state = {
            "transfer_id": self.manifest.transfer_id,
            "sha256": self.manifest.sha256,
            "size": self.manifest.size,
            "chunks": {
                str(seq): base64.b64encode(body).decode("ascii")
                for seq, body in self.chunks.items()
            },
        }
        tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)

    def _final_name(self, manifest: proto.Manifest) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", manifest.name) or "image.bin"
        if not name.lower().endswith("." + manifest.ext):
            name += "." + manifest.ext
        return name

    def _already_have(self, manifest: proto.Manifest) -> Path | None:
        """Find an existing file with the same content, if any."""
        name = self._final_name(manifest)
        stem = Path(name).stem
        candidates = [self.out_dir / name]
        candidates.extend(self.out_dir.glob(f"{stem}_*.{manifest.ext}"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for block in iter(lambda: handle.read(65536), b""):
                    digest.update(block)
            if digest.hexdigest() == manifest.sha256:
                return candidate
        return None

    def _finish(self) -> None:
        assert self.manifest is not None
        payload = b"".join(self.chunks[i] for i in range(self.manifest.chunks))
        if not proto.verify(payload, self.manifest):
            log("  Pruefsumme stimmt (noch) nicht - warte auf fehlende Pakete.")
            return

        name = self._final_name(self.manifest)
        output = payload
        if payload.startswith(palette.MAGIC):
            try:
                output = palette.container_to_png(payload)
            except palette.PaletteError as exc:
                log(f"  MC5-Bild konnte nicht gebaut werden: {exc}")
                return
            name = str(Path(name).with_suffix(".png"))
            width, height, _mode, _packed = palette.parse_container(payload)
            log(f"  5-Farben-Bild: {width}x{height} Pixel -> Palett-PNG")

        target = self.out_dir / name
        counter = 1
        extension = Path(name).suffix.lstrip(".") or self.manifest.ext
        while target.exists():
            target = self.out_dir / f"{Path(name).stem}_{counter}.{extension}"
            counter += 1

        self.out_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(output)
        self._progress_path(self.manifest).unlink(missing_ok=True)
        log(f"EMPFANGEN: {target} ({len(output)} Bytes, SHA-256 {self.manifest.sha256})")
        self.completed_ids.add(self.manifest.transfer_id)
        self.manifest = None

    def handle(self, text: str, sender: str | None = None) -> None:
        if self.sender_filter and sender and not sender.startswith(self.sender_filter):
            return
        try:
            parsed = proto.parse_message(text)
        except proto.ProtocolError as exc:
            log(f"  Ignoriere ungueltige I1-Nachricht: {exc}")
            return
        if parsed is None:
            return

        if self.args.id and parsed.transfer_id != self.args.id:
            return
        if parsed.transfer_id in self.completed_ids:
            return

        if isinstance(parsed, proto.Manifest):
            if self.manifest and self.manifest.transfer_id != parsed.transfer_id:
                log(f"Neuer Transfer {parsed.transfer_id} (alter verworfen).")
                self.chunks = {}
            if self.manifest is None:
                self.manifest = parsed
                log(
                    f"Manifest: ID {parsed.transfer_id}, {parsed.size} Bytes, "
                    f"{parsed.chunks} Pakete, Name {parsed.name!r}"
                )
                self._load_progress(parsed)
                existing = self._already_have(parsed)
                if existing is not None:
                    log(f"Bereits vollstaendig: {existing} - keine Pakete noetig.")
                    self.completed_ids.add(parsed.transfer_id)
                    self.manifest = None
            return

        if not isinstance(parsed, proto.DataChunk):
            return
        if self.manifest is None:
            log(f"  Paket {parsed.seq} vor Manifest - wird verworfen.")
            return
        if parsed.transfer_id != self.manifest.transfer_id:
            return
        if parsed.seq >= self.manifest.chunks:
            log(f"  Paket {parsed.seq} ausserhalb des Bereichs - ignoriert.")
            return
        if parsed.seq in self.chunks:
            return

        self.chunks[parsed.seq] = parsed.data
        if len(self.chunks) % self.args.save_every == 0:
            self._save_progress()
        if self.args.report_every and len(self.chunks) % self.args.report_every == 0:
            log(f"  {len(self.chunks)}/{self.manifest.chunks} Pakete empfangen")
        if len(self.chunks) == self.manifest.chunks:
            self._save_progress()
            self._finish()


async def recv_from_mock(args: argparse.Namespace, receiver: Receiver) -> int:
    path = Path(args.target).expanduser()
    seen = 0
    while True:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                seen += 1
                try:
                    item = json.loads(line)
                    receiver.handle(item.get("text", ""))
                except (ValueError, AttributeError, TypeError) as exc:
                    log(f"Ungueltige Mock-Zeile: {exc}")
        if not args.follow:
            break
        await asyncio.sleep(1)
    log(f"Mock-Datei gelesen: {seen} Nachrichten aus {path}")
    return 0


async def cmd_recv(
    args: argparse.Namespace, cancelled: Callable[[], bool] | None = None
) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    receiver = Receiver(args)

    if args.transport == "mock":
        return await recv_from_mock(args, receiver)

    mc = await connect(args)
    from meshcore import EventType

    if args.sender:
        res = await mc.commands.get_contacts()
        if res.type == EventType.ERROR:
            raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {res.payload}")
        contact = mc.get_contact_by_name(args.sender) or mc.get_contact_by_key_prefix(args.sender)
        receiver.sender_filter = (
            contact["public_key"][:12] if contact else args.sender.lower()
        )

    log(f"{BANNER}")
    log(f"Empfaenger aktiv ({args.transport}), Ausgabe: {out_dir}")
    log("Warte auf I1-Nachrichten ... (Strg+C zum Beenden)")

    async def on_message(event) -> None:
        payload = event.payload or {}
        receiver.handle(
            payload.get("text", ""),
            payload.get("pubkey_prefix"),
        )

    mc.subscribe(EventType.CONTACT_MSG_RECV, on_message)
    await mc.start_auto_message_fetching()

    interrupted = False
    try:
        while True:
            if cancelled and cancelled():
                interrupted = True
                break
            await asyncio.sleep(0.25)
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
    finally:
        receiver._save_progress()
        await mc.stop_auto_message_fetching()
        await mc.disconnect()
    return 130 if interrupted else 0


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #


async def cmd_contacts(args: argparse.Namespace) -> int:
    mc = await connect(args)
    if mc is None:
        raise SystemExit("contacts braucht ein echtes Radio (kein mock).")
    from meshcore import EventType

    res = await mc.commands.get_contacts()
    if res.type == EventType.ERROR:
        raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {res.payload}")
    contacts = mc.contacts or {}
    if not contacts:
        log("Keine Kontakte bekannt. Auf dem Empfaenger ein Advert senden.")
    for contact in sorted(contacts.values(), key=lambda c: c.get("adv_name", "")):
        if isinstance(contact, dict):
            log(f"{contact.get('adv_name', '?'):<24} {contact.get('public_key', '')[:12]}")
    await mc.disconnect()
    return 0


# --------------------------------------------------------------------------- #
# Selftest (no hardware, no dependencies)
# --------------------------------------------------------------------------- #


def cmd_selftest(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    size = args.size
    data = bytes(rng.randrange(256) for _ in range(size))
    name = "charmera_2026.jpg"
    manifest = proto.make_manifest(data, name)
    messages = proto.make_data_messages(data, manifest)

    assert all(len(m) <= proto.MAX_CHARS for m in messages)
    assert b64roundtrip(manifest, messages) == data
    print(
        f"Selftest OK: {size} Bytes, {len(messages)} Pakete, "
        f"max. {max(map(len, messages))}/{proto.MAX_CHARS} Zeichen"
    )
    return 0


def b64roundtrip(manifest: proto.Manifest, messages: list[str], loss: float = 0.0) -> bytes:
    """Simulate a lossy, reordered mesh and rebuild the file."""
    receiver = Receiver(
        argparse.Namespace(
            out=str(os.environ.get("TEMP", ".")),
            id=None,
            report_every=10**9,
            save_every=10**9,
        )
    )
    packets = [m for m in messages if random.random() >= loss]
    random.shuffle(packets)
    receiver.handle(proto.encode_manifest(manifest))
    for text in packets:
        receiver.handle(text)
    return b"".join(receiver.chunks[i] for i in range(manifest.chunks))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcimg",
        description="Bytegenaue Bild-/Datei-Uebertragung ueber MeshCore-Direktnachrichten",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def transport_options(p: argparse.ArgumentParser, need_target: bool = True) -> None:
        p.add_argument(
            "--transport",
            choices=("ble", "serial", "tcp", "mock"),
            default="ble",
            help="Verbindung (Standard: ble)",
        )
        p.add_argument(
            "--target",
            default=None,
            help=(
                "BLE-Adresse/-Name, COM-Port (serial), Host (tcp) oder "
                "JSONL-Datei (mock). Ohne Angabe sucht BLE nach Geraeten."
            ),
        )
        p.add_argument("--tcp-port", type=int, default=5000, help="TCP-Port (Standard: 5000)")
        p.add_argument("--pin", default=None, help="BLE-PIN, falls gesetzt")
        p.add_argument("--debug", action="store_true", help="meshcore-Debug-Log anzeigen")
        p.add_argument(
            "--auto-reconnect",
            action="store_true",
            help="Verbindung bei BLE-/TCP-Verlust automatisch wiederherstellen",
        )
        if need_target:
            p.add_argument(
                "--mock-file-check",
                action="store_true",
                help=argparse.SUPPRESS,
            )

    send = sub.add_parser("send", help="Datei senden")
    send.add_argument("file", help="Pfad zur Originaldatei (JPEG/PNG/...) ")
    send.add_argument("--dest", required=True, help="Kontaktname oder Public-Key-Prefix")
    transport_options(send)
    send.add_argument("--max-chars", type=int, default=proto.MAX_CHARS, help="Zeichen pro Nachricht")
    send.add_argument(
        "--palette5",
        action="store_true",
        help="Bild auf 5 Helligkeitsstufen quantisieren (3 Grau + Schwarz/Weiss)",
    )
    send.add_argument(
        "--dither",
        choices=palette.DITHER_MODES,
        default="floyd",
        help="Dither-Verfahren fuer 5-Farben-Modus (Standard: floyd)",
    )
    send.add_argument("--gap-ms", type=int, default=300, help="Pause zwischen Nachrichten")
    send.add_argument("--ack", action="store_true", help="Auf ACK warten und erneut senden")
    send.add_argument("--retries", type=int, default=3, help="Versuche pro Paket mit --ack")
    send.add_argument("--manifest-repeat", type=int, default=3, help="Manifest-Wiederholungen")
    send.add_argument("--limit", type=int, default=None, help="Nur N Datenpakete senden (Test)")
    send.add_argument("--report-every", type=int, default=25, help="Fortschritt alle N Pakete")
    send.set_defaults(func=cmd_send)

    recv = sub.add_parser("recv", help="Pakete empfangen und Datei zurueckbauen")
    transport_options(recv)
    recv.add_argument("--out", default=".", help="Ausgabeordner")
    recv.add_argument("--sender", default=None, help="Nur von diesem Kontakt/diesem Prefix annehmen")
    recv.add_argument("--id", default=None, help="Nur diese Transfer-ID annehmen")
    recv.add_argument("--follow", action="store_true", help="Mock-Datei fortlaufend lesen")
    recv.add_argument("--save-every", type=int, default=10, help="Fortschritt alle N Pakete speichern")
    recv.add_argument("--report-every", type=int, default=25, help="Fortschritt alle N Pakete")
    recv.set_defaults(func=cmd_recv)

    contacts = sub.add_parser("contacts", help="bekannte Kontakte anzeigen")
    transport_options(contacts)
    contacts.set_defaults(func=cmd_contacts)

    selftest = sub.add_parser("selftest", help="Protokoll ohne Hardware pruefen")
    selftest.add_argument("--size", type=int, default=40_000, help="Testdatengroesse in Bytes")
    selftest.add_argument("--seed", type=int, default=1, help="Zufalls-Seed")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("send", "recv") and args.transport == "mock" and not args.target:
        raise SystemExit(" Fuer mock muss --target eine JSONL-Datei sein.")
    if args.command == "send" and args.limit is not None and args.limit < 1:
        raise SystemExit("--limit muss >= 1 sein")
    if inspect.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        sys.exit(130)
