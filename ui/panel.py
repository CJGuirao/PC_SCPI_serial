"""The base panel: layout, live loop, framing, cursors and views.

Instrument operations live in the subclass this is paired with (app.application.App);
everything the panel needs from an instrument it asks for through ask_scope().
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
from modernlab.settings.bench import PROBE_CHOICES, ScopeSetup, device_label

# The drawing primitives and the palette they need; the constants below are this
# panel's own, because they describe the panel's behaviour and not the widgets'.
from ui.widgets import (
    ASSETS, CYAN, HOLD_AMBER, INK, LIVE_GREEN, PANEL, PAUSE_RED, SCREEN, YELLOW,
    Rotary, transport_icon,
)
from ui.dialogs import CaptureTableDialog, ReadoutDialog, ScpiConsoleDialog, SetupDialog

#: Gap between the end of one live capture and the start of the next. A screen
#: capture costs about 0.5 s on this instrument even with the header reused -
#: the endpoint hands over 64 bytes every 32 ms - so live refresh is
#: instrument-limited at roughly two frames per second, and this gap only gives
#: the firmware a moment before the next frame is asked for.
LIVE_GAP_MS = 120

#: Frames between framing watches in the live loop. Three text reads cost ~96 ms
#: against the header's 254 ms, so this runs oftener than LIVE_HEADER_EVERY and a
#: front-panel volts/div still shows up in about a second.
LIVE_FRAMING_EVERY = 4

#: What the big header label says while nothing is attached, and the fallback when a
#: scope answers but does not name itself.
UNCONNECTED_NAME = "Unconnected Scope"
CONNECTED_NAME = "Connected Scope"

#: Live frames between calibrations against the instrument's own readings. The
#: measurement block is a few round trips, and it is what keeps the volts right
#: when the header's volts/div label has drifted from the gain.
LIVE_CALIBRATE_EVERY = 8

#: Re-read the capture header every this many live frames. The header is a
#: measured 254-352 ms of each frame and only changes when a setting does, so it
#: is reused; re-reading it this often bounds how long a change made on the
#: instrument's own front panel can go unnoticed by the plot.
LIVE_HEADER_EVERY = 12


#: What the plot can show. The same capture feeds all of them; only the drawing
#: changes, so switching view never costs a trip to the instrument.
VIEWS = ("time", "fft", "math", "xy")

VIEW_LABELS = (("time", "TIME"), ("fft", "FFT"), ("math", "MATH"), ("xy", "XY"))

#: Plot colours. Dark is what the panel uses; Print is for an exported figure,
#: where a wall of dark pixels is the wrong thing to hand somebody.
PALETTES = {
    "Dark": {"face": SCREEN, "grid": "#526060", "text": "#94a5a5",
             "trace1": "#ffe33e", "trace2": "#26d7e8", "math": "#c792ea"},
    "Light": {"face": "#fbfbf7", "grid": "#c9cdc7", "text": "#3c4646",
              "trace1": "#b58900", "trace2": "#007f9e", "math": "#7a51c2"},
    "Print": {"face": "#ffffff", "grid": "#d8dad4", "text": "#333333",
              "trace1": "#111111", "trace2": "#555555", "math": "#777777"},
}

#: The horizontal cursors are the two the analysis layer can read a difference from.
CURSOR_TARGETS = (("t1", "V1"), ("t2", "V2"), ("v1", "H1"), ("v2", "H2"))

#: How close, in pixels, a press has to be to pick up a marker rather than place one.
CURSOR_GRAB_PIXELS = 9.0

class ModernLabUI:
    def setup_gui(self):
        # Replaced as soon as the state is known; this is what the window says in the
        # moment before anything has been read from, or connected to, the instrument.
        self.root.title(UNCONNECTED_NAME)
        self.root.geometry("1440x900")
        self.root.minsize(1120, 760)
        self.root.configure(bg=PANEL)
        self.style.configure(".", font=("Segoe UI", 9), background=PANEL, foreground=INK)
        self.style.configure("TFrame", background=PANEL)
        self.style.configure("TLabel", background=PANEL, foreground=INK)
        self.style.configure("TLabelframe", background=PANEL, bordercolor="#a8aaa5", relief="groove")
        self.style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        self.style.configure("TButton", padding=(8, 5), background="#ecece7", relief="raised")
        self.style.map("TButton", background=[("pressed", "#b7bab5"), ("active", "#f7f7f1")])
        self.style.configure("TCombobox", padding=3, fieldbackground="#efefea")
        self._busy = False
        self._closing = False
        # Every call to the instrument goes through this one worker, so the Tk
        # thread never waits on USB and two threads never share the HID handle.
        self.io = ScopeIO(lambda: self.scope)
        # A late delivery per submission token, for dialogs that fill themselves in
        # when the reads they asked for come back.
        self._deliveries = {}
        self._live_timer = None
        self._live_started = None
        # Consecutive capture failures: reset on success, drives the reconnect watchdog.
        self._capture_failures = 0
        self._live_period = None
        self._live_frames = 0
        self._dmm_tick = 0
        #: Drawn transport glyphs, kept alive for Tk and drawn once per look.
        self._state_icons = {}
        #: What the indicator is showing, and which way the button's glyph points.
        self._state_face = None
        self._live_button_kind = None
        #: Last framing signature seen from the instrument, for the live watch.
        self._framing_signature = None
        self._drawer_open = False
        self._vars = {}
        #: Which view the plot is showing, and the settings each view needs.
        self._view = "time"
        self._view_buttons = {}
        saved = getattr(getattr(self, "setup", None), "values", {}) or {}
        self._palette_name = saved.get("palette") or "Dark"
        self._fft_options = {"window": saved.get("fft_window") or "hanning",
                             "format": saved.get("fft_format") or "dBV",
                             "log": False, "peaks": 5}
        self._math_options = {"op": "subtract", "second": "CH2"}
        self._xy_options = {"x": "CH1", "y": "CH2"}
        #: Cursor positions in data units: two times and two voltages. None means
        #: the marker is not placed, which is different from being at zero.
        self._cursors = {"t1": None, "t2": None, "v1": None, "v2": None}
        self._cursor_target = "t1"
        self._cursor_buttons = {}
        self._cursor_artists = {}
        self._drag_cursor = None
        #: Set while a capture came from a file rather than the instrument.
        self._loaded_capture = None
        # Live controls apply shortly after the value settles, so a dragged knob
        # does not flood the instrument with one write per detent.
        self._live_apply = {}
        self._button_images = {}
        self.root.protocol("WM_DELETE_WINDOW", self.close_panel)

        header = tk.Frame(self.root, bg=PANEL)
        header.pack(fill="x", padx=20, pady=(12, 8))
        # The big label names what is on the other end of the cable, not the project.
        # An operator's first question is whether a scope is attached and which one, and
        # the answer has to come from the instrument: this app talks to an HDS271 and an
        # HDS272, so the model is read from the unit's own *IDN? rather than assumed
        # from the dialect it was opened with.
        self.brand_label = tk.Label(header, text=UNCONNECTED_NAME, bg=PANEL, fg=INK,
                                    font=("Segoe UI", 19, "bold"))
        self.brand_label.pack(side="left")
        self._brand_shown = UNCONNECTED_NAME
        tk.Label(header, text="  /  DIGITAL OSCILLOSCOPE", bg=PANEL,
                 fg="#646c6c", font=("Segoe UI", 9)).pack(side="left", padx=8)
        # The transport controls sit together on the right - what to talk over, where to
        # find it, and the one button that connects or drops the link. They get their own
        # frame so the order in this source is the order on screen: packing straight onto
        # the right of the header puts the FIRST widget packed at the far right, which
        # reads backwards and reorders itself the moment someone edits it.
        transport = tk.Frame(header, bg=PANEL)
        transport.pack(side="right")
        self.conn_type = tk.StringVar(value="usb")
        self.device_info = tk.StringVar(value="Not connected")
        type_box = ttk.Combobox(transport, textvariable=self.conn_type, values=("lan", "usb"),
                                state="readonly", width=5)
        type_box.pack(side="left", padx=(0, 5))
        # The HDS200/HDS300 is a USB instrument, so USB is the default. A LAN
        # address means nothing to it, so the field is only on screen for the
        # transport that uses it - otherwise it invites typing an address that
        # would be ignored.
        self.conn_address = ttk.Entry(transport, width=16)
        self.conn_address.insert(0, "10.1.1.131")
        # The LAN path is the SDS-series dialect (SDS6202, verified before the refactor).
        # It was not re-verified after the package split and is marked legacy in the README.
        self.conn_hint = tk.Label(transport, text="auto-detect", bg=PANEL, fg="#646c6c",
                                  font=("Segoe UI", 9))
        self.conn_lan_note = tk.Label(transport,
                                      text="LAN: SDS dialect (legacy, not re-verified)",
                                      bg=PANEL, fg="#a08060", font=("Segoe UI", 8))
        self.connect_btn = ttk.Button(transport, text="Connect", command=lambda: self.action(self.toggle_connection))
        self.connect_btn.pack(side="left", padx=5)
        self.sync_connection_fields()
        type_box.bind("<<ComboboxSelected>>", lambda event: self.sync_connection_fields())

        chassis = tk.Frame(self.root, bg=PANEL, bd=3, relief="ridge")
        chassis.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        chassis.grid_columnconfigure(0, weight=1)
        chassis.grid_rowconfigure(0, weight=1)
        display = tk.Frame(chassis, bg="#303638", bd=7, relief="sunken")
        display.grid(row=0, column=0, sticky="nsew", padx=(10, 7), pady=10)
        self.build_display(display)
        controls = tk.Frame(chassis, bg=PANEL, width=432)
        controls.grid(row=0, column=1, sticky="ns", padx=(3, 10), pady=10)
        controls.grid_columnconfigure((0, 1), weight=1)
        horizontal = self.group(controls, "HORIZONTAL", 0, 0)
        trigger = self.group(controls, "TRIGGER", 0, 1)
        self.build_horizontal(horizontal)
        self.build_trigger(trigger)
        vertical = self.group(controls, "VERTICAL", 1, 0, span=2)
        self.build_channels(vertical)
        acq = self.group(controls, "ACQUISITION", 2, 0, span=2)
        self.auto_refresh_var = tk.BooleanVar(value=False)
        # The button is the transport control and says what pressing it will do: a
        # pause glyph while the panel is capturing, a play glyph while it is not.
        self.live_btn = tk.Button(acq, command=self.toggle_live, bg="#e9e9e3",
                                  activebackground="#f4f4ec", relief="raised", bd=2,
                                  padx=9, pady=5, cursor="hand2", takefocus=True,
                                  text="LIVE", font=("Segoe UI", 9, "bold"))
        self.live_btn.pack(side="left", expand=True, fill="x", padx=3)
        self.update_live_button()
        self.key(acq, "CAPTURE", self.download_waveform).pack(side="left", expand=True, fill="x", padx=3)
        # AUTO is the front panel's autoset: find the signal and frame it.
        self.key(acq, "AUTO", self.auto_frame, color="#9cd4e8").pack(side="left", expand=True, fill="x", padx=3)
        self.key(acq, "SINGLE", self.single_trigger).pack(side="left", expand=True, fill="x", padx=3)
        self.key(acq, "RUN/STOP", self.toggle_run_stop, color="#c8e8a8").pack(side="left", expand=True, fill="x", padx=3)
        ttk.Label(controls, text="Knobs: drag • wheel • arrow keys",
                  foreground="#626968").grid(row=3, column=0, columnspan=2, pady=(9, 0))

        self.drawer = ttk.Notebook(display)
        self.build_drawer()
        footer = tk.Frame(self.root, bg=PANEL)
        footer.pack(side="bottom", fill="x", padx=18, pady=(0, 8))
        self.status_var = tk.StringVar(value="Ready | Disconnected")
        self.time_var = tk.StringVar()
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")
        ttk.Label(footer, textvariable=self.time_var).pack(side="right")
        self.update_time()
        self._poll_timer = self.root.after(80, self.poll_capture)
        # Before anything is attached the instrument's channel count is unknown,
        # so the panel opens showing one channel and reveals a second one only
        # once a two-channel instrument confirms it.
        try:
            self.apply_channel_count()
        except Exception:
            pass

    def group(self, parent, title, row, column, span=1):
        frame = ttk.LabelFrame(parent, text=title, padding=7)
        frame.grid(row=row, column=column, columnspan=span, sticky="nsew", padx=4, pady=5)
        return frame

    def key(self, parent, text, command, color="#e9e9e3"):
        button = tk.Button(parent, text=text, command=lambda: self.action(command),
                           bg=color, activebackground="#f4f4ec", fg=INK,
                           relief="raised", bd=2, padx=9, pady=7, cursor="hand2",
                           font=("Segoe UI", 9, "bold"), takefocus=True)
        if color == "#e9e9e3":
            width = max(90, len(text) * 8 + 24)
            if width not in self._button_images:
                # Bound to the widget's own interpreter: a photo image left to
                # the default root ends up in whichever window was created
                # first, and then does not exist for any other one.
                self._button_images[width] = ImageTk.PhotoImage(
                    Image.open(ASSETS / "button.png").convert("RGBA").resize(
                        (width, 40), Image.Resampling.LANCZOS), master=parent)
            button.configure(image=self._button_images[width], compound="center",
                             bg=PANEL, activebackground=PANEL, bd=0, padx=0, pady=0)
        return button

    def action(self, command):
        """Run a control's command.

        Not gated on an acquisition being in flight: with all instrument I/O on
        the one worker, a setting changed now is queued behind the frame being
        captured and applied a moment later, which is what a real instrument's
        own front panel does. Refusing the press instead made the panel read as
        unresponsive for half of every second during live acquisition.
        """
        command()

    def ready(self):
        if not self.scope.is_connected:
            self.status_var.set("Connect an oscilloscope to change instrument settings")
            return False
        return True

    def choices(self, method, fallback):
        """A choice list from the controller, falling back to the class constant.

        The HDS series takes different acquisition modes and memory depths from
        the older SDS dialect, so the controller answers for the instrument in
        front of it instead of the UI hard-coding one of them.
        """
        getter = getattr(self.scope, method, None)
        if callable(getter):
            try:
                values = list(getter() or ())
            except Exception:
                values = []
            if values:
                return values
        try:
            return list(getattr(self.scope, fallback, ()) or ())
        except Exception:
            return []

    def selector(self, parent, name, values, default, command, width=9):
        var = tk.StringVar(value=default)
        widget = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        setattr(self, name, widget)
        self._vars[name] = var
        previous = [default]
        widget.bind("<FocusIn>", lambda e: previous.__setitem__(0, var.get()))
        widget.configure(postcommand=lambda: previous.__setitem__(0, var.get()))
        def selected(_):
            if self.ready():
                command()
                previous[0] = var.get()
            else:
                var.set(previous[0])
        widget.bind("<<ComboboxSelected>>", selected)
        return widget

    def encoder(self, parent, name, values, default, command, size=68):
        combo = self.selector(parent, name, values, default, command)
        knob = Rotary(parent, self._vars[name],
                      lambda d: self.step_choice(name, values, d, command), size=size, values=values)
        knob.pack(pady=2)
        combo.pack(pady=2)
        return knob

    def step_choice(self, name, values, direction, command):
        if not self.ready():
            return
        widget = getattr(self, name)
        try:
            index = list(values).index(widget.get())
        except ValueError:
            index = 0
        target = min(len(values)-1, max(0, index+direction))
        if widget.get() != str(values[target]):
            widget.set(values[target])
            command()

    def numeric(self, parent, name, command, size=48, live=False, step=1.0):
        """A knob plus an entry box.

        ``live=True`` drops the Set button and applies the value as soon as it
        stops changing, the way a front-panel knob behaves. ``step`` is how much
        one detent moves the value, so a control measured in divisions does not
        jump a whole unit at a time.
        """
        var = tk.StringVar(value="0")
        self._vars[name] = var
        row = ttk.Frame(parent)
        row.pack(pady=1)
        knob = Rotary(row, var, lambda d: self.step_number(name, d, command, step, live), size=size)
        knob.pack(side="left", padx=(0, 3))
        entry = ttk.Entry(row, textvariable=var, width=7, justify="center")
        setattr(self, name, entry)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self.action(command))
        if live:
            var.trace_add("write", lambda *_: self.schedule_live(name, command))
        else:
            ttk.Button(row, text="Set", width=3, command=lambda: self.action(command)).pack(side="left", padx=2)
        return knob

    def schedule_live(self, name, command, delay=200):
        """Queue a live write, replacing any still pending for the same control."""
        if getattr(self, "_sync_depth", 0):
            # A readback is being written into the control right now. It came from
            # the instrument, so sending it straight back is at best a wasted round
            # trip and at worst a write the instrument never asked for.
            return
        if not self.root.winfo_exists():
            return
        pending = self._live_apply.get(name)
        if pending:
            try:
                self.root.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
        self._live_apply[name] = self.root.after(delay, lambda: self.apply_live(name, command))

    def apply_live(self, name, command):
        self._live_apply.pop(name, None)
        # The window may have gone while a live write was still queued.
        if not self.root.winfo_exists():
            return
        if self.ready():
            command()

    def step_number(self, name, direction, command, step=1.0, live=False):
        if not self.ready():
            return
        try:
            current = float(self._vars[name].get())
        except ValueError:
            if self._vars[name].get().strip():
                self.status_var.set("Enter a numeric value before turning this knob")
                return
            # An empty box is a value that has not been read yet, not a bad one:
            # the knob moves from zero instead of refusing to move at all.
            current = 0.0
        value = current + direction * step
        self._vars[name].set("%g" % value)
        if not live:
            command()

    def build_horizontal(self, parent):
        self.encoder(parent, "timebase_scale", self.scope.TIMEBASE_SCALES, "1ms", self.set_timebase)
        ttk.Label(parent, text="TIME / DIV", font=("Segoe UI", 9, "bold")).pack(pady=(1, 4))
        # Position is expressed in divisions, which is how a scope front panel
        # thinks about it, and applied live.
        self.numeric(parent, "timebase_offset", self.set_timebase_position, live=True, step=0.5)
        ttk.Label(parent, text="POSITION (div)", font=("Segoe UI", 9, "bold")).pack(pady=(2, 0))

    def build_trigger(self, parent):
        self.numeric(parent, "trigger_level", self.set_trigger_level, size=64,
                     live=True, step=0.1)
        ttk.Label(parent, text="TRIGGER LEVEL (V)", font=("Segoe UI", 9, "bold")).pack(pady=(0, 5))
        for label, name, values, default, command in (
            ("Mode", "trigger_mode", self.scope.TRIGGER_MODES, "AUTO", self.set_trigger_mode),
            ("Source", "trigger_source", self.scope.TRIGGER_SOURCES, "CH1", self.set_trigger_source),
            # The trigger's own coupling list, which is not the channel one: it
            # adds the HF and LF noise-rejecting filters.
            ("Coupling", "trigger_coupling", self.scope.TRIGGER_COUPLING, "DC",
             self.set_trigger_coupling),
            ("Slope", "trigger_slope", self.scope.TRIGGER_SLOPES, "RISE", self.set_trigger_slope)):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=6).pack(side="left")
            self.selector(row, name, values, default, command, width=7).pack(side="right")

    def build_channels(self, parent):
        # Only the channels the instrument actually has are shown; a
        # single-channel HDS271 answers nothing on :CH2:, so its whole column is
        # hidden rather than left sitting there dead.
        self._channel_frames = {}
        for ch, color in ((1, YELLOW), (2, CYAN)):
            col = tk.Frame(parent, bg=PANEL)
            self._channel_frames[ch] = col
            col.pack(side="left", expand=True, fill="both", padx=5)
            state = tk.BooleanVar(value=(ch == 1))
            setattr(self, f"ch{ch}_display", state)
            key = tk.Checkbutton(col, text=f"CH{ch} DISPLAY", variable=state, indicatoron=False,
                                bg="#bfc3bd", selectcolor=color, activebackground=color,
                                font=("Segoe UI", 9, "bold"), bd=2, relief="raised",
                                command=lambda c=ch, v=state: self.toggle_channel(c, v), pady=4)
            key.pack(fill="x", pady=(0, 3))
            command = lambda c=ch: self.set_channel_scale(c, getattr(self, f"ch{c}_scale").get())
            self.encoder(col, f"ch{ch}_scale", self.scope.VOLTAGE_SCALES, "1v", command, size=68)
            ttk.Label(col, text="VOLTS / DIV", font=("Segoe UI", 9, "bold")).pack()
            self.numeric(col, f"ch{ch}_offset",
                         lambda c=ch: self.set_channel_offset(c, getattr(self, f"ch{c}_offset").get()),
                         live=True, step=0.1)
            ttk.Label(col, text="POSITION (V)", font=("Segoe UI", 9, "bold")).pack(pady=(2, 0))
            for title, suffix, values, default, setter in (
                ("Coupling", "coupling", self.scope.COUPLING_MODES, "DC", self.set_channel_coupling),
                ("Probe", "probe", self.scope.PROBE_ATTEN, "X10", self.set_channel_probe)):
                row = ttk.Frame(col)
                row.pack(fill="x", pady=(5, 0))
                ttk.Label(row, text=title).pack(side="left", padx=(0, 4))
                name = f"ch{ch}_{suffix}"
                self.selector(row, name, values, default,
                              lambda c=ch, n=name, s=setter: s(c, getattr(self, n).get()),
                              width=5).pack(side="right")

    def toggle_channel(self, channel, variable):
        if self.ready():
            self.set_channel_display(channel, variable.get())
        else:
            variable.set(not variable.get())

    def show_channels(self, available):
        """Show only this many channels, on the panel and in the plot readouts.

        A single-channel HDS271 answers nothing on :CH2:, so its column, its plot
        readout and the measurement source are all removed rather than left on
        screen doing nothing.
        """
        for channel, frame in getattr(self, "_channel_frames", {}).items():
            if channel <= available:
                if not frame.winfo_manager():
                    frame.pack(side="left", expand=True, fill="both", padx=5)
            else:
                frame.pack_forget()

        for channel, widgets in getattr(self, "_readout_widgets", {}).items():
            for index, widget in enumerate(widgets):
                if channel <= available:
                    if not widget.winfo_manager():
                        widget.pack(side="left", padx=(12, 4) if index == 0 else (0, 12))
                else:
                    widget.pack_forget()

        source = getattr(self, "meas_source", None)
        if source is not None:
            values = tuple("CH%d" % number for number in range(1, available + 1))
            source.configure(values=values)
            if source.get() not in values:
                source.set(values[0])

        self.log("%s reports %d channel%s" % (
            getattr(self.scope, "model", None) or "instrument",
            available, "" if available == 1 else "s"))

    def apply_channel_count(self):
        """Probe the instrument, then show only the channels it really has.

        Range is probed rather than inferred from the model name, so a
        two-channel HDS200/HDS300 keeps its second column. The probe is a round
        trip, so it is made on the worker; showing them is a widget change.
        """
        if not getattr(self.scope, "is_connected", False):
            # Nothing attached yet: only CH1 is certain, and a column for a
            # channel the instrument does not have is the thing being avoided.
            self.show_channels(1)
            return
        self.report_later("channels", self.probe_channel_count,
                          lambda available, error=None: self.show_channels(available or 1))

    @staticmethod
    def probe_channel_count(scope):
        """How many channels the instrument answers for. On the worker."""
        try:
            return scope.get_channel_count()
        except Exception:
            # An instrument that will not say is treated as a single-channel scope
            # rather than guessed at from the model name.
            return 1

    def build_display(self, parent):
        top = tk.Frame(parent, bg=SCREEN)
        top.pack(fill="x")
        self.capture_state = tk.StringVar(value="NO ACQUISITION")
        # The state of the acquisition at a glance: what is running, how fast this
        # view is turning over, and a colour that says the same thing without
        # reading. The word is the mode, the glyph is what is happening in it.
        self.state_icon = tk.Label(top, bg=SCREEN)
        self.state_icon.pack(side="left", padx=(12, 6), pady=10)
        self.state_word = tk.Label(top, text="SINGLE", bg=SCREEN, fg=PAUSE_RED,
                                   font=("Consolas", 11, "bold"))
        self.state_word.pack(side="left", pady=10)
        self.state_rate = tk.Label(top, text="", bg=SCREEN, fg="#7f8c8b",
                                   font=("Consolas", 9))
        self.state_rate.pack(side="left", padx=(8, 0), pady=12)
        tk.Label(top, textvariable=self.device_info, bg=SCREEN, fg="#a5b9b5",
                 font=("Segoe UI", 9)).pack(side="right", padx=12)
        # Every path that changes what the panel is doing writes this variable, so
        # the indicator follows the variable rather than each of them remembering.
        self.capture_state.trace_add("write", self.update_state_indicator)
        self.update_state_indicator()
        self.fig.set_facecolor(SCREEN)
        self.fig.subplots_adjust(left=.085, right=.97, top=.95, bottom=.12)
        self.style_plot(empty=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().configure(width=400, height=300, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._connect_plot_events()
        readouts = tk.Frame(parent, bg=SCREEN)
        readouts.pack(fill="x", pady=(0, 8))
        self._readout_widgets = {}
        for ch, color in ((1, YELLOW), (2, CYAN)):
            tag = tk.Label(readouts, text=f"CH{ch}", fg=INK, bg=color,
                           font=("Consolas", 11, "bold"), padx=6)
            tag.pack(side="left", padx=(12, 4))
            text = tk.StringVar(value="— /div")
            setattr(self, f"ch{ch}_readout", text)
            value = tk.Label(readouts, textvariable=text, bg=SCREEN, fg=color,
                             font=("Consolas", 10))
            value.pack(side="left", padx=(0, 12))
            self._readout_widgets[ch] = (tag, value)
        # What the plot shows, and where the cursors are. Both are about reading
        # the capture, so they sit with the trace rather than in a drawer.
        strip = tk.Frame(parent, bg="#303638")
        strip.pack(fill="x", pady=(3, 0))
        for name, label in VIEW_LABELS:
            button = self.key(strip, label, (lambda view: lambda: self.set_view(view))(name),
                              color="#444c4e")
            button.configure(fg="#f1f3ed", activeforeground=INK, padx=6,
                             font=("Segoe UI", 8, "bold"))
            button.pack(side="left", padx=2, pady=2)
            self._view_buttons[name] = button
        self._refresh_view_buttons()
        tk.Label(strip, text="CURSORS", bg="#303638", fg="#8fa3a3",
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(12, 4))
        for key_name, label in CURSOR_TARGETS:
            button = self.key(strip, label,
                              (lambda which: lambda: self.set_cursor_target(which))(key_name),
                              color="#444c4e")
            button.configure(fg="#f1f3ed", activeforeground=INK, padx=5,
                             font=("Segoe UI", 8, "bold"))
            button.pack(side="left", padx=1, pady=2)
            self._cursor_buttons[key_name] = button
        self.key(strip, "CLEAR", self.clear_cursors, color="#444c4e").pack(side="left", padx=2)
        self._refresh_cursor_buttons()
        # The readout is the point of the cursors: what two markers measure.
        self.cursor_text = tk.StringVar(value="Cursors: none placed.")
        tk.Label(parent, textvariable=self.cursor_text, bg=SCREEN, fg="#9cf0c9",
                 font=("Consolas", 9), anchor="w").pack(fill="x", padx=12, pady=(4, 0))

        keys = tk.Frame(parent, bg="#303638")
        self._display_keys = keys
        keys.pack(fill="x", pady=(5, 0))
        # ZOOM +/- were removed: they moved the axes while live acquisition was
        # re-drawing them from the capture, so the view jumped back and forth.
        # Framing is the volts/div and time/div controls' job in the meantime.
        for label, command in (
            ("MEASURE", lambda: self.show_drawer(0)),
            ("ACQUIRE", lambda: self.show_drawer(1)),
            ("ANALYSE", lambda: self.show_drawer(2)),
            ("TABLE", self.show_table),
            ("SETUP", self.open_setup),
            ("OPEN", self.open_capture),
            ("FIT", self.auto_scale), ("SAVE", self.save_waveform),
            ("UTILITY", lambda: self.show_drawer(3))):
            button = self.key(keys, label, command, color="#444c4e")
            button.configure(fg="#f1f3ed", activeforeground=INK, padx=4, font=("Segoe UI", 8, "bold"))
            button.pack(side="left", fill="x", expand=True, padx=2, pady=2)

    def style_plot(self, empty=False):
        self.ax.set_facecolor(SCREEN)
        self.ax.set_title("")
        self.ax.tick_params(colors="#94a5a5", labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color("#526060")
        self.ax.xaxis.set_major_locator(MaxNLocator(nbins=10))
        self.ax.yaxis.set_major_locator(MaxNLocator(nbins=8))
        self.ax.xaxis.set_minor_locator(AutoMinorLocator(5))
        self.ax.yaxis.set_minor_locator(AutoMinorLocator(5))
        self.ax.grid(True, which="major", color="#6e8282", alpha=.35, linestyle=":")
        self.ax.grid(True, which="minor", color="#425050", alpha=.16, linestyle=":")
        self.ax.set_xlabel("Time", color="#94a5a5", fontsize=9)
        self.ax.set_ylabel("Voltage (V)", color="#94a5a5", fontsize=9)
        if empty:
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(-4, 4)
            self.ax.text(.5, .5, "READY TO ACQUIRE",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color="#748886", fontsize=13, fontweight="bold")
            self.ax.text(.5, .44,
                         "Press CAPTURE to read the screen" if self._is_connected()
                         else "Connect your scope, then press CAPTURE",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color="#617472", fontsize=10)

    def build_drawer(self):
        measure = ttk.Frame(self.drawer, padding=8)
        acquire = ttk.Frame(self.drawer, padding=10)
        utility = ttk.Frame(self.drawer, padding=8)
        analyse = ttk.Frame(self.drawer, padding=8)
        self.drawer.add(measure, text="Measurements")
        self.drawer.add(acquire, text="Acquisition")
        # Analyse sits before Utility, and the key row's indices rely on that order.
        self.drawer.add(analyse, text="Analyse")
        self.drawer.add(utility, text="Utility & log")
        self.build_analysis(analyse)
        self.meas_source = ttk.Combobox(measure, values=("CH1", "CH2"), state="readonly", width=6)
        self.meas_source.set("CH1")
        self.meas_source.pack(side="left", anchor="n", padx=5)
        self.key(measure, "READ", self.get_measurements).pack(side="left", anchor="n", padx=5)
        # The HDS271 carries a multimeter next to the scope: :DMM:MEAS? reads it
        # and :DMM:REL/:DMM:CONFigure set it up. It is a separate subsystem, so
        # its readout lives here rather than on the trace, and it is polled on
        # the same timer as the rest of the status.
        multimeter = ttk.Frame(measure, padding=(12, 0, 6, 0))
        multimeter.pack(side="left", anchor="n", padx=5)
        ttk.Label(multimeter, text="MULTIMETER", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.dmm_value = tk.StringVar(value="—")
        tk.Label(multimeter, textvariable=self.dmm_value, bg=SCREEN, fg="#9cf0c9",
                 font=("Consolas", 15, "bold"), width=13, anchor="e",
                 padx=8, pady=4).pack(pady=(3, 5))
        self.dmm_note = tk.StringVar(value="")
        self.dmm_relative = tk.BooleanVar(value=False)
        self.selector(multimeter, "dmm_function", ("VOLT", "AMP"), "VOLT",
                      self.set_dmm_function).pack(pady=1)
        self.selector(multimeter, "dmm_type", ("DC", "AC"), "DC",
                      self.set_dmm_function).pack(pady=1)
        # Only the functions this instrument actually answers are offered, and that
        # is found by asking it: the manual documents resistance, diode, continuity
        # and capacitance for the series, and this unit is silent for all four.
        self.dmm_mode = tk.StringVar(value="")
        self.dmm_mode_box = ttk.Combobox(multimeter, textvariable=self.dmm_mode, values=(),
                                         state="readonly", width=12)
        self.dmm_mode_box.pack(pady=(4, 1))
        self.key(multimeter, "SET MODE", self.set_dmm_mode).pack(fill="x", pady=(1, 2))
        self.key(multimeter, "REL", self.toggle_dmm_relative).pack(fill="x", pady=(5, 2))
        ttk.Label(multimeter, textvariable=self.dmm_note, wraplength=150,
                  foreground="#7a4a1e", font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))
        self.measurement_text = self.text_area(measure)
        for label, name, values, default, command in (
            ("Type", "acq_type", self.choices("acquire_mode_choices", "ACQ_TYPES"),
             "SAMPle", self.set_acquire_type),
            ("Averages", "acq_average", self.scope.AVG_COUNTS, "4", self.set_acquire_average),
            ("Memory", "mem_depth", self.choices("memory_depth_choices", "MEMORY_DEPTHS"),
             "4K", self.set_memory_depth)):
            column = ttk.Frame(acquire)
            column.pack(side="left", anchor="n", padx=15)
            ttk.Label(column, text=label).pack(anchor="w", pady=5)
            self.selector(column, name, values, default, command).pack()
        actions = ttk.Frame(utility)
        actions.pack(side="left", fill="y", padx=5)
        for label, command in (("Replot data", self.plot_waveform), ("Clear display", self.clear_plot),
                               ("Query settings", self.query_all_states), ("Reset scope", self.reset_scope)):
            self.key(actions, label, command).pack(fill="x", pady=1)
        self.log_text = self.text_area(utility)
        # Legacy log writes to both destinations; keep the console available in its own tab.
        console = ttk.Frame(self.drawer, padding=8)
        self.drawer.add(console, text="Console")
        self.console_text = self.text_area(console)
        self.drawer.bind("<Double-Button-1>", lambda e: self.hide_drawer())

    def text_area(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        text = tk.Text(frame, height=6, width=35, bg=SCREEN, fg="#c4d6d2",
                       font=("Consolas", 9), bd=0, padx=9, pady=6, yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        scroll.config(command=text.yview)
        return text

    def show_drawer(self, index):
        if self._drawer_open and self.drawer.index("current") == index:
            self.hide_drawer()
            return
        self.drawer.pack(fill="x", padx=4, pady=4, before=self._display_keys)
        self.drawer.select(index)
        self._drawer_open = True

    def hide_drawer(self):
        self.drawer.pack_forget()
        self._drawer_open = False

    def single_trigger(self):
        if self.ready():
            self.trigger_mode.set("SINGle")
            self.set_trigger_mode()

    def icon(self, kind, colour, size=16):
        """A transport glyph, drawn once per look and kept alive for Tk."""
        key = (kind, colour, size)
        image = self._state_icons.get(key)
        if image is None:
            image = transport_icon(kind, colour, size)
            self._state_icons[key] = image
        return image

    def show_scope_name(self, *_args):
        """Put the attached instrument's own model in the header.

        The name comes from the instrument itself (``*IDN?`` -> ``HDS272``), never from
        the dialect the app opened or the family it was built for, so a different unit on
        the same bench names itself correctly. Nothing attached says so, and the label is
        only touched when the text actually changes, since this runs on every live frame.
        """
        label = getattr(self, "brand_label", None)
        if label is None:
            return
        if not getattr(self.scope, "is_connected", False):
            text = UNCONNECTED_NAME
        else:
            model = str(getattr(self.scope, "model", "") or "").strip()
            text = model.upper() or CONNECTED_NAME
        if text != getattr(self, "_brand_shown", None):
            label.configure(text=text)
            # The window title says the same thing as the label, from this one place:
            # an operator looking at the taskbar should see which scope is attached, and
            # two titles that can disagree are one more thing to get wrong.
            self.root.title(text)
            self._brand_shown = text

    def update_state_indicator(self, *_args):
        """Say what the panel is doing: the mode, the rate, and a colour for it.

        LIVE is green and playing. A single capture in hand - one shot, AUTO, or a
        capture in flight - is amber and turning. Neither is red and paused. The
        rate is the rate this view is being redrawn at, which is the capture round
        trip; it is not the instrument's sample rate, which is a different number by
        three orders of magnitude and belongs to the instrument, not to this panel.

        Called from a trace on the state variable as well as directly, because every
        path that changes what the panel is doing goes through that variable: the
        indicator follows it instead of each path remembering to say so.
        """
        # The header names the instrument, and it is driven from the same place as the
        # state: every path that connects, disconnects or fails to acquire already comes
        # through here, so the name cannot drift out of step with the link.
        self.show_scope_name()
        state = str(self.capture_state.get() or "") if hasattr(self, "capture_state") else ""
        live_on = bool(getattr(self, "auto_refresh_var", None) is not None
                       and self.auto_refresh_var.get())
        if live_on:
            face, colour, word = "play", LIVE_GREEN, "LIVE"
        elif "FILE" in state.upper():
            # A capture from disk is not being acquired at all, and saying SINGLE
            # for it would be a claim about the instrument that is not true.
            face, colour, word = "refresh", HOLD_AMBER, "FILE"
        elif not getattr(self.scope, "is_connected", False):
            face, colour, word = "pause", PAUSE_RED, "OFFLINE"
        elif state.endswith("\u2026"):
            face, colour, word = "refresh", HOLD_AMBER, "SINGLE"
        else:
            face, colour, word = "pause", PAUSE_RED, "SINGLE"

        self._state_face = face
        icon = getattr(self, "state_icon", None)
        if icon is not None:
            icon.configure(image=self.icon(face, colour))
        word_label = getattr(self, "state_word", None)
        if word_label is not None:
            word_label.configure(text=word, foreground=colour)
        rate = getattr(self, "state_rate", None)
        if rate is not None:
            period = getattr(self, "_live_period", None)
            # The rate only means anything while the panel is the thing driving
            # the captures; a stopped panel has no rate to report.
            rate.configure(text="%.1f fps" % (1.0 / period) if face == "play" and period else "")

    def update_live_button(self):
        """The button is the transport control: pause when running, play when not."""
        button = getattr(self, "live_btn", None)
        if button is None:
            return
        running = bool(self.auto_refresh_var.get())
        kind = "pause" if running else "play"
        self._live_button_kind = kind
        button.configure(image=self.icon(kind, PAUSE_RED if running else LIVE_GREEN, 20))
        self.update_state_indicator()

    def toggle_live(self):
        if self.auto_refresh_var.get():
            self.auto_refresh_var.set(False)
            self.update_live_button()
            if not self._busy:
                self.capture_state.set("CAPTURED" if self.scope.waveform.channels else "NO ACQUISITION")
            if self._live_timer:
                self.root.after_cancel(self._live_timer)
                self._live_timer = None
        elif self.ready():
            self.auto_refresh_var.set(True)
            self.update_live_button()
            # A new run starts with no measured rate, so the label does not show
            # whatever the previous run happened to average.
            self._live_period = None
            self._live_frames = 0
            self.download_waveform()

    def start_worker(self, worker, label):
        """Run a background worker, and never leave the panel wedged.

        Every control is gated on ``_busy``, so a worker that cannot even be
        started - which is what happened when the AUTO command referred to a
        module the file never imported - would otherwise leave the whole panel
        refusing input, with nothing on screen to say why.
        """
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self._busy = False
            self.capture_state.set("ACQUISITION FAILED")
            self.status_var.set(str(exc))
            self.log(f"Could not start {label}: {exc}", "ERROR")
            return False
        return True

    def live_gap_ms(self):
        """How long to leave between live frames, from the bench settings.

        Default is 0: start the next capture the moment the previous one lands.
        The instrument is the floor (~450 ms over USB HID), so the displayed rate
        is the honest figure rather than whatever the interval was set to.
        A non-zero value slows things down intentionally (e.g. to reduce CPU load
        while recording to disk).
        """
        try:
            seconds = float(getattr(self.setup, "values", {}).get("live_interval_s") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        return max(0, int(seconds * 1000))

    def watch_framing(self):
        """Ask for the framing signature; the answer lands in on_instrument_result.

        Three cheap text reads, but they are read on the worker: asking here and
        handling the answer later is what keeps a 96 ms USB round trip out of the
        Tk callback. Coalesced, so a watch that has been overtaken by a newer one
        is dropped rather than queueing behind the frame the user is waiting for.
        """
        self.io.submit("framing", lambda scope: scope.framing_signature(), coalesce=True)

    def handle_framing(self, signature):
        """Act on a framing signature: drop the header when the instrument moved.

        When a value moves, the cached header is dropped so the next capture is
        parsed against the instrument's current framing, the change is logged, and
        :meth:`framing_changed` is called for the panel's own controls to follow.
        """
        if signature == self._framing_signature:
            return
        first = self._framing_signature is None
        self._framing_signature = signature
        if first:
            return
        self.scope.invalidate_capture_header()
        self.log("The instrument's framing changed: %s"
                 % ", ".join("%s %s" % (name, value) for name, value
                             in zip(self.scope.FRAMING_NAMES, signature)))
        self.framing_changed()

    def framing_changed(self):
        """Hook for a framing change seen on the instrument. The panel syncs here."""

    # ------------------------------------------------------------------
    # Views
    def set_view(self, name):
        """Switch what the plot shows.

        The capture is untouched - the same samples are drawn as a trace, a
        spectrum, a maths curve or an XY figure - so switching view never costs a
        trip to the instrument and works on a file just as well.
        """
        name = str(name or "time").lower()
        if name not in VIEWS:
            name = "time"
        self._view = name
        self._refresh_view_buttons()
        try:
            self.plot_waveform()
        except Exception as exc:
            self.log("Could not draw the %s view: %s" % (name, exc), "ERROR")

    def _refresh_view_buttons(self):
        for name, button in (self._view_buttons or {}).items():
            active = (name == self._view)
            button.configure(bg=("#f1f3ed" if active else "#444c4e"),
                             fg=(INK if active else "#f1f3ed"))

    def palette(self):
        """The colours the plot draws in, by name."""
        return PALETTES.get(getattr(self, "_palette_name", "Dark") or "Dark", PALETTES["Dark"])

    def set_palette(self, name):
        """Change the plot's colours, for a screen or for paper."""
        if name in PALETTES:
            self._palette_name = name
        self.style_plot()
        try:
            self.plot_waveform()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cursors
    def _connect_plot_events(self):
        self.canvas.mpl_connect("button_press_event", self._on_plot_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_plot_motion)
        self.canvas.mpl_connect("button_release_event", self._on_plot_release)

    def set_cursor_target(self, which):
        """Choose which marker a click on the plot will place."""
        if which in self._cursors:
            self._cursor_target = which
            self._refresh_cursor_buttons()
            self.status_var.set("Click the plot to place marker %s" % which.upper())

    def _refresh_cursor_buttons(self):
        for key_name, button in (self._cursor_buttons or {}).items():
            chosen = (key_name == self._cursor_target)
            placed = self._cursors.get(key_name) is not None
            button.configure(bg=("#9cdc9c" if chosen else "#444c4e"),
                             fg=(INK if chosen else ("#f1f3ed" if placed else "#8fa3a3")))

    def cursor_positions(self):
        """The markers, as placed."""
        return dict(self._cursors)

    def place_cursor(self, which, value):
        """Put a marker somewhere, in data units. Used by the tests and by reset."""
        if which in self._cursors and value is not None:
            self._cursors[which] = float(value)
        self._refresh_cursor_buttons()
        self.refresh_marker_lines()
        self.on_cursors_moved()

    def clear_cursors(self):
        for key_name in self._cursors:
            self._cursors[key_name] = None
        self._refresh_cursor_buttons()
        self.refresh_marker_lines()
        self.on_cursors_moved()

    def _on_plot_press(self, event):
        """Pick up a marker under the press, or place the selected one there."""
        if getattr(event, "inaxes", None) is None or event.xdata is None or event.ydata is None:
            return
        target = self._marker_under(event)
        if target is None:
            target = self._cursor_target
            self._store_cursor(target, event)
        self._drag_cursor = target
        self.refresh_marker_lines()
        self.on_cursors_moved()

    def _on_plot_motion(self, event):
        if not getattr(self, "_drag_cursor", None):
            return
        if getattr(event, "inaxes", None) is None or event.xdata is None or event.ydata is None:
            return
        self._store_cursor(self._drag_cursor, event)
        self.refresh_marker_lines()
        self.on_cursors_moved()

    def _on_plot_release(self, event):
        self._drag_cursor = None

    def _store_cursor(self, which, event):
        """A time marker takes the x of the click, a voltage marker takes the y."""
        if which in ("t1", "t2"):
            self._cursors[which] = float(event.xdata)
        elif which in ("v1", "v2"):
            self._cursors[which] = float(event.ydata)
        self._refresh_cursor_buttons()

    def _marker_under(self, event):
        """The placed marker whose line is closest to the press, within a few pixels.

        Pixels rather than data units: a marker has to be grabbable at any zoom,
        and a tolerance in volts would be unusable at one scale and unusable the
        other way at the next.
        """
        best, best_distance = None, CURSOR_GRAB_PIXELS
        for which, value in self._cursors.items():
            if value is None:
                continue
            if which in ("t1", "t2"):
                pixel = self.ax.transData.transform((value, self.ax.get_ylim()[0]))[0]
                distance = abs(pixel - event.x)
            else:
                pixel = self.ax.transData.transform((self.ax.get_xlim()[0], value))[1]
                distance = abs(pixel - event.y)
            if distance <= best_distance:
                best, best_distance = which, distance
        return best

    def refresh_marker_lines(self):
        """Draw the placed markers, and report what they measure.

        The artists are rebuilt on every call. On a full clear+redraw the old
        artists are gone already; on the fast path (no ax.clear) they are removed
        explicitly here before the new ones are added.
        """
        # Remove artists from the previous call so fast-path frames do not stack.
        for artist in getattr(self, "_cursor_artists", {}).values():
            try:
                artist.remove()
            except Exception:
                pass
        for artist in getattr(self, "_cursor_annots", []):
            try:
                artist.remove()
            except Exception:
                pass
        colours = {"t1": "#9cdc9c", "t2": "#9cdc9c", "v1": "#ffb86b", "v2": "#ffb86b"}
        self._cursor_artists = {}
        self._cursor_annots = []
        for which, value in (self._cursors or {}).items():
            if value is None:
                continue
            if which in ("t1", "t2"):
                artist = self.ax.axvline(value, color=colours[which], linewidth=1.1,
                                         linestyle="--", alpha=0.95)
                label = "%s %s" % (which.upper(), analysis.format_seconds(value))
                annot = self.ax.annotate(label, xy=(value, 1.0), xycoords=("data", "axes fraction"),
                                 xytext=(3, -10), textcoords="offset points",
                                 color=colours[which], fontsize=8, fontweight="bold")
            else:
                artist = self.ax.axhline(value, color=colours[which], linewidth=1.1,
                                         linestyle="--", alpha=0.95)
                label = "%s %s" % (which.upper(), analysis.format_volts(value))
                annot = self.ax.annotate(label, xy=(0.0, value), xycoords=("axes fraction", "data"),
                                 xytext=(4, 0), textcoords="offset points",
                                 color=colours[which], fontsize=8, fontweight="bold")
            self._cursor_artists[which] = artist
            self._cursor_annots.append(annot)
        self.canvas.draw_idle()

    def on_cursors_moved(self):
        """A marker moved: the readout is the app's to compute, since it has the traces."""
        return None

    # ------------------------------------------------------------------
    # The analysis drawer
    def build_analysis(self, parent):
        """The controls for what the analysis layer computes, and for reading it."""
        spectrum = ttk.Frame(parent, padding=(8, 4, 8, 4))
        spectrum.pack(side="left", anchor="n", padx=6)
        ttk.Label(spectrum, text="SPECTRUM", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        row = ttk.Frame(spectrum)
        row.pack(anchor="w", pady=2)
        for label, name, values, command in (
            ("Window", "fft_window", analysis.WINDOWS, self.set_fft_option),
            ("Format", "fft_format", analysis.FFT_FORMATS, self.set_fft_option)):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 3))
            box = ttk.Combobox(row, values=values, state="readonly", width=9)
            current = self._fft_options["window" if name == "fft_window" else "format"]
            box.set(current if current in values else values[0])
            box.pack(side="left", padx=(0, 8))
            box.bind("<<ComboboxSelected>>",
                     (lambda key, widget: lambda event: command(key, widget.get()))(name, box))
        peaks = ttk.Frame(spectrum)
        peaks.pack(anchor="w", pady=2)
        self.fft_log = tk.BooleanVar(value=bool(self._fft_options.get("log")))
        tk.Checkbutton(peaks, text="Log frequency", variable=self.fft_log, bg=PANEL, fg=INK,
                       selectcolor=PANEL, activebackground=PANEL,
                       command=lambda: self.set_fft_option("log", self.fft_log.get())).pack(side="left")
        self.key(peaks, "PEAKS", self.report_peaks).pack(side="left", padx=6)

        maths = ttk.Frame(parent, padding=(8, 4, 8, 4))
        maths.pack(side="left", anchor="n", padx=6)
        ttk.Label(maths, text="MATHS", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        row = ttk.Frame(maths)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text="Op").pack(side="left", padx=(0, 3))
        op_box = ttk.Combobox(row, values=analysis.MATH_OPS, state="readonly", width=9)
        op_box.set(self._math_options.get("op", "subtract"))
        op_box.pack(side="left", padx=(0, 8))
        op_box.bind("<<ComboboxSelected>>",
                    lambda event: self.set_math_option("op", op_box.get()))
        ttk.Label(row, text="with").pack(side="left", padx=(0, 3))
        second_box = ttk.Combobox(row, values=("CH1", "CH2", "CH3", "CH4"), state="readonly", width=5)
        second_box.set(self._math_options.get("second", "CH2"))
        second_box.pack(side="left")
        second_box.bind("<<ComboboxSelected>>",
                        lambda event: self.set_math_option("second", second_box.get()))

        tools = ttk.Frame(parent, padding=(8, 4, 8, 4))
        tools.pack(side="left", anchor="n", padx=6)
        ttk.Label(tools, text="READ & SEND", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        row = ttk.Frame(tools)
        row.pack(anchor="w", pady=2)
        self.key(row, "DATA TABLE", self.show_table).pack(side="left", padx=2)
        self.key(row, "OPEN FILE", self.open_capture).pack(side="left", padx=2)
        self.key(row, "SCPI", self.open_console, color="#9cd4e8").pack(side="left", padx=2)
        row = ttk.Frame(tools)
        row.pack(anchor="w", pady=2)
        self.key(row, "TRIGGER…", self.open_trigger_state).pack(side="left", padx=2)
        self.key(row, "AUTO TIME", self.software_autoset, color="#9cdc9c").pack(side="left", padx=2)
        row = ttk.Frame(tools)
        row.pack(anchor="w", pady=2)
        self.record_var = tk.BooleanVar(value=False)
        self.key(row, "RECORD", self.toggle_recording, color="#ffd166").pack(side="left", padx=2)
        self.record_label = tk.StringVar(value="idle")
        tk.Label(row, textvariable=self.record_label, bg=PANEL, fg="#7a6a2e",
                 font=("Segoe UI", 8)).pack(side="left", padx=6)

        # The spectrum and maths options are read back by the app when it draws.
        self.spectrum_text = self.text_area(parent)

    def set_fft_option(self, key, value):
        self._fft_options[key] = value
        if self._view == "fft":
            self.set_view("fft")
        else:
            self.status_var.set("FFT %s: %s (shown in the FFT view)" % (key, value))

    def set_math_option(self, key, value):
        self._math_options[key] = value
        if self._view == "math":
            self.set_view("math")

    # ------------------------------------------------------------------
    # Dialogs
    def show_table(self):
        """The samples as a table: the view a spreadsheet would give, in the app."""
        columns, rows = self.table_data()
        if not rows:
            messagebox.showinfo("Data table", "No capture to show. Take one first.")
            return
        CaptureTableDialog(self.root, columns, rows, on_save=self.save_waveform)

    def open_capture(self):
        """Read a capture back from a file, the way the vendor's player does."""
        path = filedialog.askopenfilename(
            title="Open a capture", filetypes=[("Capture files", "*.csv *.json"),
                                               ("CSV", "*.csv"), ("JSON", "*.json"),
                                               ("All files", "*.*")])
        if path:
            self.load_capture_file(path)

    def open_console(self):
        """A console for the instrument's own command language.

        Read-only until the operator says otherwise: this is the tool that
        established how this dialect behaves, and it can also set the instrument
        into states that a capture will then report as broken.
        """
        dialog = ScpiConsoleDialog(self.root, on_send=self.run_scpi_command, scope=self.scope)
        # Replies arrive after the dialog is open, so the app needs somewhere to
        # put them; cleared when the window goes, so nothing is written into a
        # destroyed widget.
        self._console_sink = dialog.append
        dialog.bind("<Destroy>", lambda event: setattr(self, "_console_sink", None), add="+")

    def open_trigger_state(self):
        """What the instrument says its trigger is doing, right now."""
        ReadoutDialog(self.root, "Trigger and acquisition", None,
                      request=self.trigger_state_rows,
                      on_refresh=self.refresh_trigger_state)

    # ------------------------------------------------------------------
    # Placeholders the app fills in
    def report_peaks(self):
        return None

    def table_data(self):
        return [], []

    def load_capture_file(self, path):
        return None

    def run_scpi_command(self, text, allow_writes):
        return "not connected"

    def trigger_state_rows(self):
        return []

    def refresh_trigger_state(self):
        return None

    def software_autoset(self):
        return None

    def toggle_recording(self):
        return None

    def download_waveform(self, reuse_header=False):
        """Ask for a capture; it is fetched on the worker and drawn when it lands.

        ``reuse_header=True`` is the live path: it skips re-reading the capture
        header, which is a third of the frame, and lets the plot run at about
        twice the rate. A capture the user asked for gets a fresh header.

        Nothing here waits: the request goes on the queue and ``poll_capture``
        picks the finished frame up. This method is called from Tk callbacks, so
        every microsecond it spends is a microsecond the window cannot repaint.
        """
        if not self.ready():
            return
        if self._live_timer:
            self.root.after_cancel(self._live_timer)
            self._live_timer = None
        self._busy = True
        self._live_started = time.monotonic()
        self.capture_state.set("ACQUIRING…")
        self.status_var.set("Downloading waveform…")
        # Calibrate every capture the user asked for, and now and then in the live
        # loop. The measurement block costs a few round trips; without it the volts
        # come from the header's volts/div label, and this firmware does not keep
        # that label in step with the gain.
        calibrate = (not reuse_header) or (self._live_frames % LIVE_CALIBRATE_EVERY == 0)
        self.ask_scope("capture",
                       lambda scope: scope.download_waveform_data(reuse_header=reuse_header,
                                                                  calibrate=calibrate),
                       priority=PRIORITY_CAPTURE)

    def ask_scope(self, kind, work, coalesce=False, priority=PRIORITY_COMMAND):
        """Hand an instrument call to the I/O worker. Returns its token.

        One place where the panel talks to the instrument, so that every call is
        serialised on the single owner the endpoint requires and none of them runs
        on the Tk thread.
        """
        return self.io.submit(kind, work, coalesce=coalesce, priority=priority)

    def report_later(self, kind, work, deliver, coalesce=False):
        """Run an instrument call and hand its result to ``deliver`` when it lands.

        The callback travels with the submission, so a dialog can ask for something
        that costs a few round trips and fill itself in when the answer arrives.
        Nothing on this panel reads the instrument on the Tk thread any more, which
        is what makes that necessary rather than a nicety.
        """
        token = self.ask_scope(kind, work, coalesce=coalesce)
        if token is not None and deliver is not None:
            self._deliveries[token] = deliver
        return token

    def tell_scope(self, description, work, coalesce=False):
        """Send a setting from the worker, and log what was asked for.

        The write is queued behind whatever is in flight instead of blocking the
        window, which is what a front panel does: the knob moves now and the
        instrument follows. The log line is the user's request; what the
        instrument made of it is the controller's to report.
        """
        self.log(description)
        return self.ask_scope("write", work, coalesce=coalesce)

    def on_instrument_result(self, kind, token, value, error):
        """A finished instrument call, back on the UI thread.

        Returns True when this kind was handled here. Subclasses handle their own
        kinds first and fall through to this.
        """
        if kind == "capture":
            self.finish_capture(value, error)
            return True
        if kind == "framing":
            if error is not None:
                self.log("Framing watch failed: %s" % error)
            else:
                self.handle_framing(value)
            return True
        return False

    def poll_capture(self):
        """Drain finished instrument calls and keep the live loop turning.

        This is the only place results are applied, and it never calls the
        instrument itself, so the frame the user is waiting for is the only thing
        that can make the window wait.
        """
        def dispatch(kind, token, value, error):
            if not self.on_instrument_result(kind, token, value, error):
                self.log("Unhandled instrument reply: %s" % kind, "WARNING")

        self.io.poll(dispatch)
        if not self._closing or self._busy:
            self._poll_timer = self.root.after(80, self.poll_capture)
            # The multimeter is read on the same timer, but far less often: one
            # query is ~32 ms and its reading does not move quickly.
            self._dmm_tick = (self._dmm_tick + 1) % 8
            if self._dmm_tick == 0:
                self.poll_dmm()

    def finish_capture(self, success, error):
        """A frame arrived: draw it, and ask for the next one if live."""
        self._busy = False
        if self._closing:
            self.finish_close()
            return
        if success:
            self._capture_failures = 0
            # The readbacks are asked for, not performed: they are three more USB
            # round trips, and the next frame must not queue behind them.
            self.refresh_cursors()
            self.plot_waveform()
            # Unattended recording, if it is on: one file per frame, written from
            # the worker so the disk does not hold up the drawing.
            self.record_capture()
            if self.auto_refresh_var.get():
                # Show the rate actually achieved rather than the rate asked for: a
                # capture is instrument-limited, so the honest figure is the one
                # measured between frames.
                now = time.monotonic()
                if self._live_started:
                    period = now - self._live_started
                    self._live_period = (period if self._live_period is None
                                         else 0.7 * self._live_period + 0.3 * period)
                self.capture_state.set("LIVE • %.1f s" % self._live_period
                                       if self._live_period else "LIVE")
                # The rate is measured per frame, so the indicator is refreshed
                # here rather than only when the state itself changes.
                self.update_state_indicator()
            else:
                self.capture_state.set("CAPTURED")
            self.status_var.set("Waveform acquired")
            self.report_auto_frame()
        else:
            self._capture_failures += 1
            self.log(error or "Waveform download failed", "ERROR")
            if self.auto_refresh_var.get():
                self._attempt_reconnect(error)
                return          # _attempt_reconnect drives the next state
            else:
                self.capture_state.set("ACQUISITION FAILED")
                self.status_var.set(error or "Waveform download failed")
        if self.auto_refresh_var.get():
            # Reuse the header on most frames, and re-read it now and then so a
            # change made on the front panel still reaches the plot.
            self._live_frames += 1
            # The framing gets watched on its own, cheaper schedule: three 32 ms
            # text reads against the 254 ms header. A volts/div set from the
            # instrument's menu would otherwise sit unseen until the header came
            # round - seconds during which the time base, whose label is read every
            # frame, appears to work and the volts/div does not.
            if (self._live_frames % LIVE_FRAMING_EVERY) == 0:
                self.watch_framing()
            reuse = (self._live_frames % LIVE_HEADER_EVERY) != 0
            self._live_timer = self.root.after(
                self.live_gap_ms(), lambda: self.download_waveform(reuse_header=reuse))


    # Maximum consecutive capture failures before the watchdog gives up and
    # stops LIVE rather than looping forever.
    _RECONNECT_GIVE_UP = 5

    def _attempt_reconnect(self, last_error=None):
        """Watchdog: try to recover a dropped USB connection, then resume LIVE.

        Called from ``finish_capture`` when a live capture fails.  Drives the
        state machine on the worker thread so the UI thread never blocks:

        - 1st failure  -> endpoint halt-clear (cheap; repairs a stalled pipe)
        - 2nd+ failure -> full disconnect + reconnect (repairs firmware reset /
                          unplug-replug)
        - >= _RECONNECT_GIVE_UP failures -> give up, stop LIVE, say why

        Every branch posts a result back as kind="reconnect" so
        ``on_instrument_result`` can update the panel on the Tk thread.
        """
        n = self._capture_failures
        if n >= self._RECONNECT_GIVE_UP:
            self.auto_refresh_var.set(False)
            self.update_live_button()
            self.capture_state.set("DISCONNECTED")
            self.status_var.set("Reconnect failed after %d attempts \u2014 press Connect" % n)
            self.log("Auto-reconnect gave up after %d consecutive failures. "
                     "Press Connect to retry." % n)
            return

        if n == 1:
            # First failure: try a cheap halt-clear before giving up the frame.
            self.capture_state.set("RECOVERING\u2026")
            self.status_var.set("Connection stalled \u2014 clearing endpoint\u2026")
            def do_halt_clear(scope):
                t = getattr(scope, "connection", None)
                if t is not None and hasattr(t, "recover"):
                    t.recover()
                return scope.get_idn() or ""
            self.report_later("reconnect", do_halt_clear, self._finish_reconnect)
        else:
            # Subsequent failures: full close + reopen.
            self.capture_state.set("RECONNECTING\u2026")
            self.status_var.set("Reconnecting\u2026 (attempt %d)" % n)
            self.log("Capture failed %d time(s) in a row \u2014 attempting full reconnect." % n)
            self.report_later("reconnect", lambda scope: scope.reconnect(),
                              self._finish_reconnect)

    def _finish_reconnect(self, answer, error=None):
        """Handle the watchdog result on the Tk thread.

        ``answer`` is either an idn string (halt-clear path) or a
        ``(success, idn)`` tuple (full reconnect path).
        """
        if error is not None:
            self.log("Reconnect raised: %s" % error, "ERROR")
            self._capture_failures += 1
            self._attempt_reconnect()
            return

        # Normalise: halt-clear returns a string, full reconnect a tuple.
        if isinstance(answer, tuple):
            opened, idn = answer
        else:
            opened = bool(answer)
            idn = answer if isinstance(answer, str) else ""

        if not opened:
            self.log("Reconnect attempt %d failed." % self._capture_failures, "ERROR")
            self._capture_failures += 1
            self._attempt_reconnect()
            return

        # Back online.
        self._capture_failures = 0
        self.log("Reconnected: %s" % (idn or "instrument answered"))
        self.capture_state.set("LIVE")
        if idn:
            self.device_info.set(idn)
        # Invalidate the cached header so the first post-reconnect frame is
        # parsed fresh \u2014 the instrument may have reset its settings.
        try:
            self.scope.invalidate_capture_header()
        except Exception:
            pass
        # Resume the live loop from a clean slate.
        self._live_period = None
        self._live_frames = 0
        self.download_waveform(reuse_header=False)

    def wait_for_instrument(self, timeout=5.0):
        """Run the worker's queue to completion, applying results as they land.

        The app itself never calls this: it drains from its own timer, which is what
        keeps the window live. This exists so a test, or a headless probe, can assert
        what a control did without a mainloop - driving the same path the UI drives
        rather than a parallel one.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.io.poll(lambda kind, token, value, error:
                         self.on_instrument_result(kind, token, value, error))
            if self.io.quiet():
                return True
            time.sleep(0.005)
        return False

    def close_panel(self):
        self._closing = True
        self.auto_refresh_var.set(False)
        if self._live_timer:
            self.root.after_cancel(self._live_timer)
        if self._busy:
            self.status_var.set("Finishing acquisition before closing…")
        else:
            self.finish_close()


    def finish_close(self):
        # The worker is stopped before the widgets go: a result arriving after the
        # canvas is gone would be applied to a destroyed widget.
        try:
            self.io.stop(timeout=1.0)
            self.io.drain()
        except Exception as exc:                                   # noqa: BLE001
            self.log("Instrument worker did not stop cleanly: %s" % exc)
        for name in ("_clock_timer", "_poll_timer", "_live_timer"):
            timer = getattr(self, name, None)
            if timer:
                self.root.after_cancel(timer)
        # Live controls queue a write per knob movement; none may outlive the window.
        for timer in list(getattr(self, "_live_apply", {}).values()):
            if timer:
                try:
                    self.root.after_cancel(timer)
                except (tk.TclError, ValueError):
                    pass
        self._live_apply.clear()
        self.scope.disconnect()
        self.root.destroy()


