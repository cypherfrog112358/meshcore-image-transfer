#!/usr/bin/env python3
"""Tkinter desktop UI for mcimg.

Start with::

    .venv\\Scripts\\python.exe mcimg_gui.py

The radio work runs in background threads so the window stays responsive.
All UI updates are marshalled back through a queue onto the Tk main thread.
"""

from __future__ import annotations

import asyncio
import inspect
import queue
import re
import threading
import traceback
import types
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import mcimg
import mcimg_palette as palette
import mcimg_protocol as proto

PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s+Pakete")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("mcimg - MeshCore Bild-Transfer")
        self.root.geometry("960x680")
        self.root.minsize(820, 560)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.busy = False
        self.current_total = 0

        mcimg.set_log_sink(lambda msg: self.events.put(("log", msg)))

        self._build_ui()
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(4, weight=1)

        # Connection frame
        conn = ttk.LabelFrame(main, text="Verbindung", padding=8)
        conn.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        conn.columnconfigure(1, weight=1)

        ttk.Label(conn, text="Transport:").grid(row=0, column=0, sticky="w")
        self.transport_var = tk.StringVar(value="ble")
        transport = ttk.Combobox(
            conn, textvariable=self.transport_var,
            values=("ble", "serial", "tcp", "mock"), width=12, state="readonly",
        )
        transport.grid(row=0, column=1, sticky="w")

        ttk.Label(conn, text="Ziel (BLE-Name/Adresse, COM-Port, Host oder Mock-Datei):").grid(
            row=1, column=0, sticky="w"
        )
        self.target_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.target_var).grid(row=1, column=1, columnspan=2, sticky="ew")

        ttk.Label(conn, text="BLE-PIN (optional):").grid(row=2, column=0, sticky="w")
        self.pin_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.pin_var, show="*", width=20).grid(row=2, column=1, sticky="w")

        ttk.Label(conn, text="TCP-Port:").grid(row=2, column=2, sticky="w", padx=(20, 5))
        self.tcp_port_var = tk.StringVar(value="5000")
        ttk.Entry(conn, textvariable=self.tcp_port_var, width=8).grid(row=2, column=3, sticky="w")

        ttk.Label(conn, text="Kontakte:").grid(row=3, column=0, sticky="w")
        self.contacts_var = tk.StringVar()
        self.contacts_box = ttk.Combobox(conn, textvariable=self.contacts_var, width=40)
        self.contacts_box.grid(row=3, column=1, sticky="ew")
        self.contacts_btn = ttk.Button(conn, text="Kontakte laden", command=self.load_contacts)
        self.contacts_btn.grid(row=3, column=2, sticky="w", padx=(8, 0))

        # Tabs
        tabs = ttk.Notebook(main)
        tabs.grid(row=1, column=0, columnspan=3, sticky="nsew")
        send_tab = ttk.Frame(tabs, padding=10)
        recv_tab = ttk.Frame(tabs, padding=10)
        tabs.add(send_tab, text="Senden")
        tabs.add(recv_tab, text="Empfangen")
        send_tab.columnconfigure(1, weight=1)
        recv_tab.columnconfigure(1, weight=1)

        # Send tab
        ttk.Label(send_tab, text="Bild / Datei:").grid(row=0, column=0, sticky="w", pady=4)
        self.file_var = tk.StringVar()
        ttk.Entry(send_tab, textvariable=self.file_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(send_tab, text="Durchsuchen...", command=self.choose_file).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        ttk.Label(send_tab, text="Empfänger (Kontakt):").grid(row=1, column=0, sticky="w", pady=4)
        self.dest_var = tk.StringVar()
        self.dest_box = ttk.Combobox(send_tab, textvariable=self.dest_var, width=40)
        self.dest_box.grid(row=1, column=1, sticky="ew")

        ttk.Label(send_tab, text="Pause zwischen Paketen (ms):").grid(row=2, column=0, sticky="w", pady=4)
        self.gap_var = tk.StringVar(value="50")
        ttk.Spinbox(send_tab, textvariable=self.gap_var, from_=0, to=60000, width=10).grid(
            row=2, column=1, sticky="w"
        )

        self.ack_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(send_tab, text="Auf ACK warten und erneut senden", variable=self.ack_var).grid(
            row=3, column=1, sticky="w", pady=4
        )

        ttk.Label(send_tab, text="Versuche pro Paket:").grid(row=4, column=0, sticky="w", pady=4)
        self.retries_var = tk.StringVar(value="3")
        ttk.Spinbox(send_tab, textvariable=self.retries_var, from_=1, to=10, width=6).grid(
            row=4, column=1, sticky="w"
        )

        ttk.Label(send_tab, text="Zeichen pro Nachricht:").grid(row=5, column=0, sticky="w", pady=4)
        self.max_chars_var = tk.StringVar(value=str(proto.MAX_CHARS))
        ttk.Spinbox(send_tab, textvariable=self.max_chars_var, from_=20, to=133, width=8).grid(
            row=5, column=1, sticky="w"
        )

        self.palette4_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            send_tab,
            text="4-Farben-Modus (Schwarz + 2 Grau + Weiss, Originalaufloesung)",
            variable=self.palette4_var,
        ).grid(row=6, column=1, sticky="w", pady=(6, 2))

        ttk.Label(send_tab, text="Dither:").grid(row=7, column=0, sticky="w", pady=2)
        self.dither_var = tk.StringVar(value="floyd")
        ttk.Combobox(
            send_tab, textvariable=self.dither_var,
            values=palette.DITHER_MODES, width=10, state="readonly",
        ).grid(row=7, column=1, sticky="w")

        self.send_btn = ttk.Button(send_tab, text="Senden starten", command=self.start_send)
        self.send_btn.grid(row=8, column=1, sticky="w", pady=(10, 0))
        self.stop_send_btn = ttk.Button(send_tab, text="Stopp", command=self.cancel, state="disabled")
        self.stop_send_btn.grid(row=8, column=2, sticky="w", padx=(8, 0), pady=(10, 0))

        # Receive tab
        ttk.Label(recv_tab, text="Ausgabeordner:").grid(row=0, column=0, sticky="w", pady=4)
        self.out_var = tk.StringVar()
        ttk.Entry(recv_tab, textvariable=self.out_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(recv_tab, text="Durchsuchen...", command=self.choose_out).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        ttk.Label(recv_tab, text="Nur von diesem Absender (optional):").grid(row=1, column=0, sticky="w", pady=4)
        self.sender_var = tk.StringVar()
        self.sender_box = ttk.Combobox(recv_tab, textvariable=self.sender_var, width=40)
        self.sender_box.grid(row=1, column=1, sticky="ew")

        ttk.Label(recv_tab, text="Nur diese Transfer-ID (optional):").grid(row=2, column=0, sticky="w", pady=4)
        self.id_var = tk.StringVar()
        ttk.Entry(recv_tab, textvariable=self.id_var, width=20).grid(row=2, column=1, sticky="w")

        ttk.Label(
            recv_tab, text="Empfänger vor dem Sender starten. Bei Abbruch bleibt der Fortschritt erhalten."
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 4))

        self.recv_btn = ttk.Button(recv_tab, text="Empfang starten", command=self.start_recv)
        self.recv_btn.grid(row=4, column=1, sticky="w", pady=(8, 0))
        self.stop_recv_btn = ttk.Button(recv_tab, text="Stopp", command=self.cancel, state="disabled")
        self.stop_recv_btn.grid(row=4, column=2, sticky="w", padx=(8, 0), pady=(8, 0))

        # Progress and log
        self.progress = ttk.Progressbar(main, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(main, textvariable=self.status_var).grid(row=3, column=0, columnspan=3, sticky="w")

        log_frame = ttk.LabelFrame(main, text="Protokoll", padding=4)
        log_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # -------------------------------------------------------------- Actions --

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(title="Bilddatei wählen")
        if path:
            self.file_var.set(path)

    def choose_out(self) -> None:
        path = filedialog.askdirectory(title="Ausgabeordner wählen")
        if path:
            self.out_var.set(path)

    def _connection_args(self) -> types.SimpleNamespace:
        try:
            port = int(self.tcp_port_var.get() or 5000)
        except ValueError:
            port = 5000
        return types.SimpleNamespace(
            transport=self.transport_var.get(),
            target=self.target_var.get().strip() or None,
            pin=self.pin_var.get().strip() or None,
            tcp_port=port,
            debug=False,
            auto_reconnect=True,
        )

    def _send_args(self) -> types.SimpleNamespace:
        args = self._connection_args()
        defaults = {"gap_ms": 50, "retries": 3, "max_chars": proto.MAX_CHARS}
        for name, var in (
            ("gap_ms", self.gap_var),
            ("retries", self.retries_var),
            ("max_chars", self.max_chars_var),
        ):
            try:
                setattr(args, name, int(var.get()))
            except ValueError:
                setattr(args, name, defaults[name])
        args.file = self.file_var.get().strip()
        args.dest = self.dest_var.get().strip()
        args.ack = self.ack_var.get()
        args.palette4 = self.palette4_var.get()
        args.pipeline = 8
        args.repair = True
        args.repair_wait = 15.0
        args.repair_rounds = 2
        args.table_full_wait = 1.0
        args.table_full_retries = 10
        args.table_full_max_wait = 10.0
        args.dither = self.dither_var.get()
        if args.dither not in palette.DITHER_MODES:
            args.dither = "floyd"
        args.manifest_repeat = 3
        args.limit = None
        args.report_every = 10
        return args

    def _recv_args(self) -> types.SimpleNamespace:
        args = self._connection_args()
        args.out = self.out_var.get().strip() or str(Path.home())
        args.sender = self.sender_var.get().strip() or None
        args.id = self.id_var.get().strip() or None
        args.report_every = 10
        args.save_every = 5
        args.follow = False
        return args

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.send_btn, self.recv_btn, self.contacts_btn):
            button.configure(state=state)
        stop_state = "normal" if busy else "disabled"
        self.stop_send_btn.configure(state=stop_state)
        self.stop_recv_btn.configure(state=stop_state)

    def start_worker(self, func, args=()) -> None:
        if self.busy:
            return
        self.stop_flag.clear()
        self.current_total = 0
        self.progress.configure(value=0, maximum=1)
        self._set_busy(True)

        def runner() -> None:
            try:
                result = func(*args, lambda: self.stop_flag.is_set())
                code = asyncio.run(result) if inspect.isawaitable(result) else result
            except BaseException as exc:  # noqa: BLE001 - show any failure in UI
                self.events.put(("error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))
            else:
                self.events.put(("finished", code))

        self.worker_thread = threading.Thread(target=runner, daemon=True)
        self.worker_thread.start()

    def start_send(self) -> None:
        args = self._send_args()
        if not args.file:
            messagebox.showerror("mcimg", "Bitte eine Datei auswählen.")
            return
        if not args.dest:
            messagebox.showerror("mcimg", "Bitte einen Empfänger-Kontakt angeben.")
            return
        if args.transport == "mock" and not args.target:
            messagebox.showerror("mcimg", "Für mock muss ein Ziel (JSONL-Datei) angegeben werden.")
            return
        self.status_var.set("Senden läuft...")
        self.start_worker(mcimg.cmd_send, (args,))

    def start_recv(self) -> None:
        args = self._recv_args()
        if args.transport == "mock" and not args.target:
            messagebox.showerror("mcimg", "Für mock muss ein Ziel (JSONL-Datei) angegeben werden.")
            return
        self.status_var.set("Empfang läuft...")
        self.start_worker(mcimg.cmd_recv, (args,))

    def cancel(self) -> None:
        if self.busy:
            self.stop_flag.set()
            self.status_var.set("Stopp angefordert...")

    def load_contacts(self) -> None:
        if self.busy:
            return
        args = self._connection_args()
        if args.transport == "mock":
            messagebox.showinfo("mcimg", "Kontakte brauchen ein echtes Radio (kein mock).")
            return
        self.status_var.set("Kontakte werden geladen...")
        self._set_busy(True)

        def runner() -> None:
            try:
                names = asyncio.run(self._fetch_contacts(args))
            except BaseException as exc:  # noqa: BLE001
                self.events.put(("error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))
            else:
                self.events.put(("contacts", names))

        threading.Thread(target=runner, daemon=True).start()

    @staticmethod
    async def _fetch_contacts(args) -> list[str]:
        from meshcore import EventType

        mc = await mcimg.connect(args)
        try:
            res = await mc.commands.get_contacts()
            if res.type == EventType.ERROR:
                raise RuntimeError(f"Kontakte konnten nicht gelesen werden: {res.payload}")
            return sorted(
                c.get("adv_name", "?")
                for c in (mc.contacts or {}).values()
                if isinstance(c, dict) and c.get("adv_name")
            )
        finally:
            await mc.disconnect()

    # ---------------------------------------------------------- Tk events --

    def _drain_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(value))
                match = PROGRESS_RE.search(str(value))
                if match:
                    current, total = int(match.group(1)), int(match.group(2))
                    self.current_total = total
                    self.progress.configure(maximum=max(total, 1), value=current)
                    self.status_var.set(f"Pakete: {current}/{total}")
            elif kind == "contacts":
                values = list(value)
                for box in (self.contacts_box, self.dest_box, self.sender_box):
                    box.configure(values=values)
                self.dest_var.set(values[0] if values else "")
                self.sender_var.set(values[0] if values else "")
                self.status_var.set(f"{len(values)} Kontakte geladen.")
                self._set_busy(False)
            elif kind == "finished":
                self._set_busy(False)
                if value == 0:
                    self.status_var.set("Fertig.")
                elif value == 130:
                    self.status_var.set("Abgebrochen.")
                else:
                    self.status_var.set(f"Beendet (Code {value}).")
            elif kind == "error":
                self._set_busy(False)
                self._append_log(str(value))
                messagebox.showerror("mcimg", str(value).splitlines()[0])
                self.status_var.set("Fehler.")

        self.root.after(100, self._drain_events)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy:
            self.stop_flag.set()
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
