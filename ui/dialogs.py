"""The four dialogs the panel opens.

Each is a plain Toplevel that is handed what it needs and gives a value back
through a callback, so none of them reaches into the panel's state directly.
"""
import math
import queue
import threading

from modernlab.app.io_worker import PRIORITY_CAPTURE, PRIORITY_COMMAND, PRIORITY_REFRESH, ScopeIO
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import AutoMinorLocator, MaxNLocator

from modernlab import analysis
from modernlab.storage import export
from modernlab.settings.bench import (PROBE_CHOICES, ScopeSetup, attached_scopes,
                                      device_label)

from ui.widgets import INK, PANEL, SCREEN, YELLOW


class CaptureTableDialog(tk.Toplevel):
    """The capture as a table of numbers.

    A Treeview with every sample in it would take seconds to fill for a saved
    deep-memory file, so the display is capped and says so: the export is where
    the whole record goes.
    """

    #: Rows shown before the display stops filling itself.
    DISPLAY_LIMIT = 4000

    def __init__(self, parent, columns, rows, on_save=None):
        super().__init__(parent)
        self.title("Data table")
        self.configure(bg=PANEL)
        self.transient(parent)
        self.rows = list(rows)
        frame = tk.Frame(self, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        tree = ttk.Treeview(frame, columns=list(columns), show="headings", height=20)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=118, anchor="e", stretch=True)
        shown = self.rows[:self.DISPLAY_LIMIT]
        for row in shown:
            tree.insert("", "end", values=row)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        note = ("%d rows" % len(self.rows)) if len(self.rows) <= self.DISPLAY_LIMIT else (
            "showing the first %d of %d rows - save the capture for the whole record"
            % (self.DISPLAY_LIMIT, len(self.rows)))
        tk.Label(self, text=note, bg=PANEL, fg="#5c6464",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12)
        buttons = tk.Frame(self, bg=PANEL)
        buttons.pack(fill="x", padx=10, pady=(4, 10))
        if on_save is not None:
            ttk.Button(buttons, text="Save capture…", command=on_save).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")


class ScpiConsoleDialog(tk.Toplevel):
    """Send the instrument its own command language and read what comes back.

    Read-only until the operator ticks the box: a write can leave the instrument
    in a state a later capture reports as broken, which is exactly how the
    volts/div write behaved on this unit.
    """

    PLACEHOLDER = "e.g.  *IDN?   :CHANnel1:SCALe?   :MEAS:VPP?"

    def __init__(self, parent, on_send, scope=None):
        super().__init__(parent)
        self.title("SCPI console")
        self.configure(bg=PANEL)
        self.transient(parent)
        self.geometry("640x420")
        self.on_send = on_send
        self.scope = scope
        self.history = []
        self.history_at = 0

        header = tk.Frame(self, bg=PANEL)
        header.pack(fill="x", padx=10, pady=(10, 2))
        self.info = tk.StringVar(value=self.identity())
        tk.Label(header, textvariable=self.info, bg=PANEL, fg="#4a5252",
                 font=("Segoe UI", 8)).pack(anchor="w")
        self.allow_writes = tk.BooleanVar(value=False)
        tk.Checkbutton(header, text="Allow writes (a write can change the instrument)",
                       variable=self.allow_writes, bg=PANEL, fg="#8a4a1e",
                       selectcolor=PANEL, activebackground=PANEL,
                       font=("Segoe UI", 8)).pack(anchor="w")

        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True, padx=10)
        self.log = tk.Text(body, bg=SCREEN, fg="#c4d6d2", font=("Consolas", 9),
                           bd=0, padx=9, pady=6, wrap="word")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.configure(state="disabled")

        entry_row = tk.Frame(self, bg=PANEL)
        entry_row.pack(fill="x", padx=10, pady=(6, 10))
        tk.Label(entry_row, text="Command", bg=PANEL, fg=INK,
                 font=("Segoe UI", 9)).pack(side="left")
        self.entry = ttk.Entry(entry_row, width=46)
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        self.entry.bind("<Return>", lambda event: self.send())
        self.entry.bind("<Up>", lambda event: self.recall(-1))
        self.entry.bind("<Down>", lambda event: self.recall(1))
        ttk.Button(entry_row, text="Send", command=self.send).pack(side="left")
        self.entry.focus_set()
        self.write("Type a query and press Enter. Up and Down recall what you sent.")

    def identity(self):
        """Who is on the other end, if anything."""
        if self.scope is None:
            return "Not connected"
        model = getattr(self.scope, "model", "") or "?"
        serial = getattr(self.scope, "serial_number", "") or "?"
        firmware = getattr(self.scope, "firmware", "") or "?"
        return "%s  S/N %s  firmware %s" % (model, serial, firmware)

    def write(self, text, colour=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def recall(self, direction):
        if not self.history:
            return "break"
        self.history_at = max(0, min(len(self.history), self.history_at + direction))
        self.entry.delete(0, "end")
        if self.history_at < len(self.history):
            self.entry.insert(0, self.history[-1 - self.history_at])
        return "break"

    def send(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.history.append(text)
        self.history_at = 0
        self.entry.delete(0, "end")
        self.write("> " + text)
        try:
            note = self.on_send(text, self.allow_writes.get())
        except Exception as exc:                                  # never take the window down
            note = "failed: %s" % exc
        # The command is on the instrument's worker now, so this is the request's
        # own note; the reply arrives through append() when the instrument answers.
        if note:
            self.write("\u00b7 " + str(note))

    def append(self, text):
        """Write a late reply. Called from the app when the answer lands."""
        try:
            self.write("< " + str(text))
        except tk.TclError:
            # The console was closed while the instrument was answering, which is
            # allowed: the reply has nowhere to go and no one to tell.
            return


class ReadoutDialog(tk.Toplevel):
    """A read-only list of what the instrument says, with a Refresh.

    The rows come from a provider rather than being pushed in, so what is on
    screen is what was just asked for.
    """

    def __init__(self, parent, title, provider, on_refresh=None, request=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PANEL)
        self.transient(parent)
        self.provider = provider
        self.on_refresh = on_refresh
        # When a request is given, the rows are read on the instrument worker and
        # shown when they arrive, so opening this dialog does not freeze the window
        # for the twelve queries it makes.
        self.request = request
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=12, pady=12)
        self.rows = {}
        buttons = tk.Frame(self, bg=PANEL)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.refresh()

    def refresh(self):
        if self.on_refresh is not None:
            try:
                self.on_refresh()
            except Exception:
                pass
        if self.request is not None:
            self.show_rows([("reading…", "the instrument is answering")])
            self.request(self.show_rows)
            return
        try:
            rows = list(self.provider() or [])
        except Exception as exc:
            rows = [("error", str(exc))]
        self.show_rows(rows)

    def show_rows(self, rows, error=None):
        """Draw the rows. Also the delivery callback for an asynchronous read."""
        if error is not None:
            rows = [("error", str(error))]
        # The dialog may have been closed while the instrument was answering.
        try:
            self.body.winfo_children()
        except tk.TclError:
            return
        for widget in self.body.winfo_children():
            widget.destroy()
        for index, (label, value) in enumerate(rows or []):
            tk.Label(self.body, text=str(label), bg=PANEL, fg="#5c6464",
                     font=("Segoe UI", 9), anchor="w").grid(row=index, column=0,
                                                            sticky="w", padx=(0, 18), pady=1)
            tk.Label(self.body, text=str(value), bg=PANEL, fg=INK,
                     font=("Consolas", 10), anchor="w").grid(row=index, column=1,
                                                             sticky="w", pady=1)


class SetupDialog(tk.Toplevel):
    """Configure and save the bench settings: which scope, and its calibration.

    It only collects values; the app owns the file and the applying, so what is
    written and what takes effect are the same single path. Every field is
    pre-filled from what is in force, so opening it and pressing Save is a no-op
    rather than a reset.
    """

    def __init__(self, parent, setup, devices=None, last_amplitude=None, on_apply=None):
        super().__init__(parent)
        self.title("Setup")
        self.configure(bg="#202628")
        self.resizable(False, False)
        self.transient(parent)
        self.setup = setup
        self.on_apply = on_apply
        self._last_amplitude = last_amplitude
        self._devices = list(devices or [])
        self._serial_by_label = {}

        body = tk.Frame(self, bg="#202628", padx=14, pady=12)
        body.pack(fill="both", expand=True)
        row = 0

        tk.Label(body, text="BENCH SETUP", bg="#202628", fg=YELLOW,
                 font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=3,
                                                     sticky="w", pady=(0, 10))
        row += 1

        # -- which scope ----------------------------------------------------
        tk.Label(body, text="Scope", bg="#202628", fg="#f1f3ed", anchor="w",
                 font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=3)
        self.device_choice = tk.StringVar(value=self._device_default())
        self.device_box = ttk.Combobox(body, textvariable=self.device_choice,
                                       values=self._device_labels(), state="readonly",
                                       width=44)
        self.device_box.grid(row=row, column=1, sticky="we", pady=3)
        tk.Button(body, text="Rescan", command=self.rescan, bg="#444c4e", fg="#f1f3ed",
                  relief="flat", padx=6, cursor="hand2").grid(row=row, column=2, padx=(6, 0))
        row += 1
        tk.Label(body, text="Which attached scope to talk to. Save one here when more than one\n"
                            "is plugged in: otherwise the first found is used, and the log says so.",
                 bg="#202628", fg="#9fb0b3", justify="left", anchor="w",
                 font=("Segoe UI", 8)).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        # -- calibration ----------------------------------------------------
        self.trim = tk.StringVar(value=self._format(self.setup.values.get("calibration_trim")))
        self.reference = tk.StringVar(
            value=self._format(self.setup.values.get("reference_volts_per_code"), blank=""))
        self.amplitude = tk.StringVar(
            value=self._format(self.setup.values.get("known_amplitude"), blank=""))

        for label, variable, hint in (
            ("Volts gain trim", self.trim,
             "1.0 agrees with the instrument's own readings. Derive a factor here when\n"
             "the generator, not the scope, is the reference."),
            ("Known amplitude (Vpp)", self.amplitude,
             "The amplitude a calibration is derived from, e.g. 25.0. Kept so the\n"
             "field is filled in next time."),
            ("Bench reference", self.reference,
             "The uncalibrated fallback in volts per code. Blank keeps the value the\n"
             "code carries, which is where it is documented."),
        ):
            tk.Label(body, text=label, bg="#202628", fg="#f1f3ed", anchor="w",
                     font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=(8, 0))
            entry = tk.Entry(body, textvariable=variable, bg="#0f1416", fg=YELLOW,
                             insertbackground=YELLOW, relief="flat", width=18,
                             font=("Consolas", 10))
            entry.grid(row=row, column=1, sticky="w", pady=(8, 0))
            if label.startswith("Known"):
                tk.Button(body, text="Calibrate now", command=self.derive_trim,
                          bg="#444c4e", fg="#f1f3ed", relief="flat", padx=6,
                          cursor="hand2").grid(row=row, column=2, padx=(6, 0), pady=(8, 0))
            tk.Label(body, text=hint, bg="#202628", fg="#9fb0b3", justify="left",
                     anchor="w", font=("Segoe UI", 8)).grid(row=row + 1, column=1,
                                                            columnspan=2, sticky="w")
            row += 2

        tk.Label(body, text="Probe", bg="#202628", fg="#f1f3ed", anchor="w",
                 font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.probe = tk.StringVar(value=self.setup.values.get("probe") or "X1")
        ttk.Combobox(body, textvariable=self.probe, values=list(PROBE_CHOICES),
                     state="readonly", width=8).grid(row=row, column=1, sticky="w",
                                                     pady=(8, 0))
        row += 1
        tk.Label(body, text="Recorded, not applied: this instrument reads real volts at the BNC\n"
                            "whatever probe its label claims.",
                 bg="#202628", fg="#9fb0b3", justify="left", anchor="w",
                 font=("Segoe UI", 8)).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        # -- workbench: where a recording goes, and how the plot and spectrum look
        self.record_folder = tk.StringVar(value=self.setup.values.get("record_folder") or "")
        tk.Label(body, text="Record folder", bg="#202628", fg="#f1f3ed", anchor="w",
                 font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=(8, 0))
        tk.Entry(body, textvariable=self.record_folder, bg="#0f1416", fg=YELLOW,
                 insertbackground=YELLOW, relief="flat", width=30,
                 font=("Consolas", 9)).grid(row=row, column=1, sticky="w", pady=(8, 0))
        tk.Button(body, text="Browse…", command=self.choose_record_folder, bg="#444c4e",
                  fg="#f1f3ed", relief="flat", padx=6,
                  cursor="hand2").grid(row=row, column=2, padx=(6, 0), pady=(8, 0))
        row += 1
        tk.Label(body, text="RECORD on the Analyse tab writes every capture here, one CSV per\n"
                            "frame, with the settings that made its numbers true.",
                 bg="#202628", fg="#9fb0b3", justify="left", anchor="w",
                 font=("Segoe UI", 8)).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        self.live_interval = tk.StringVar(
            value=self._format(self.setup.values.get("live_interval_s"), blank="0"))
        self.palette_choice = tk.StringVar(value=self.setup.values.get("palette") or "Dark")
        self.fft_window_choice = tk.StringVar(value=self.setup.values.get("fft_window") or "hanning")
        self.fft_format_choice = tk.StringVar(value=self.setup.values.get("fft_format") or "dBV")
        for label, variable, values in (
            ("Live interval (s)", self.live_interval, None),
            ("Plot palette", self.palette_choice, list(ScopeSetup.PALETTE_NAMES)),
            ("FFT window", self.fft_window_choice, list(analysis.WINDOWS)),
            ("FFT format", self.fft_format_choice, list(analysis.FFT_FORMATS)),
        ):
            tk.Label(body, text=label, bg="#202628", fg="#f1f3ed", anchor="w",
                     font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=(6, 0))
            if values is None:
                tk.Entry(body, textvariable=variable, bg="#0f1416", fg=YELLOW,
                         insertbackground=YELLOW, relief="flat", width=18,
                         font=("Consolas", 10)).grid(row=row, column=1, sticky="w", pady=(6, 0))
            else:
                ttk.Combobox(body, textvariable=variable, values=values, state="readonly",
                             width=12).grid(row=row, column=1, sticky="w", pady=(6, 0))
            row += 1
        tk.Label(body, text="The interval is the gap between live frames; a capture itself costs\n"
                            "about half a second, so the rate actually achieved is shown live.",
                 bg="#202628", fg="#9fb0b3", justify="left", anchor="w",
                 font=("Segoe UI", 8)).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        self.status = tk.StringVar(value="")
        tk.Label(body, textvariable=self.status, bg="#202628", fg="#8fe28f", anchor="w",
                 justify="left", font=("Consolas", 8), wraplength=430).grid(
                     row=row, column=0, columnspan=3, sticky="w", pady=(10, 4))
        row += 1

        buttons = tk.Frame(body, bg="#202628")
        buttons.grid(row=row, column=0, columnspan=3, sticky="we", pady=(4, 0))
        for text, command in (("Save & apply", self.save_and_apply),
                              ("Save", self.save_only),
                              ("Close", self.destroy)):
            tk.Button(buttons, text=text, command=command, bg="#444c4e", fg="#f1f3ed",
                      relief="flat", padx=10, pady=5, cursor="hand2",
                      font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _format(value, blank="1.0"):
        if value in (None, ""):
            return blank
        return ("%.6g" % float(value)) if isinstance(value, (int, float)) else str(value)

    def _device_labels(self):
        """The combobox entries, and the serial each one stands for."""
        labels = ["Automatic (first attached)"]
        self._serial_by_label = {"Automatic (first attached)": None}
        for info in self._devices:
            label = device_label(info)
            serial = (info.get("serial") or "").strip() or None
            if serial:                      # a device with no serial cannot be chosen
                self._serial_by_label[label] = serial
            labels.append(label)
        return labels

    def _device_default(self):
        wanted = self.setup.usb_serial
        labels = self._device_labels()
        for label, serial in self._serial_by_label.items():
            if serial and serial == wanted:
                return label
        if wanted:
            return "Automatic (first attached)"
        return labels[0]

    def rescan(self):
        """Look again for attached scopes - a candidate is usually plugged in live."""
        self._devices = attached_scopes()
        self.device_box.configure(values=self._device_labels())
        self.device_choice.set(self._device_default())
        count = len(self._devices)
        self.status.set("%d scope%s attached" % (count, "" if count == 1 else "s"))

    def derive_trim(self):
        """Turn a known amplitude plus the last capture into a trim factor."""
        if self._last_amplitude is None:
            self.status.set("No capture yet: take one with Live or Capture first.")
            return
        measured = self._last_amplitude()
        # What the app shows already includes the trim in force, so the new factor
        # compounds with it rather than replacing it.
        trim = ScopeSetup.trim_from(self.amplitude.get(), measured,
                                    current=self.setup.calibration_trim or 1.0)
        if trim is None:
            self.status.set("Need a known amplitude above 0 and a capture with a "
                            "measured amplitude. The last capture reads %s Vpp."
                            % ("none" if measured is None else "%.4g" % measured,))
            return
        self.trim.set("%.6g" % trim)
        self.status.set("Last capture reads %.4g Vpp; a factor of %.6g makes it %.4g Vpp. "
                        "Save & apply to use it." % (measured, trim, float(self.amplitude.get())))

    # ------------------------------------------------------------------ actions
    def collect(self):
        """The values as this dialog shows them, validated by ScopeSetup."""
        return ScopeSetup({
            "usb_serial": self._serial_by_label.get(self.device_choice.get()),
            "calibration_trim": self.trim.get(),
            "reference_volts_per_code": self.reference.get(),
            "probe": self.probe.get(),
            "known_amplitude": self.amplitude.get(),
            "record_folder": self.record_folder.get(),
            "live_interval_s": self.live_interval.get(),
            "palette": self.palette_choice.get(),
            "fft_window": self.fft_window_choice.get(),
            "fft_format": self.fft_format_choice.get(),
        }, path=self.setup.path)

    def choose_record_folder(self):
        """Pick the folder unattended recordings are written to."""
        chosen = filedialog.askdirectory(title="Where should recordings go?")
        if chosen:
            self.record_folder.set(chosen)

    def save_and_apply(self):
        if self.on_apply:
            self.on_apply(self.collect(), True)

    def save_only(self):
        if self.on_apply:
            self.on_apply(self.collect(), False)
