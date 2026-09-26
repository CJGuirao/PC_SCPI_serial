"""Modern Lab front panel. Instrument operations remain in main.App."""
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

from PIL import Image, ImageTk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import AutoMinorLocator, MaxNLocator

from scope_setup import PROBE_CHOICES, ScopeSetup, device_label

PANEL = "#d5d4cf"
INK = "#252a2c"
SCREEN = "#101719"
YELLOW = "#ffe33e"
CYAN = "#26d7e8"
ASSETS = Path(__file__).resolve().parent / "assets" / "modern_lab"

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

#: Live frames between calibrations against the instrument's own readings. The
#: measurement block is a few round trips, and it is what keeps the volts right
#: when the header's volts/div label has drifted from the gain.
LIVE_CALIBRATE_EVERY = 8

#: Re-read the capture header every this many live frames. The header is a
#: measured 254-352 ms of each frame and only changes when a setting does, so it
#: is reused; re-reading it this often bounds how long a change made on the
#: instrument's own front panel can go unnoticed by the plot.
LIVE_HEADER_EVERY = 12


class Rotary(tk.Canvas):
    """A focusable detented encoder: drag up/right, wheel, or arrow keys."""
    def __init__(self, parent, variable, change, size=86, values=None):
        super().__init__(parent, width=size, height=size, bg=PANEL,
                         highlightthickness=2, highlightbackground=PANEL,
                         highlightcolor="#697e87", takefocus=True, cursor="hand2")
        self.variable, self.change, self.values = variable, change, values
        self.size = size
        self.sprite = ImageTk.PhotoImage(
            Image.open(ASSETS / "knob.png").convert("RGBA").resize((size, size), Image.Resampling.LANCZOS),
            master=self)
        self.create_image(size / 2, size / 2, image=self.sprite)
        self.marker = self.create_line(0, 0, 0, 0, fill="#f5f4e9", width=3, capstyle=tk.ROUND)
        self.anchor = None
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "anchor", None))
        self.bind("<MouseWheel>", lambda e: self.step(1 if e.delta > 0 else -1))
        self.bind("<Button-4>", lambda e: self.step(1))
        self.bind("<Button-5>", lambda e: self.step(-1))
        for key in ("Up", "Right"):
            self.bind("<" + key + ">", lambda e: self.step(1))
        for key in ("Down", "Left"):
            self.bind("<" + key + ">", lambda e: self.step(-1))
        self.variable.trace_add("write", self.redraw)
        self.redraw()

    def redraw(self, *_):
        try:
            value = self.variable.get()
            if self.values:
                index = list(self.values).index(value)
                angle = -135 + 270 * index / max(1, len(self.values) - 1)
            else:
                angle = float(value) * 6
        except (ValueError, tk.TclError):
            angle = 0
        rad = math.radians(angle - 90)
        c = self.size / 2
        self.coords(self.marker, c + math.cos(rad)*self.size*.20,
                    c + math.sin(rad)*self.size*.20,
                    c + math.cos(rad)*self.size*.32,
                    c + math.sin(rad)*self.size*.32)

    def press(self, event):
        self.focus_set()
        self.anchor = (event.x, event.y)

    def drag(self, event):
        if self.anchor:
            delta = event.x - self.anchor[0] + self.anchor[1] - event.y
            if abs(delta) >= 10:
                self.step(1 if delta > 0 else -1)
                self.anchor = (event.x, event.y)

    def step(self, direction):
        self.change(direction)
        return "break"


class ModernLabUI:
    def setup_gui(self):
        self.root.title("Modern Lab • OWON Oscilloscope")
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
        self._results = queue.Queue()
        self._live_timer = None
        self._live_started = None
        self._live_period = None
        self._live_frames = 0
        self._dmm_tick = 0
        #: Last framing signature seen from the instrument, for the live watch.
        self._framing_signature = None
        self._drawer_open = False
        self._vars = {}
        # Live controls apply shortly after the value settles, so a dragged knob
        # does not flood the instrument with one write per detent.
        self._live_apply = {}
        self._button_images = {}
        self.root.protocol("WM_DELETE_WINDOW", self.close_panel)

        header = tk.Frame(self.root, bg=PANEL)
        header.pack(fill="x", padx=20, pady=(12, 8))
        tk.Label(header, text="MODERN LAB", bg=PANEL, fg=INK,
                 font=("Segoe UI", 19, "bold")).pack(side="left")
        tk.Label(header, text="  /  DIGITAL STORAGE OSCILLOSCOPE", bg=PANEL,
                 fg="#646c6c", font=("Segoe UI", 9)).pack(side="left", padx=8)
        self.conn_type = tk.StringVar(value="usb")
        self.device_info = tk.StringVar(value="Not connected")
        type_box = ttk.Combobox(header, textvariable=self.conn_type, values=("lan", "usb"),
                                state="readonly", width=5)
        type_box.pack(side="left", padx=(20, 5))
        # The HDS200/HDS300 is a USB instrument, so USB is the default. A LAN
        # address means nothing to it, so the field is only on screen for the
        # transport that uses it - otherwise it invites typing an address that
        # would be ignored.
        self.conn_address = ttk.Entry(header, width=16)
        self.conn_address.insert(0, "10.1.1.131")
        self.conn_hint = tk.Label(header, text="auto-detect", bg=PANEL, fg="#646c6c",
                                  font=("Segoe UI", 9))
        self.connect_btn = ttk.Button(header, text="Connect", command=lambda: self.action(self.toggle_connection))
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
        self.live_btn = self.key(acq, "LIVE REFRESH", self.toggle_live, color="#9cdc9c")
        self.live_btn.configure(command=self.toggle_live)
        self.live_btn.pack(side="left", expand=True, fill="x", padx=3)
        self.key(acq, "CAPTURE", self.download_waveform).pack(side="left", expand=True, fill="x", padx=3)
        # AUTO is the front panel's autoset: find the signal and frame it.
        self.key(acq, "AUTO", self.auto_frame, color="#9cd4e8").pack(side="left", expand=True, fill="x", padx=3)
        self.key(acq, "SINGLE", self.single_trigger).pack(side="left", expand=True, fill="x", padx=3)
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
        if self._busy:
            self.status_var.set("Acquiring waveform… please wait")
            return
        command()

    def ready(self):
        if not self.scope.is_connected:
            self.status_var.set("Connect an oscilloscope to change instrument settings")
            return False
        if self._busy:
            self.status_var.set("Acquiring waveform… please wait")
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
            value = float(self._vars[name].get()) + direction * step
        except ValueError:
            self.status_var.set("Enter a numeric value before turning this knob")
            return
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
        two-channel HDS200/HDS300 keeps its second column.
        """
        if not getattr(self.scope, "is_connected", False):
            # Nothing attached yet: only CH1 is certain, and a column for a
            # channel the instrument does not have is the thing being avoided.
            self.show_channels(1)
            return
        try:
            available = self.scope.get_channel_count()
        except Exception:
            available = 1
        self.show_channels(available)

    def build_display(self, parent):
        top = tk.Frame(parent, bg=SCREEN)
        top.pack(fill="x")
        self.capture_state = tk.StringVar(value="NO ACQUISITION")
        tk.Label(top, textvariable=self.capture_state, bg=SCREEN, fg="#a5b9b5",
                 font=("Consolas", 10, "bold")).pack(side="left", padx=12, pady=9)
        tk.Label(top, textvariable=self.device_info, bg=SCREEN, fg="#a5b9b5",
                 font=("Segoe UI", 9)).pack(side="right", padx=12)
        self.fig.set_facecolor(SCREEN)
        self.fig.subplots_adjust(left=.085, right=.97, top=.95, bottom=.12)
        self.style_plot(empty=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().configure(width=400, height=300, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
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
        keys = tk.Frame(parent, bg="#303638")
        self._display_keys = keys
        keys.pack(fill="x", pady=(5, 0))
        # ZOOM +/- were removed: they moved the axes while live acquisition was
        # re-drawing them from the capture, so the view jumped back and forth.
        # Framing is the volts/div and time/div controls' job in the meantime.
        for label, command in (
            ("MEASURE", lambda: self.show_drawer(0)),
            ("ACQUIRE", lambda: self.show_drawer(1)),
            ("SETUP", self.open_setup),
            ("FIT", self.auto_scale), ("SAVE", self.save_waveform),
            ("UTILITY", lambda: self.show_drawer(2))):
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
        self.drawer.add(measure, text="Measurements")
        self.drawer.add(acquire, text="Acquisition")
        self.drawer.add(utility, text="Utility & log")
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

    def toggle_live(self):
        if self.auto_refresh_var.get():
            self.auto_refresh_var.set(False)
            self.live_btn.configure(text="LIVE REFRESH", bg="#9cdc9c")
            if not self._busy:
                self.capture_state.set("CAPTURED" if self.scope.waveform_data.channels else "NO ACQUISITION")
            if self._live_timer:
                self.root.after_cancel(self._live_timer)
                self._live_timer = None
        elif self.ready():
            self.auto_refresh_var.set(True)
            self.live_btn.configure(text="PAUSE REFRESH", bg="#f3bd75")
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

    def watch_framing(self):
        """Notice a framing change made on the instrument and re-read the header.

        Only three cheap text reads. When one moves, the cached header is dropped
        so the next capture is parsed against the instrument's current framing, the
        change is logged, and :meth:`framing_changed` is called for the panel's own
        controls to follow.
        """
        try:
            signature = self.scope.framing_signature()
        except Exception as exc:                                   # noqa: BLE001
            self.log("Framing watch failed: %s" % exc)
            return
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

    def download_waveform(self, reuse_header=False):
        """Start a capture on a worker thread.

        ``reuse_header=True`` is the live path: it skips re-reading the capture
        header, which is a third of the frame, and lets the plot run at about
        twice the rate. A capture the user asked for gets a fresh header.
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
        def worker():
            try:
                # Calibrate every capture the user asked for, and now and then in
                # the live loop. The measurement block costs a few round trips;
                # without it the volts come from the header's volts/div label, and
                # this firmware does not keep that label in step with the gain.
                calibrate = (not reuse_header) or (self._live_frames % LIVE_CALIBRATE_EVERY == 0)
                self._results.put(
                    (self.scope.download_waveform_data(reuse_header=reuse_header,
                                                       calibrate=calibrate), None))
            except Exception as exc:
                self._results.put((False, str(exc)))
        self.start_worker(worker, "capture")

    def poll_capture(self):
        try:
            success, error = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            if self._closing:
                self.finish_close()
                return
            if success:
                self.refresh_cursors()
                self.plot_waveform()
                if self.auto_refresh_var.get():
                    # Show the rate actually achieved rather than the rate asked
                    # for: a capture is instrument-limited, so the honest figure
                    # is the one measured between frames.
                    now = time.monotonic()
                    if self._live_started:
                        period = now - self._live_started
                        self._live_period = (period if self._live_period is None
                                             else 0.7 * self._live_period + 0.3 * period)
                    self.capture_state.set("LIVE • %.1f s" % self._live_period
                                           if self._live_period else "LIVE")
                else:
                    self.capture_state.set("CAPTURED")
                self.status_var.set("Waveform acquired")
                self.report_auto_frame()
            else:
                self.capture_state.set("ACQUISITION FAILED")
                self.status_var.set(error or "Waveform download failed")
                self.log(error or "Waveform download failed", "ERROR")
                if self.auto_refresh_var.get():
                    self.toggle_live()
                self.capture_state.set("ACQUISITION FAILED")
            if self.auto_refresh_var.get():
                # Reuse the header on most frames, and re-read it now and then so
                # a change made on the front panel still reaches the plot.
                self._live_frames += 1
                # The framing gets watched on its own, cheaper schedule: three
                # 32 ms text reads against the 254 ms header. A volts/div set from
                # the instrument's menu would otherwise sit unseen until the header
                # came round - seconds during which the time base, whose label is
                # read every frame, appears to work and the volts/div does not.
                if (self._live_frames % LIVE_FRAMING_EVERY) == 0:
                    self.watch_framing()
                reuse = (self._live_frames % LIVE_HEADER_EVERY) != 0
                self._live_timer = self.root.after(
                    LIVE_GAP_MS, lambda: self.download_waveform(reuse_header=reuse))
        if not self._closing or self._busy:
            self._poll_timer = self.root.after(80, self.poll_capture)
            # The multimeter is read on the same timer, but far less often: one
            # query is ~32 ms and its reading does not move quickly.
            self._dmm_tick = (self._dmm_tick + 1) % 8
            if self._dmm_tick == 0:
                self.poll_dmm()

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
        from scope_setup import attached_scopes
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
        }, path=self.setup.path)

    def save_and_apply(self):
        if self.on_apply:
            self.on_apply(self.collect(), True)

    def save_only(self):
        if self.on_apply:
            self.on_apply(self.collect(), False)
