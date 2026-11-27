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


def _is_table_full(payload) -> bool:
    """Recognise ERR_CODE_TABLE_FULL in the different firmware spellings."""
    if isinstance(payload, dict):
        if payload.get("error_code") == 3 or payload.get("error code") == 3:
            return True
    text = str(payload).lower()
    return any(part in text for part in ("table_full", "table full", "table-full"))


async def send_text(mc, contact, args: argparse.Namespace, text: str) -> bool:
    """Send one text message, retrying when the radio's send table is full."""
    if args.transport == "mock":
        with open(args.target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "text": text}) + "\n")
        return True

    from meshcore import EventType

    wait = max(0.2, float(getattr(args, "table_full_wait", 1.0)))
    attempts = max(1, int(getattr(args, "table_full_retries", 10)))

    for attempt in range(attempts):
        try:
            if args.ack:
                result = await mc.commands.send_msg_with_retry(
                    contact, text, max_attempts=args.retries
                )
                if result is not None and not result.is_error():
                    return True
                if attempt + 1 < attempts:
                    await asyncio.sleep(wait)
                    continue
                log("  FEHLER beim Senden: keine ACK-Bestaetigung erhalten")
                return False

            result = await mc.commands.send_msg(contact, text)
            if result.type != EventType.ERROR:
                return True
            if _is_table_full(result.payload) and attempt + 1 < attempts:
                current = float(getattr(args, "_table_pause", 0.0))
                new_pause = max(wait, current * 1.5)
                new_pause = min(
                    new_pause, max(wait, float(getattr(args, "table_full_max_wait", 10.0)))
                )
                if new_pause > current:
                    args._table_pause = new_pause
                    log(f"  Sendetabelle voll - Pause auf {new_pause:g}s erhoeht")
                await asyncio.sleep(max(wait, new_pause))
                continue
            log(f"  FEHLER beim Senden: {result.payload}")
            return False
        except Exception as exc:  # noqa: BLE001 - radio APIs raise many types
            log(f"  FEHLER beim Senden: {exc}")
            return False
    return False


async def _pace(args: argparse.Namespace) -> None:
    """Pause between messages, including backpressure from a full send table."""
    delay = max(args.gap_ms / 1000, float(getattr(args, "_table_pause", 0.0)))
    if delay > 0:
        await asyncio.sleep(delay)


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
    palette_mode = getattr(args, "palette4", False) or getattr(args, "palette5", False)
    if palette_mode:
        data = palette.image_to_container(path, args.dither)
        transfer_name = path.stem + ".mc4"
        log(f"4-Farben-Modus: {palette.container_info(data)}")
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
    if getattr(args, "pipeline", 1) > 1:
        log(f"Pipeline: {args.pipeline} Pakete pro Block (Reparatur bei Rueckkanal)")
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
    unrepaired: set[int] = set()
    stopped = False
    pipeline = max(1, int(getattr(args, "pipeline", 1)))
    repair_active = (
        pipeline > 1
        and bool(getattr(args, "repair", True))
        and mc is not None
        and contact is not None
    )
    repair_wait = max(0.0, float(getattr(args, "repair_wait", 15)))
    repair_rounds = max(1, int(getattr(args, "repair_rounds", 2)))
    replies: dict[str, proto.Repair] = {}
    subscription = None

    if repair_active:
        from meshcore import EventType

        def on_reply(event) -> None:
            payload = event.payload or {}
            try:
                parsed = proto.parse_message(payload.get("text", "") or "")
            except proto.ProtocolError:
                return
            if (
                isinstance(parsed, proto.Repair)
                and parsed.transfer_id == manifest.transfer_id
            ):
                replies["value"] = parsed

        subscription = mc.subscribe(EventType.CONTACT_MSG_RECV, on_reply)

    async def send_one(seq: int) -> bool:
        ok = await send_text(mc, contact, args, messages[seq])
        await _pace(args)
        return ok

    args._table_pause = 0.0
    try:
        for _ in range(args.manifest_repeat):
            if cancelled and cancelled():
                stopped = True
                break
            if not await send_text(
                mc, contact, args, proto.encode_manifest(manifest, args.max_chars)
            ):
                log("  Manifest konnte nicht gesendet werden.")
            await _pace(args)

        seq = 0
        while not stopped and seq < len(messages):
            batch_end = min(len(messages), seq + pipeline)
            local_failed: set[int] = set()

            for current in range(seq, batch_end):
                if cancelled and cancelled():
                    stopped = True
                    break
                if await send_one(current):
                    unrepaired.discard(current)
                else:
                    local_failed.add(current)
                    unrepaired.add(current)

            # Instead of waiting for an ACK after every packet, ask the
            # receiver once per block which packets it actually missed.
            if repair_active:
                for _round in range(repair_rounds):
                    replies.pop("value", None)
                    query = proto.encode_query(manifest.transfer_id, seq, batch_end - 1)
                    if not await send_text(mc, contact, args, query):
                        log("  Rueckfrage konnte nicht gesendet werden.")
                    deadline = time.monotonic() + repair_wait
                    while "value" not in replies and time.monotonic() < deadline:
                        if cancelled and cancelled():
                            stopped = True
                            break
                        await asyncio.sleep(0.2)
                    if stopped:
                        break

                    reply = replies.pop("value", None)
                    if reply is None:
                        log("  Keine Reparaturrueckmeldung - Pipeline ohne Rueckkanal.")
                        repair_active = False
                        break

                    if reply.resend_all:
                        targets = set(range(seq, batch_end))
                    else:
                        targets = {s for s in reply.missing if seq <= s < batch_end}
                    targets |= local_failed
                    if not targets:
                        break

                    for target in sorted(targets):
                        if cancelled and cancelled():
                            stopped = True
                            break
                        if await send_one(target):
                            local_failed.discard(target)
                            unrepaired.discard(target)
                        else:
                            unrepaired.add(target)

            if stopped:
                break
            if args.report_every and (
                batch_end % args.report_every == 0 or batch_end == len(messages)
            ):
                elapsed = time.monotonic() - started
                rate = batch_end / elapsed if elapsed else 0
                eta = (len(messages) - batch_end) / rate if rate else float("inf")
                log(
                    f"  {batch_end}/{len(messages)} Pakete  "
                    f"({rate:.2f}/s, verbleibend ~{human_time(eta)}, "
                    f"fehlgeschlagen: {len(unrepaired)})"
                )
            seq = batch_end
    finally:
        if subscription is not None and mc is not None:
            mc.unsubscribe(subscription)

    failed = len(unrepaired)
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
        #: Optional callback used to answer pipeline repair queries.
        self.feedback: Callable[[str], None] | None = None
        self._asked_resend: set[str] = set()

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
            log("  Pruefsumme stimmt nicht - alle Pakete erneut anfordern.")
            if self.feedback and self.manifest.transfer_id not in self._asked_resend:
                self._asked_resend.add(self.manifest.transfer_id)
                # Drop the bad copies so the retransmitted packets replace them.
                self.chunks.clear()
                self._progress_path(self.manifest).unlink(missing_ok=True)
                self.feedback(
                    proto.encode_repair(self.manifest.transfer_id, [], resend_all=True)
                )
            return

        name = self._final_name(self.manifest)
        output = payload
        if palette.is_container(payload):
            try:
                output = palette.container_to_png(payload)
            except palette.PaletteError as exc:
                log(f"  Palettenbild konnte nicht gebaut werden: {exc}")
                return
            name = str(Path(name).with_suffix(".png"))
            log(f"  {palette.container_info(payload)} -> PNG")

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

        if isinstance(parsed, proto.Query):
            if self.feedback is None or (
                self.manifest is not None
                and parsed.transfer_id != self.manifest.transfer_id
            ):
                return
            total = self.manifest.chunks if self.manifest else parsed.end + 1
            end = min(parsed.end, total - 1)
            missing = [
                seq for seq in range(parsed.start, end + 1) if seq not in self.chunks
            ]
            self.feedback(proto.encode_repair(parsed.transfer_id, missing))
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

    reply_contact = {"value": None}
    if args.sender:
        res = await mc.commands.get_contacts()
        if res.type == EventType.ERROR:
            raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {res.payload}")
        contact = mc.get_contact_by_name(args.sender) or mc.get_contact_by_key_prefix(args.sender)
        reply_contact["value"] = contact
        receiver.sender_filter = (
            contact["public_key"][:12] if contact else args.sender.lower()
        )

    async def send_feedback(text: str) -> None:
        target = reply_contact["value"]
        if target is None:
            return
        try:
            await mc.commands.send_msg(target, text)
        except Exception as exc:  # noqa: BLE001 - radio APIs raise many types
            log(f"  Reparaturrueckmeldung fehlgeschlagen: {exc}")

    receiver.feedback = lambda text: asyncio.create_task(send_feedback(text))

    log(f"{BANNER}")
    log(f"Empfaenger aktiv ({args.transport}), Ausgabe: {out_dir}")
    log("Warte auf I1-Nachrichten ... (Strg+C zum Beenden)")

    async def on_message(event) -> None:
        payload = event.payload or {}
        prefix = payload.get("pubkey_prefix")
        if prefix and reply_contact["value"] is None:
            reply_contact["value"] = mc.get_contact_by_key_prefix(prefix)
        receiver.handle(
            payload.get("text", ""),
            prefix,
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
        "--palette4",
        action="store_true",
        help="Bild auf 4 Helligkeitsstufen quantisieren (2 Grau + Schwarz/Weiss); "
        "adaptiv als PNG oder 2-Bit-Rohdaten",
    )
    send.add_argument(
        "--palette5", action="store_true", help=argparse.SUPPRESS  # alter Alias
    )
    send.add_argument(
        "--dither",
        choices=palette.DITHER_MODES,
        default="floyd",
        help="Dither-Verfahren fuer 4-Farben-Modus (Standard: floyd)",
    )
    send.add_argument("--gap-ms", type=int, default=50, help="Pause zwischen Nachrichten")
    send.add_argument(
        "--pipeline",
        type=int,
        default=8,
        help="Pakete pro Block ohne Einzel-ACK (1 = klassisch, Standard: 8)",
    )
    send.add_argument(
        "--repair",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fehlende Pakete per Rueckfrage nachsenden (Standard: an)",
    )
    send.add_argument(
        "--repair-wait",
        type=float,
        default=15.0,
        help="Max. Wartezeit auf eine Reparaturantwort in Sekunden",
    )
    send.add_argument(
        "--repair-rounds",
        type=int,
        default=2,
        help="Reparaturrunden pro Block (Standard: 2)",
    )
    send.add_argument(
        "--table-full-wait",
        type=float,
        default=1.0,
        help="Pause bei ERR_CODE_TABLE_FULL in Sekunden (Standard: 1)",
    )
    send.add_argument(
        "--table-full-retries",
        type=int,
        default=10,
        help="Versuche bei voller Sendetabelle (Standard: 10)",
    )
    send.add_argument(
        "--table-full-max-wait",
        type=float,
        default=10.0,
        help="Maximale Pause bei voller Sendetabelle (Standard: 10 s)",
    )
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
