"""The application: the thin core the mixins complete.

Every instrument operation lives in a mixin under app/mixins/ - connection,
controls, dmm, framing, views, readback, autoset, recording and files. This
module keeps the state (App.__init__), the small helpers several mixins share,
and the wiring that puts the mixins in front of the base panel.
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from modernlab.app.io_worker import PRIORITY_REFRESH
from ui.panel import ModernLabUI
import os

from modernlab import analysis
from modernlab.storage import export
from owon_controller import OWONScopeController
from modernlab.settings.bench import ScopeSetup, attached_scopes
from modernlab.instrument.capture.waveform import HDS_HORIZONTAL_DIVISIONS, HDS_VERTICAL_DIVISIONS, WaveformData

from app.mixins.connection import ConnectionMixin
from app.mixins.controls import ControlsMixin
from app.mixins.dmm import DmmMixin
from app.mixins.framing import FramingMixin
from app.mixins.views import ViewsMixin
from app.mixins.readback import ReadbackMixin
from app.mixins.autoset import AutosetMixin
from app.mixins.recording import RecordingMixin
from app.mixins.files import FilesMixin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

def _attached_scopes():
    """Look up attached_scopes the way the old single-file app did.

    It used to be a module global of main, so ``patch("main.attached_scopes")`` in the
    tests rebound the exact name connect_scope and open_setup read. After the split the
    name lives here, where such a patch does not reach, so it is resolved through the
    imported namespace at call time instead - a plain ``import main`` at module scope
    would be a circular import (main imports this module).
    """
    import sys
    launcher = sys.modules.get("main")
    getter = getattr(launcher, "attached_scopes", None)
    return getter() if callable(getter) else attached_scopes()




class App(ConnectionMixin, ControlsMixin, DmmMixin, FramingMixin, ViewsMixin, ReadbackMixin, AutosetMixin, RecordingMixin, FilesMixin, ModernLabUI):
    """Modern front panel for OWON HDS200/HDS300 scopes."""

    def __init__(self, root):
        self.root = root
        # The window title is owned by the panel (see show_scope_name): it names the
        # attached instrument, and a second writer here can only make the two disagree.
        self.root.geometry("1440x900")
        self.root.minsize(1120, 760)
        self.root.configure(bg="#d5d4cf")

        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.scope = OWONScopeController(family="hds")
        self.fig = Figure(figsize=(12, 6), facecolor="#101719")
        self.ax = self.fig.add_subplot(111, facecolor="#101719")
        self.canvas = None
        self._clock_timer = None
        self._plot_cache = []
        # Values the plot annotates; refreshed from the instrument after capture.
        self._trigger_level_v = None
        self._horizontal_position = None
        self._auto_report = None
        #: Unit and type shown after the multimeter reading, e.g. "V DC".
        self._dmm_caption = ""
        self._clock_running = True
        #: The bench settings: which scope, and the calibration that describes it.
        #: Loaded before anything is drawn, so the first capture is already decoded
        #: with the right factor.
        self.setup = ScopeSetup.load()
        try:
            self.setup.apply()
        except Exception as exc:                     # a bad value must not stop start-up
            self.log("Setup could not be applied: %s" % exc, "ERROR")
        #: The software's own display scale per channel, in the connector units the
        #: panel's control uses. Empty means Auto: follow the scale the capture was
        #: decoded at. The instrument's vertical gain is not reachable over this
        #: interface - a write moves the label it reports and nothing else, measured
        #: by reading one signal at 200mV/div through 2V/div and getting identical
        #: codes, where the coarser setting would have clipped it - so the volts/div
        #: control that has to work is the one this app draws with.
        self._display_scale = {}
        #: The panel's own vertical position per channel, in volts at the input.
        self._display_offset = {}
        #: Where each trace's 0 V sits, as last drawn: the panel's position control
        #: applied, per channel.
        self._zero_marks = []
        #: The instrument's own vertical position, as last seen per channel, so a
        #: front-panel turn can be told from the readbacks that come every frame.
        self._instrument_position = {}
        #: How many readbacks are being written into live controls right now: a
        #: control applies what it holds, and a readback must not become a write.
        self._sync_depth = 0
        self._display_scale_is_auto = {}
        #: The probe convention the trace is SHOWN in, per channel. Unset means the
        #: instrument's own, and the trace is then drawn in tip volts.
        self._display_probe = {}
        #: What the cursors last measured, for the strip under the plot.
        self._cursor_reading = None
        #: Unattended recording: every capture written to a folder, untouched.
        self.recording = False
        self._records = 0
        # --- Software averaging ---
        #: Number of frames to average (1 = off). Controlled from the ANALYSE panel.
        self._avg_depth = 1
        #: Ring buffer of recent decoded waveform arrays, one list per channel name.
        self._avg_buf = {}          # {channel_name: deque of np.ndarray}
        # --- Reference trace overlay ---
        #: Saved (times_array, volts_array, label_str) or None when cleared.
        self._ref_trace = None
        # --- Zoom ---
        #: (x_lo, x_hi) in data coords, or None when at full scale.
        self._zoom_xlim = None
        #: (y_lo, y_hi) in data coords, or None when at full scale.
        self._zoom_ylim = None
        #: matplotlib RectangleSelector instance for drag-to-zoom.
        self._zoom_selector = None

        self.setup_gui()
        self.capture_state.set("NO ACQUISITION")
        self.status_var.set("Ready | Disconnected")
        self.update_time()

    def update_time(self):
        if not self._clock_running or not self.root.winfo_exists():
            return
        self.time_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if self.root.state() == "withdrawn":
            return
        self._clock_timer = self.root.after(1000, self.update_time)

    def log(self, message, level="INFO"):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] [{level}] {message}\n"
        for attr in ("console_text", "log_text"):
            widget = getattr(self, attr, None)
            if widget is not None and widget.winfo_exists():
                widget.insert(tk.END, line)
                widget.see(tk.END)

    def _set_status(self, text):
        if hasattr(self, "status_var"):
            self.status_var.set(text)
        self.log(text)

    def _is_connected(self):
        return bool(getattr(self.scope, "is_connected", False))

    def _match_choice(self, value, choices):
        token = str(value).strip().lower().replace(".0", "")
        for choice in choices:
            if token == str(choice).strip().lower().replace(".0", ""):
                return choice
        return None

    def reset_scope(self):
        if not self._is_connected():
            return
        if messagebox.askyesno("Reset scope", "Reset oscilloscope to defaults?"):
            self.tell_scope("Instrument reset", lambda scope: scope.reset())
            time.sleep(0.2)
            self.query_all_states()
            self.log("Scope reset")

    def single_trigger(self):
        if self._is_connected():
            self.scope.single_trigger()
            self.log("Single trigger armed")

    def toggle_run_stop(self):
        """Toggle the instrument between RUN and STOP.

        Verified on HDS271 V1.3.0: ``:RUNning STOP`` freezes the display and
        ``:TRIGger:STATus?`` returns ``STOP``; ``:RUNning RUN`` resumes
        triggering and the status returns ``TRIG``.

        The current state is read first so the button toggles correctly whether
        the scope is running or stopped.  Both the state query and the write run
        on the worker thread so the UI never blocks on USB.
        """
        if not self._is_connected():
            return
        def do_toggle(scope):
            state = scope.get_run_state()
            if state is None:
                # Node not available on this firmware; log and do nothing.
                return None
            if state == "RUN":
                scope.run(False)
                return "STOP"
            else:
                scope.run(True)
                return "RUN"
        self.report_later("run_stop", do_toggle, self._finish_run_stop)

    def _finish_run_stop(self, new_state, error=None):
        """Apply the result of a RUN/STOP toggle on the Tk thread."""
        if error is not None:
            self.log("RUN/STOP failed: %s" % error, "ERROR")
            return
        if new_state is None:
            self.log("RUN/STOP: :RUNning? node not available on this firmware", "WARNING")
            return
        self.log("Instrument %s" % new_state)
        self.status_var.set("Instrument %s" % new_state)

    def last_capture_amplitude(self):
        """The peak-to-peak of the last capture as the app shows it, in volts.

        This is the figure a calibration is derived against, so it is read the way
        the panel reads it: the channel it is already drawing, with the trim in
        force included.
        """
        for entry in getattr(self.scope.waveform, "channels", []) or []:
            volts = entry.get("waveform") or []
            if len(volts) > 1:
                return float(max(volts) - min(volts))
        return None

    def open_setup(self):
        """Show the bench setup: which scope, and the numbers that calibrate it."""
        from ui.dialogs import SetupDialog
        if getattr(self, "_setup_window", None) is not None:
            try:
                self._setup_window.lift()
                return
            except tk.TclError:
                self._setup_window = None
        devices = _attached_scopes()
        self._setup_window = SetupDialog(
            self.root, self.setup, devices=devices,
            last_amplitude=self.last_capture_amplitude,
            on_apply=self.save_setup)
        self._setup_window.protocol("WM_DELETE_WINDOW", self._close_setup)

    def _close_setup(self):
        window, self._setup_window = getattr(self, "_setup_window", None), None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def save_setup(self, values, apply_now):
        """Store what the setup dialog collected, and optionally put it to work."""
        self.setup = values
        if apply_now:
            try:
                changed = self.setup.apply()
                self.log("Setup applied: %s" % ", ".join(changed))
            except Exception as exc:
                messagebox.showerror("Setup", "Could not apply: %s" % exc)
                return
            # The display and spectrum choices are not the decode, but they are the
            # same kind of thing to the person who just set them: they should take
            # effect on Save & apply, without a restart.
            saved = self.setup.values
            self._palette_name = saved.get("palette") or self._palette_name
            self._fft_options["window"] = saved.get("fft_window") or self._fft_options["window"]
            self._fft_options["format"] = saved.get("fft_format") or self._fft_options["format"]
        if self.setup.save():
            self.log("Setup saved to %s (%s)" % (self.setup.path, self.setup.as_text()))
        else:
            messagebox.showerror("Setup", "Could not write %s" % self.setup.path)
            return
        # The decode may have moved under the current capture, so redraw it.
        if apply_now:
            try:
                self.plot_waveform()
            except Exception as exc:
                self.log("Redraw after setup failed: %s" % exc, "ERROR")

    def on_instrument_result(self, kind, token, value, error):
        """Apply a finished instrument call. Runs on the UI thread, never blocks."""
        deliver = getattr(self, "_deliveries", {}).pop(token, None)
        if deliver is not None:
            try:
                deliver(value, error)
            except Exception as exc:
                self.log("Could not show %s: %s" % (kind, exc), "ERROR")
            return True
        if kind == "cursors":
            if error is None:
                self.apply_cursor_readings(*value)
            return True
        if kind == "dmm":
            self.apply_dmm_reading(value, error)
            return True
        if kind == "autoset":
            self._busy = False
            if error is not None:
                self._auto_report = None
                self.log("AUTO failed: %s" % error, "ERROR")
                self.capture_state.set("AUTO FAILED")
                self.status_var.set(str(error))
            else:
                self._auto_report = value
                self.plot_waveform()
                self.report_auto_frame()
                self.capture_state.set("FRAMED")
                self.status_var.set("Framed from the instrument's own measurements")
            return True
        if kind == "record":
            if error is not None:
                self.recording = False
                self.record_label.set("stopped: %s" % error)
                self.log("Recording stopped: %s" % error, "ERROR")
            else:
                self.record_label.set("%d file(s) written" % self._records)
            return True
        return super().on_instrument_result(kind, token, value, error)

    def close_panel(self):
        self._clock_running = False
        if self._clock_timer:
            try:
                self.root.after_cancel(self._clock_timer)
            except Exception:
                pass
        super().close_panel()
