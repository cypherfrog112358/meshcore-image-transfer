#!/usr/bin/env python3
"""MeshCore desktop app with image transfer.

This GUI follows the layout of the official MeshCore desktop app: a navigation
rail, contact list, chat view and a connection page. It connects directly via
BLE to a MeshCore companion such as the Heltec V4 and adds image transfer,
including the adaptive 4-colour mode, as extra features.

Start with::

    .venv\\Scripts\\python.exe mcimg_app.py
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import traceback
import types
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from PIL import Image, ImageTk

import mcimg
import mcimg_palette as palette
import mcimg_protocol as proto

PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s+Pakete")
IMAGE_SAVED_RE = re.compile(r"^EMPFANGEN: (.+) \(\d+ Bytes")
CONFIG_PATH = Path.home() / ".mcimg_app.json"


class RadioSession:
    """Owns the asyncio event loop and the BLE connection."""

    def __init__(self, emit):
        self.emit = emit  # emit(kind, value) -> thread-safe UI queue
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.mc = None
        self.receiver: mcimg.Receiver | None = None
        self.devices: list[dict] = []
        self.reply_key: str | None = None

    # ------------------------------------------------------------- loop --

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="mcimg-radio", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Radio-Eventloop konnte nicht gestartet werden")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()
        if tasks:
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.loop.close()

    def submit(self, coro, callback=None):
        if self.loop is None or self.loop.is_closed():
            raise RuntimeError("Radio-Eventloop ist nicht aktiv")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if callback is not None:

            def _done(fut):
                try:
                    value = fut.result()
                except BaseException as exc:  # noqa: BLE001 - shown in UI
                    self.emit("error", f"{type(exc).__name__}: {exc}")
                else:
                    self.emit("callback", (callback, value))

            future.add_done_callback(_done)
        return future

    def stop(self) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout=5)
        except BaseException:  # noqa: BLE001 - best effort on close
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)

    async def _shutdown(self) -> None:
        await self._stop_receiver()
        await self._disconnect(emit=False)

    # -------------------------------------------------------- connection --

    async def scan_ble_devices(self, timeout: float = 5.0) -> list[dict]:
        """Discover all nearby BLE devices, MeshCore companions first."""
        self.emit("status", "BLE-Geraete werden gesucht ...")
        try:
            from bleak import BleakScanner
        except ImportError as exc:
            self.emit("log", f"BLE-Scan nicht moeglich: {exc}")
            self.emit("status", "BLE-Scan nicht verfuegbar.")
            return []

        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except Exception as exc:  # noqa: BLE001 - reported in the log
            self.emit("log", f"BLE-Scan fehlgeschlagen: {exc}")
            self.emit("status", "BLE-Scan fehlgeschlagen.")
            return []

        def sort_key(entry: dict) -> tuple[int, int]:
            name = entry["name"].lower()
            meshcore = int(
                not (name.startswith("meshcore") or name.startswith("heltec")
                     or "meshcore" in name or "heltec" in name)
            )
            rssi = entry["rssi"]
            return meshcore, -(rssi if isinstance(rssi, int) else -200)

        seen: set[str] = set()
        entries: list[dict] = []
        for device, adv in found.values():
            address = getattr(device, "address", "") or ""
            if address and address in seen:
                continue
            seen.add(address)
            name = (
                getattr(adv, "local_name", None)
                or getattr(device, "name", None)
                or "Unbekannt"
            ).strip()
            entries.append(
                {
                    "name": name,
                    "address": address,
                    "rssi": getattr(adv, "rssi", None),
                    "device": device,
                }
            )
        entries.sort(key=sort_key)
        self.devices = entries

        self.emit("log", f"{len(entries)} BLE-Geraete gefunden:")
        for entry in entries[:20]:
            rssi = entry["rssi"]
            signal = f", {rssi} dBm" if isinstance(rssi, int) else ""
            self.emit("log", f"  {entry['name']} - {entry['address']}{signal}")
        self.emit(
            "status",
            f"{len(entries)} BLE-Geraete gefunden." if entries else "Keine BLE-Geraete gefunden.",
        )
        return [
            {"name": e["name"], "address": e["address"], "rssi": e["rssi"]}
            for e in entries
        ]

    async def connect(self, target: str, pin: str, device=None) -> bool:
        await self._disconnect(emit=False)
        from meshcore import EventType, MeshCore

        mc = await MeshCore.create_ble(
            target or None,
            device=device,
            pin=pin or None,
            debug=False,
            auto_reconnect=True,
        )
        if mc is None:
            raise RuntimeError("Verbindung zum BLE-Companion fehlgeschlagen")
        self.mc = mc
        mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
        self._serialize_commands(mc)
        try:
            contacts = await self.get_contacts()
        except BaseException:
            await self._disconnect(emit=False)
            raise
        await mc.start_auto_message_fetching()
        self.emit("connected", target or "Heltec V4")
        return contacts

    @staticmethod
    def _serialize_commands(mc) -> None:
        """Keep radio commands from overlapping (BAD_STATE on the companion)."""
        lock = asyncio.Lock()
        original = mc.commands.send

        async def locked_send(data, expected_events=None, timeout=None):
            async with lock:
                return await original(data, expected_events, timeout)

        mc.commands.send = locked_send

    async def disconnect(self) -> None:
        await self._disconnect(emit=True)

    async def _disconnect(self, emit: bool) -> None:
        if self.mc is None:
            return
        mc, self.mc = self.mc, None
        try:
            await mc.stop_auto_message_fetching()
        except BaseException:  # noqa: BLE001
            pass
        try:
            await mc.disconnect()
        except BaseException:  # noqa: BLE001
            pass
        if emit:
            self.emit("disconnected", None)

    def connected(self) -> bool:
        return self.mc is not None

    # ---------------------------------------------------------- messages --

    def _name_for(self, prefix: str) -> str:
        if not self.mc or not prefix:
            return prefix or "?"
        for contact in (self.mc.contacts or {}).values():
            if not isinstance(contact, dict):
                continue
            key = contact.get("public_key", "")
            if key[:12] == prefix:
                return contact.get("adv_name") or contact.get("custom_name") or prefix
        return prefix

    def _on_message(self, event) -> None:
        payload = event.payload or {}
        text = payload.get("text", "") or ""
        prefix = payload.get("pubkey_prefix") or ""
        if prefix:
            self.reply_key = prefix
        self.emit(
            "message",
            {"prefix": prefix, "name": self._name_for(prefix), "text": text},
        )
        if self.receiver is not None and text.startswith("I1:"):
            self.receiver.handle(text, prefix)

    def _contacts_list(self) -> list[dict]:
        items = []
        for contact in (self.mc.contacts or {}).values():
            if not isinstance(contact, dict):
                continue
            key = contact.get("public_key", "")
            items.append(
                {
                    "name": contact.get("adv_name") or contact.get("custom_name") or "?",
                    "key": key,
                    "prefix": key[:12],
                    "type": str(contact.get("type", "")),
                }
            )
        items.sort(key=lambda item: item["name"].lower())
        return items

    async def get_contacts(self) -> list[dict]:
        if self.mc is None:
            raise RuntimeError("Nicht verbunden - bitte zuerst verbinden")
        from meshcore import EventType

        last_error = None
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(0.3 * attempt)
                try:
                    await self.mc.commands.send_appstart(timeout=2)
                except BaseException:  # noqa: BLE001 - best effort state reset
                    pass
            result = await self.mc.commands.get_contacts()
            if result.type != EventType.ERROR:
                return self._contacts_list()
            last_error = result.payload
            self.emit(
                "log", f"Kontakte-Versuch {attempt + 1}/4 fehlgeschlagen: {last_error}"
            )

        hint = ""
        if isinstance(last_error, dict) and last_error.get("error_code") == 4:
            hint = (
                " - bitte die offizielle App vom BLE-Companion trennen "
                "und erneut verbinden"
            )
        raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {last_error}{hint}")

    def _contact(self, key: str):
        if self.mc is None:
            raise RuntimeError("Nicht verbunden - bitte zuerst verbinden")
        contact = self.mc.get_contact_by_key_prefix(key) or self.mc.get_contact_by_name(key)
        if contact is None:
            raise RuntimeError(f"Kontakt {key!r} nicht gefunden")
        return contact

    async def send_text(self, key: str, text: str, ack: bool, retries: int) -> bool:
        args = types.SimpleNamespace(transport="ble", ack=ack, retries=retries)
        return await mcimg.send_text(self.mc, self._contact(key), args, text)

    async def send_image(
        self,
        file: str,
        key: str,
        options: dict,
        cancelled,
    ) -> int:
        args = types.SimpleNamespace(
            file=file,
            dest=key,
            transport="ble",
            max_chars=options["max_chars"],
            palette4=options["palette4"],
            palette5=False,
            dither=options["dither"],
            gap_ms=options["gap_ms"],
            pipeline=options["pipeline"],
            repair=options["repair"],
            repair_wait=options["repair_wait"],
            repair_rounds=options["repair_rounds"],
            table_full_wait=options.get("table_full_wait", 1.0),
            table_full_retries=options.get("table_full_retries", 10),
            table_full_max_wait=options.get("table_full_max_wait", 10.0),
            ack=options["ack"],
            retries=options["retries"],
            manifest_repeat=3,
            limit=None,
            report_every=10,
        )
        _data, _name, manifest, messages = mcimg.prepare_transfer(args)
        return await mcimg.send_prepared(
            self.mc,
            self._contact(key),
            args,
            manifest,
            messages,
            cancelled,
        )

    # ------------------------------------------------------- image receive --

    async def start_receiver(
        self,
        out_dir: str,
        sender_key: str | None,
        transfer_id: str | None,
    ) -> None:
        args = types.SimpleNamespace(
            out=out_dir,
            id=transfer_id or None,
            report_every=25,
            save_every=10,
        )
        receiver = mcimg.Receiver(args)
        self.reply_key = sender_key or None
        if sender_key:
            try:
                contact = self._contact(sender_key)
                receiver.sender_filter = contact.get("public_key", "")[:12]
            except RuntimeError:
                receiver.sender_filter = sender_key.lower()

        def feedback(text: str) -> None:
            key = self.reply_key or sender_key
            if not key or self.mc is None:
                return
            asyncio.create_task(self.send_text(key, text, False, 1))

        receiver.feedback = feedback
        self.receiver = receiver
        self.emit("status", "Bildempfang aktiv")

    async def stop_receiver(self) -> None:
        await self._stop_receiver()
        self.emit("status", "Bildempfang gestoppt")

    async def _stop_receiver(self) -> None:
        if self.receiver is not None:
            self.receiver._save_progress()
            self.receiver = None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MeshCore - Bild-Transfer")
        self.root.geometry("1120x740")
        self.root.minsize(960, 620)

        self.cfg = self._load_config()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.session = RadioSession(lambda kind, value: self.events.put((kind, value)))
        self.contacts: list[dict] = []
        self.active_contact: dict | None = None
        self.image_contact: dict | None = None
        self.chats: dict[str, list[tuple[str, str]]] = {}
        self.stop_flag = threading.Event()
        self.preview_image: ImageTk.PhotoImage | None = None
        self.last_image: Path | None = None
        self.ble_entries: list[dict] = []
        self.scanning = False
        self.connected_label = ""

        mcimg.set_log_sink(lambda msg: self.events.put(("log", str(msg))))

        self._build_ui()
        self.session.start()
        self.root.after(100, self._drain_events)
        self.root.after(500, self.do_scan)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ config --

    @staticmethod
    def _load_config() -> dict:
        defaults = {
            "target": "HeltecV4",
            "pin": "",
            "out": str(Path.home() / "MeshCore-Bilder"),
            "palette4": True,
            "dither": "floyd",
            "gap_ms": 50,
            "pipeline": 8,
            "repair": True,
            "repair_wait": 60,
            "repair_rounds": 2,
            "retries": 3,
            "max_chars": proto.MAX_CHARS,
            "ack": False,
        }
        try:
            if CONFIG_PATH.is_file():
                stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if "palette5" in stored:
                    # Old settings: move to 4 colours and the fast defaults.
                    stored["palette4"] = stored["palette5"]
                    if stored.get("gap_ms") == 300:
                        stored["gap_ms"] = 50
                    stored["ack"] = False
                if stored.get("repair_wait") in (15, 30):
                    stored["repair_wait"] = 60
                stored.pop("palette5", None)
                defaults.update(stored)
        except (OSError, ValueError):
            pass
        return defaults

    def _save_config(self) -> None:
        data = {
            "target": self._selected_address(),
            "pin": self.pin_var.get().strip(),
            "out": self.out_var.get().strip(),
            "palette4": self.palette4_var.get(),
            "dither": self.dither_var.get(),
            "gap_ms": self._to_int(self.gap_var.get(), 50),
            "pipeline": self._to_int(self.pipeline_var.get(), 8),
            "repair": self.repair_var.get(),
            "repair_wait": self._to_int(self.repair_wait_var.get(), 60),
            "repair_rounds": self._to_int(self.repair_rounds_var.get(), 2),
            "retries": self._to_int(self.retries_var.get(), 3),
            "max_chars": self._to_int(self.max_chars_var.get(), proto.MAX_CHARS),
            "ack": self.ack_var.get(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _to_int(value: str, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # --------------------------------------------------------------- UI --

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        nav = tk.Frame(main, width=196, bg="#20242e")
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        title = tk.Label(
            nav,
            text="MeshCore",
            bg="#20242e",
            fg="#ffffff",
            font=("Segoe UI", 15, "bold"),
            anchor="w",
        )
        title.pack(fill="x", padx=18, pady=(18, 4))
        subtitle = tk.Label(
            nav,
            text="Bild-Transfer",
            bg="#20242e",
            fg="#9aa4b8",
            font=("Segoe UI", 10),
            anchor="w",
        )
        subtitle.pack(fill="x", padx=18, pady=(0, 18))

        self.nav_buttons: dict[str, tk.Button] = {}
        for label, page, icon in (
            ("Nachrichten", "messages", "💬"),
            ("Kontakte", "contacts", "👥"),
            ("Bilder", "images", "🖼"),
            ("Verbindung", "connection", "🔗"),
        ):
            button = tk.Button(
                nav,
                text=f"{icon}  {label}",
                bg="#20242e",
                fg="#c9d1df",
                activebackground="#31384a",
                activeforeground="#ffffff",
                relief="flat",
                anchor="w",
                padx=18,
                pady=9,
                font=("Segoe UI", 11),
                command=lambda p=page: self.show_page(p),
            )
            button.pack(fill="x")
            self.nav_buttons[page] = button

        footer = tk.Label(
            nav,
            text="BLE-Companion\nHeltec V4",
            bg="#20242e",
            fg="#6f7a8d",
            font=("Segoe UI", 9),
            justify="left",
        )
        footer.pack(side="bottom", fill="x", padx=18, pady=16)

        body = ttk.Frame(main)
        body.pack(side="left", fill="both", expand=True)

        header = ttk.Frame(body, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="MeshCore Bild-Transfer",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")
        self.conn_dot = tk.Label(header, text="●", fg="#e04b4b", font=("Segoe UI", 14))
        self.conn_dot.pack(side="left", padx=(16, 4))
        self.header_status_var = tk.StringVar(value="Getrennt")
        ttk.Label(header, textvariable=self.header_status_var).pack(side="left")
        self.connect_btn = ttk.Button(header, text="Verbinden", command=self.do_connect)
        self.connect_btn.pack(side="right", padx=(6, 0))
        self.disconnect_btn = ttk.Button(
            header, text="Trennen", command=self.do_disconnect, state="disabled"
        )
        self.disconnect_btn.pack(side="right")

        container = ttk.Frame(body)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.pages: dict[str, ttk.Frame] = {}
        builders = {
            "messages": self._build_messages_page,
            "contacts": self._build_contacts_page,
            "images": self._build_images_page,
            "connection": self._build_connection_page,
        }
        for name, builder in builders.items():
            page = ttk.Frame(container)
            page.grid(row=0, column=0, sticky="nsew")
            builder(page)
            self.pages[name] = page

        log_frame = ttk.LabelFrame(body, text="Protokoll", padding=4)
        log_frame.pack(fill="x", side="bottom", padx=10, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=6, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        status_frame = ttk.Frame(body, padding=(10, 4))
        status_frame.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(
            status_frame, mode="determinate", maximum=1, value=0, length=320
        )
        self.progress.pack(side="right", fill="x")

        self.show_page("connection")

    def show_page(self, name: str) -> None:
        for page_name, page in self.pages.items():
            if page_name == name:
                page.tkraise()
        for page_name, button in self.nav_buttons.items():
            active = page_name == name
            button.configure(
                bg="#31384a" if active else "#20242e",
                fg="#ffffff" if active else "#c9d1df",
            )

    def _scrolled_text(self, parent, height=12):
        frame = ttk.Frame(parent)
        text = tk.Text(frame, height=height, wrap="word", state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        return frame, text

    # ---------------------------------------------------- connection page --

    def _build_connection_page(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Verbindung zum Heltec V4", padding=16)
        box.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(box, text="BLE-Geraet:").grid(row=0, column=0, sticky="w", pady=6)
        self.target_var = tk.StringVar(value=self.cfg["target"])
        self.device_box = ttk.Combobox(box, textvariable=self.target_var, width=40)
        self.device_box.grid(row=0, column=1, sticky="ew")
        self.scan_btn = ttk.Button(box, text="Geraete suchen", command=self.do_scan, width=16)
        self.scan_btn.grid(row=0, column=2, sticky="w", padx=(6, 0))

        self.scan_info_var = tk.StringVar(value="Alle BLE-Geraete in der Naehe")
        ttk.Label(box, textvariable=self.scan_info_var, foreground="#5d6675").grid(
            row=1, column=2, sticky="w", padx=(6, 0)
        )

        ttk.Label(box, text="BLE-PIN (optional):").grid(row=1, column=0, sticky="w", pady=6)
        self.pin_var = tk.StringVar(value=self.cfg["pin"])
        ttk.Entry(box, textvariable=self.pin_var, show="*", width=36).grid(row=1, column=1, sticky="ew")

        ttk.Label(box, text="Ausgabeordner:").grid(row=2, column=0, sticky="w", pady=6)
        self.out_var = tk.StringVar(value=self.cfg["out"])
        ttk.Entry(box, textvariable=self.out_var, width=36).grid(row=2, column=1, sticky="ew")
        ttk.Button(box, text="...", width=4, command=self.choose_out).grid(row=2, column=2, padx=(6, 0))

        buttons = ttk.Frame(box)
        buttons.grid(row=3, column=1, sticky="w", pady=(16, 8))
        self.page_connect_btn = ttk.Button(buttons, text="Verbinden", command=self.do_connect)
        self.page_connect_btn.pack(side="left")
        self.page_disconnect_btn = ttk.Button(
            buttons, text="Trennen", command=self.do_disconnect, state="disabled"
        )
        self.page_disconnect_btn.pack(side="left", padx=(8, 0))

        ttk.Button(box, text="Kontakte laden", command=self.do_refresh_contacts).grid(
            row=4, column=1, sticky="w"
        )

        info = (
            "Es werden alle BLE-Geraete in der Naehe angezeigt - nicht nur MeshCore.\n"
            "MeshCore-/Heltec-Geraete stehen oben. Die Auswahl verbindet direkt;\n"
            "manuelle Namen oder Adressen bleiben ebenfalls moeglich.\n"
            "Der Heltec V4 muss mit der MeshCore-Companion-Firmware laufen.\n"
            "Standard-PIN bei vielen Geraeten ohne Display: 123456."
        )
        ttk.Label(box, text=info, foreground="#5d6675", justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(20, 0)
        )
        box.columnconfigure(1, weight=1)

    # ------------------------------------------------------ messages page --

    def _build_messages_page(self, parent: ttk.Frame) -> None:
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=12)

        left = ttk.Frame(paned, width=230)
        paned.add(left, weight=1)
        ttk.Label(left, text="Kontakte", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.contact_list = tk.Listbox(left, exportselection=False)
        self.contact_list.pack(fill="both", expand=True)
        self.contact_list.bind("<<ListboxSelect>>", self.on_select_contact)

        right = ttk.Frame(paned)
        paned.add(right, weight=4)
        self.chat_title_var = tk.StringVar(value="Kein Kontakt ausgewählt")
        ttk.Label(right, textvariable=self.chat_title_var, font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        chat_frame, self.chat_text = self._scrolled_text(right, height=14)
        chat_frame.pack(fill="both", expand=True)
        self.chat_text.tag_configure("sent", foreground="#2563a6")
        self.chat_text.tag_configure("received", foreground="#20242e")
        self.chat_text.tag_configure("image", foreground="#2e7d32")
        self.chat_text.tag_configure("system", foreground="#7a7a7a")

        entry_frame = ttk.Frame(right)
        entry_frame.pack(fill="x", pady=(8, 0))
        self.message_var = tk.StringVar()
        entry = ttk.Entry(entry_frame, textvariable=self.message_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self.do_send_message())
        ttk.Button(entry_frame, text="Senden", command=self.do_send_message).pack(side="right", padx=(8, 0))

    # ------------------------------------------------------ contacts page --

    def _build_contacts_page(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, padding=(12, 12, 12, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Aktualisieren", command=self.do_refresh_contacts).pack(side="left")
        ttk.Button(bar, text="Chat öffnen", command=self.do_open_chat).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Für Bilder übernehmen", command=self.do_use_for_images).pack(
            side="left", padx=(8, 0)
        )

        frame = ttk.Frame(parent, padding=(12, 4, 12, 12))
        frame.pack(fill="both", expand=True)
        columns = ("name", "prefix", "type")
        self.contacts_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, heading, width in (
            ("name", "Name", 260),
            ("prefix", "Öffentlicher Schlüssel", 180),
            ("type", "Typ", 90),
        ):
            self.contacts_tree.heading(column, text=heading)
            self.contacts_tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, command=self.contacts_tree.yview)
        self.contacts_tree.configure(yscrollcommand=scroll.set)
        self.contacts_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.contacts_tree.bind("<Double-1>", lambda _event: self.do_open_chat())

    # -------------------------------------------------------- images page --

    def _build_images_page(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # Send card
        send = ttk.LabelFrame(outer, text="Bild senden", padding=12)
        send.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(send, text="Bild / Datei:").grid(row=0, column=0, sticky="w", pady=4)
        self.file_var = tk.StringVar()
        ttk.Entry(send, textvariable=self.file_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(send, text="...", width=4, command=self.choose_file).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(send, text="Empfänger:").grid(row=1, column=0, sticky="w", pady=4)
        self.image_dest_var = tk.StringVar()
        self.image_dest_box = ttk.Combobox(send, textvariable=self.image_dest_var, width=30)
        self.image_dest_box.grid(row=1, column=1, sticky="ew")

        self.palette4_var = tk.BooleanVar(value=bool(self.cfg["palette4"]))
        ttk.Checkbutton(
            send,
            text="4-Farben-Modus (Schwarz + 2 Grau + Weiß, adaptiv PNG)",
            variable=self.palette4_var,
        ).grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(send, text="Dither:").grid(row=3, column=0, sticky="w", pady=3)
        self.dither_var = tk.StringVar(value=self.cfg["dither"])
        ttk.Combobox(
            send, textvariable=self.dither_var, values=palette.DITHER_MODES,
            width=12, state="readonly",
        ).grid(row=3, column=1, sticky="w")

        for row, label, var, default, to in (
            (4, "Pause (ms):", "gap_var", self.cfg["gap_ms"], 60000),
            (5, "Pipeline (Pakete):", "pipeline_var", self.cfg["pipeline"], 64),
            (6, "Warte auf Reparatur (s):", "repair_wait_var", self.cfg["repair_wait"], 300),
            (7, "Reparaturrunden:", "repair_rounds_var", self.cfg["repair_rounds"], 10),
            (8, "Versuche:", "retries_var", self.cfg["retries"], 10),
            (9, "Zeichen/Nachricht:", "max_chars_var", self.cfg["max_chars"], 133),
        ):
            ttk.Label(send, text=label).grid(row=row, column=0, sticky="w", pady=3)
            string_var = tk.StringVar(value=str(default))
            setattr(self, var, string_var)
            ttk.Spinbox(send, textvariable=string_var, from_=0, to=to, width=10).grid(
                row=row, column=1, sticky="w"
            )

        self.repair_var = tk.BooleanVar(value=bool(self.cfg["repair"]))
        ttk.Checkbutton(
            send, text="Fehlende Pakete nachfragen (schnell)", variable=self.repair_var
        ).grid(row=10, column=1, sticky="w", pady=2)
        self.ack_var = tk.BooleanVar(value=bool(self.cfg["ack"]))
        ttk.Checkbutton(send, text="ACK pro Paket (langsam)", variable=self.ack_var).grid(
            row=11, column=1, sticky="w", pady=2
        )

        buttons = ttk.Frame(send)
        buttons.grid(row=12, column=1, sticky="w", pady=(14, 0))
        self.send_image_btn = ttk.Button(buttons, text="Bild senden", command=self.do_send_image)
        self.send_image_btn.pack(side="left")
        self.stop_image_btn = ttk.Button(
            buttons, text="Stopp", command=self.do_stop_image, state="disabled"
        )
        self.stop_image_btn.pack(side="left", padx=(8, 0))
        send.columnconfigure(1, weight=1)

        # Receive card
        recv = ttk.LabelFrame(outer, text="Bild empfangen", padding=12)
        recv.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(recv, text="Nur von Absender (optional):").grid(row=0, column=0, sticky="w")
        self.recv_sender_var = tk.StringVar()
        self.recv_sender_box = ttk.Combobox(recv, textvariable=self.recv_sender_var, width=28)
        self.recv_sender_box.grid(row=0, column=1, sticky="ew")

        ttk.Label(recv, text="Transfer-ID (optional):").grid(row=1, column=0, sticky="w", pady=6)
        self.transfer_id_var = tk.StringVar()
        ttk.Entry(recv, textvariable=self.transfer_id_var, width=20).grid(row=1, column=1, sticky="w")

        recv_buttons = ttk.Frame(recv)
        recv_buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 6))
        self.recv_start_btn = ttk.Button(recv_buttons, text="Empfang starten", command=self.do_start_receive)
        self.recv_start_btn.pack(side="left")
        self.recv_stop_btn = ttk.Button(
            recv_buttons, text="Stopp", command=self.do_stop_receive, state="disabled"
        )
        self.recv_stop_btn.pack(side="left", padx=(8, 0))

        preview_frame = ttk.LabelFrame(recv, text="Letztes Bild", padding=8)
        preview_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        self.preview_label = ttk.Label(
            preview_frame, text="Noch kein Bild empfangen", anchor="center"
        )
        self.preview_label.pack(fill="both", expand=True)
        self.last_image_var = tk.StringVar(value="")
        ttk.Label(recv, textvariable=self.last_image_var, foreground="#5d6675").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        recv.columnconfigure(1, weight=1)
        recv.rowconfigure(3, weight=1)

    # ------------------------------------------------------------ actions --

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(title="Bilddatei wählen")
        if path:
            self.file_var.set(path)

    def choose_out(self) -> None:
        path = filedialog.askdirectory(title="Ausgabeordner wählen")
        if path:
            self.out_var.set(path)

    def submit(self, coro, callback=None) -> None:
        try:
            self.session.submit(coro, callback)
        except RuntimeError as exc:
            messagebox.showerror("MeshCore", str(exc))

    @staticmethod
    def _device_label(entry: dict) -> str:
        name = entry.get("name") or "Unbekannt"
        address = entry.get("address") or ""
        rssi = entry.get("rssi")
        signal = f", {rssi} dBm" if isinstance(rssi, int) else ""
        return f"{name} - {address}{signal}" if address else f"{name}{signal}"

    def _matched_device(self, value: str | None = None) -> dict | None:
        text = (self.target_var.get() if value is None else value).strip()
        for entry in self.session.devices:
            if (
                text == self._device_label(entry)
                or (entry.get("address") and text == entry["address"])
                or (entry.get("name") and text == entry["name"])
            ):
                return entry
        return None

    def _selected_address(self, value: str | None = None) -> str:
        text = (self.target_var.get() if value is None else value).strip()
        match = self._matched_device(text)
        return match["address"] if match and match.get("address") else text

    def do_scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.configure(state="disabled")
        self.scan_info_var.set("Suche laeuft ...")
        self.submit(self.session.scan_ble_devices(), self.on_devices)

    def on_devices(self, entries: list[dict]) -> None:
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self.ble_entries = list(entries)
        labels = [self._device_label(entry) for entry in entries]
        self.device_box.configure(values=labels)
        self.scan_info_var.set(
            f"{len(labels)} Geraete gefunden" if labels else "Keine Geraete gefunden"
        )
        current = self.target_var.get().strip()
        if labels and (not current or self._matched_device(current) is None):
            self.target_var.set(labels[0])
        self.status_var.set(f"{len(labels)} BLE-Geraete zur Auswahl.")

    def do_connect(self) -> None:
        value = self.target_var.get().strip()
        match = self._matched_device(value)
        self.connected_label = value or "Heltec V4"
        self.status_var.set("Verbinde per BLE ...")
        self.connect_btn.configure(state="disabled")
        self.page_connect_btn.configure(state="disabled")
        self.submit(
            self.session.connect(
                self._selected_address(value),
                self.pin_var.get().strip(),
                device=match["device"] if match else None,
            ),
            self.on_connected,
        )

    def on_connected(self, contacts: list[dict] | None = None) -> None:
        self.header_status_var.set(f"Verbunden: {self.connected_label or 'Heltec V4'}")
        self.conn_dot.configure(fg="#2e9e4f")
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.page_connect_btn.configure(state="disabled")
        self.page_disconnect_btn.configure(state="normal")
        if contacts is None:
            self.do_refresh_contacts()
        else:
            self.on_contacts(contacts)
            self.status_var.set(f"Verbunden. {len(contacts)} Kontakte geladen.")

    def do_disconnect(self) -> None:
        self.submit(self.session.disconnect(), self.on_disconnected)

    def on_disconnected(self, _value=None) -> None:
        self.header_status_var.set("Getrennt")
        self.conn_dot.configure(fg="#e04b4b")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.page_connect_btn.configure(state="normal")
        self.page_disconnect_btn.configure(state="disabled")
        self.status_var.set("Getrennt.")

    def do_refresh_contacts(self) -> None:
        self.submit(self.session.get_contacts(), self.on_contacts)

    def on_contacts(self, items: list[dict]) -> None:
        self.contacts = list(items)
        names = [item["name"] for item in self.contacts]

        self.contacts_tree.delete(*self.contacts_tree.get_children())
        for item in self.contacts:
            self.contacts_tree.insert(
                "", "end", values=(item["name"], item["prefix"], item["type"]), iid=item["prefix"]
            )

        self.contact_list.delete(0, "end")
        for name in names:
            self.contact_list.insert("end", name)

        self.image_dest_box.configure(values=names)
        self.recv_sender_box.configure(values=names)
        if names:
            if not self.active_contact:
                self.active_contact = self.contacts[0]
            if not self.image_contact:
                self.image_contact = self.contacts[0]
                self.image_dest_var.set(names[0])
            self.contact_list.selection_set(0)
        self.status_var.set(f"{len(names)} Kontakte geladen.")

    def on_select_contact(self, _event=None) -> None:
        selection = self.contact_list.curselection()
        if not selection:
            return
        name = self.contact_list.get(selection[0])
        contact = next((item for item in self.contacts if item["name"] == name), None)
        if contact:
            self.set_active_contact(contact)

    def do_open_chat(self) -> None:
        selected = self.contacts_tree.selection()
        if not selected:
            return
        prefix = selected[0]
        contact = next((item for item in self.contacts if item["prefix"] == prefix), None)
        if contact:
            self.set_active_contact(contact)
            self.show_page("messages")

    def do_use_for_images(self) -> None:
        selected = self.contacts_tree.selection()
        if not selected:
            return
        prefix = selected[0]
        contact = next((item for item in self.contacts if item["prefix"] == prefix), None)
        if contact:
            self.image_contact = contact
            self.image_dest_var.set(contact["name"])
            self.show_page("images")
            self.status_var.set(f"Bildempfänger: {contact['name']}")

    def set_active_contact(self, contact: dict) -> None:
        self.active_contact = contact
        self.chat_title_var.set(f"Chat mit {contact['name']}")
        self._render_chat(contact["prefix"])

    def do_send_message(self) -> None:
        text = self.message_var.get().strip()
        if not text:
            return
        if not self.active_contact:
            messagebox.showinfo("MeshCore", "Bitte zuerst einen Kontakt auswählen.")
            return
        prefix = self.active_contact["prefix"]
        self.message_var.set("")
        self._append_chat(prefix, text, outgoing=True)
        self.submit(
            self.session.send_text(
                prefix, text, self.ack_var.get(), self._to_int(self.retries_var.get(), 3)
            ),
            lambda ok: self.status_var.set("Nachricht gesendet." if ok else "Senden fehlgeschlagen."),
        )

    def _append_chat(self, prefix: str, text: str, outgoing: bool) -> None:
        self.chats.setdefault(prefix, []).append((text, "sent" if outgoing else "received"))
        if self.active_contact and self.active_contact["prefix"] == prefix:
            self._render_chat(prefix)

    def _render_chat(self, prefix: str) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        for text, tag in self.chats.get(prefix, []):
            if text.startswith("I1:"):
                kind = "Manifest" if text.startswith("I1:M:") else "Datenblock"
                label = f"[{kind} empfangen - {len(text)} Zeichen]"
                self.chat_text.insert("end", label + "\n", "image")
            else:
                who = "Ich" if tag == "sent" else (self.active_contact or {}).get("name", "?")
                self.chat_text.insert("end", f"{who}: {text}\n", tag)
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def do_send_image(self) -> None:
        file = self.file_var.get().strip()
        if not file or not Path(file).is_file():
            messagebox.showerror("MeshCore", "Bitte eine vorhandene Bilddatei wählen.")
            return
        contact = self._image_contact()
        if contact is None:
            messagebox.showerror("MeshCore", "Bitte einen Empfänger auswählen.")
            return
        if not self.session.connected():
            messagebox.showerror("MeshCore", "Bitte zuerst per BLE verbinden.")
            return

        self.stop_flag.clear()
        self.progress.configure(value=0, maximum=1)
        self.send_image_btn.configure(state="disabled")
        self.stop_image_btn.configure(state="normal")
        self.status_var.set("Bild wird vorbereitet ...")
        options = {
            "palette4": self.palette4_var.get(),
            "dither": self.dither_var.get(),
            "gap_ms": self._to_int(self.gap_var.get(), 50),
            "pipeline": self._to_int(self.pipeline_var.get(), 8),
            "repair": self.repair_var.get(),
            "repair_wait": self._to_int(self.repair_wait_var.get(), 60),
            "repair_rounds": self._to_int(self.repair_rounds_var.get(), 2),
            "retries": self._to_int(self.retries_var.get(), 3),
            "max_chars": self._to_int(self.max_chars_var.get(), proto.MAX_CHARS),
            "ack": self.ack_var.get(),
        }
        self.submit(
            self.session.send_image(file, contact["prefix"], options, self.stop_flag.is_set),
            self.on_image_sent,
        )

    def _image_contact(self) -> dict | None:
        name = self.image_dest_var.get().strip()
        if name:
            contact = next((item for item in self.contacts if item["name"] == name), None)
            if contact:
                return contact
        return self.image_contact

    def on_image_sent(self, code: int) -> None:
        self.send_image_btn.configure(state="normal")
        self.stop_image_btn.configure(state="disabled")
        if code == 130:
            self.status_var.set("Bildversand abgebrochen.")
        elif code == 0:
            self.status_var.set("Bild gesendet.")
        else:
            self.status_var.set(f"Bildversand beendet (Code {code}).")

    def do_stop_image(self) -> None:
        self.stop_flag.set()
        self.status_var.set("Stopp angefordert ...")

    def do_start_receive(self) -> None:
        out = self.out_var.get().strip() or str(Path.home() / "MeshCore-Bilder")
        Path(out).mkdir(parents=True, exist_ok=True)
        sender = self.recv_sender_var.get().strip() or None
        transfer_id = self.transfer_id_var.get().strip() or None
        self.submit(
            self.session.start_receiver(out, sender, transfer_id),
            lambda _ok: self.on_receive_started(out),
        )

    def on_receive_started(self, out: str) -> None:
        self.recv_start_btn.configure(state="disabled")
        self.recv_stop_btn.configure(state="normal")
        self.status_var.set(f"Bildempfang aktiv: {out}")

    def do_stop_receive(self) -> None:
        self.submit(self.session.stop_receiver(), self.on_receive_stopped)

    def on_receive_stopped(self, _value=None) -> None:
        self.recv_start_btn.configure(state="normal")
        self.recv_stop_btn.configure(state="disabled")

    # ------------------------------------------------------ Tk event loop --

    def _drain_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                line = str(value)
                self._append_log(line)
                match = PROGRESS_RE.search(line)
                if match:
                    current, total = int(match.group(1)), int(match.group(2))
                    self.progress.configure(maximum=max(total, 1), value=current)
                    self.status_var.set(f"Pakete: {current}/{total}")
                saved = IMAGE_SAVED_RE.match(line)
                if saved and saved.group(1).lower().endswith(".png"):
                    self.last_image = Path(saved.group(1))
                    self.last_image_var.set(str(self.last_image))
                    self._show_preview(self.last_image)
            elif kind == "message":
                self._append_chat(value["prefix"], value["text"], outgoing=False)
            elif kind == "status":
                self.status_var.set(str(value))
            elif kind == "error":
                detail = str(value)
                self._append_log(detail)
                self.status_var.set("Fehler.")
                self.connect_btn.configure(state="normal")
                self.page_connect_btn.configure(state="normal")
                self.scan_btn.configure(state="normal")
                self.scanning = False
                messagebox.showerror("MeshCore", detail.splitlines()[0])
            elif kind == "callback":
                callback, result = value
                try:
                    callback(result)
                except BaseException:  # noqa: BLE001
                    self._append_log(traceback.format_exc())

        self.root.after(100, self._drain_events)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_preview(self, path: Path) -> None:
        try:
            with Image.open(path) as source:
                source.thumbnail((320, 190))
                self.preview_image = ImageTk.PhotoImage(source.copy())
            self.preview_label.configure(image=self.preview_image, text="")
        except (OSError, ValueError):
            pass

    def _on_close(self) -> None:
        self.stop_flag.set()
        self._save_config()
        self.session.stop()
        mcimg.set_log_sink(None)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
