"""Modern Lab front panel. Instrument operations remain in main.App."""
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path

from PIL import Image, ImageTk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import AutoMinorLocator, MaxNLocator

PANEL = "#d5d4cf"
INK = "#252a2c"
SCREEN = "#101719"
YELLOW = "#ffe33e"
CYAN = "#26d7e8"
ASSETS = Path(__file__).resolve().parent / "assets" / "modern_lab"


class Rotary(tk.Canvas):
    """A focusable detented encoder: drag up/right, wheel, or arrow keys."""
    def __init__(self, parent, variable, change, size=86, values=None):
        super().__init__(parent, width=size, height=size, bg=PANEL,
                         highlightthickness=2, highlightbackground=PANEL,
                         highlightcolor="#697e87", takefocus=True, cursor="hand2")
        self.variable, self.change, self.values = variable, change, values
        self.size = size
        self.sprite = ImageTk.PhotoImage(
            Image.open(ASSETS / "knob.png").convert("RGBA").resize((size, size), Image.Resampling.LANCZOS))
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
        self._drawer_open = False
        self._vars = {}
        self._button_images = {}
        self.root.protocol("WM_DELETE_WINDOW", self.close_panel)

        header = tk.Frame(self.root, bg=PANEL)
        header.pack(fill="x", padx=20, pady=(12, 8))
        tk.Label(header, text="MODERN LAB", bg=PANEL, fg=INK,
                 font=("Segoe UI", 19, "bold")).pack(side="left")
        tk.Label(header, text="  /  DIGITAL STORAGE OSCILLOSCOPE", bg=PANEL,
                 fg="#646c6c", font=("Segoe UI", 9)).pack(side="left", padx=8)
        self.conn_type = tk.StringVar(value="lan")
        self.device_info = tk.StringVar(value="Not connected")
        ttk.Combobox(header, textvariable=self.conn_type, values=("lan", "usb"),
                     state="readonly", width=5).pack(side="left", padx=(20, 5))
        self.conn_address = ttk.Entry(header, width=16)
        self.conn_address.insert(0, "10.1.1.131")
        self.conn_address.pack(side="left", padx=5)
        self.connect_btn = ttk.Button(header, text="Connect", command=lambda: self.action(self.toggle_connection))
        self.connect_btn.pack(side="left", padx=5)

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
                self._button_images[width] = ImageTk.PhotoImage(
                    Image.open(ASSETS / "button.png").convert("RGBA").resize(
                        (width, 40), Image.Resampling.LANCZOS))
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

    def numeric(self, parent, name, command, size=48):
        var = tk.StringVar(value="0")
        self._vars[name] = var
        row = ttk.Frame(parent)
        row.pack(pady=1)
        knob = Rotary(row, var, lambda d: self.step_number(name, d, command), size=size)
        knob.pack(side="left", padx=(0, 3))
        entry = ttk.Entry(row, textvariable=var, width=7, justify="center")
        setattr(self, name, entry)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self.action(command))
        ttk.Button(row, text="Set", width=3, command=lambda: self.action(command)).pack(side="left", padx=2)

    def step_number(self, name, direction, command):
        if not self.ready():
            return
        try:
            value = float(self._vars[name].get()) + direction
        except ValueError:
            self.status_var.set("Enter a numeric value before turning this knob")
            return
        self._vars[name].set(str(int(value)) if value.is_integer() else str(value))
        command()

    def build_horizontal(self, parent):
        self.encoder(parent, "timebase_scale", self.scope.TIMEBASE_SCALES, "1ms", self.set_timebase)
        ttk.Label(parent, text="TIME / DIV", font=("Segoe UI", 9, "bold")).pack(pady=(1, 4))
        self.numeric(parent, "timebase_offset", self.set_timebase_offset)
        ttk.Label(parent, text="Position (px)").pack()

    def build_trigger(self, parent):
        self.numeric(parent, "trigger_level", self.set_trigger_level, size=64)
        ttk.Label(parent, text="LEVEL (px)", font=("Segoe UI", 9, "bold")).pack(pady=(0, 5))
        for label, name, values, default, command in (
            ("Mode", "trigger_mode", self.scope.TRIGGER_MODES, "AUTO", self.set_trigger_mode),
            ("Source", "trigger_source", self.scope.TRIGGER_SOURCES, "CH1", self.set_trigger_source),
            ("Slope", "trigger_slope", self.scope.TRIGGER_SLOPES, "RISE", self.set_trigger_slope)):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=6).pack(side="left")
            self.selector(row, name, values, default, command, width=7).pack(side="right")

    def build_channels(self, parent):
        for ch, color in ((1, YELLOW), (2, CYAN)):
            col = tk.Frame(parent, bg=PANEL)
            col.pack(side="left", expand=True, fill="both", padx=5)
            state = tk.BooleanVar(value=(ch == 1))
            setattr(self, f"ch{ch}_display", state)
            key = tk.Checkbutton(col, text=f"CH{ch}", variable=state, indicatoron=False,
                                bg="#bfc3bd", selectcolor=color, activebackground=color,
                                font=("Segoe UI", 11, "bold"), bd=2, relief="raised",
                                command=lambda c=ch, v=state: self.toggle_channel(c, v), pady=5)
            key.pack(fill="x", pady=(0, 3))
            command = lambda c=ch: self.set_channel_scale(c, getattr(self, f"ch{c}_scale").get())
            self.encoder(col, f"ch{ch}_scale", self.scope.VOLTAGE_SCALES, "1v", command, size=68)
            ttk.Label(col, text="VOLTS / DIV", font=("Segoe UI", 9, "bold")).pack()
            self.numeric(col, f"ch{ch}_offset",
                         lambda c=ch: self.set_channel_offset(c, getattr(self, f"ch{c}_offset").get()))
            ttk.Label(col, text="Position").pack()
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
        for ch, color in ((1, YELLOW), (2, CYAN)):
            tk.Label(readouts, text=f"CH{ch}", fg=INK, bg=color,
                     font=("Consolas", 11, "bold"), padx=6).pack(side="left", padx=(12, 4))
            text = tk.StringVar(value="— /div")
            setattr(self, f"ch{ch}_readout", text)
            tk.Label(readouts, textvariable=text, bg=SCREEN, fg=color,
                     font=("Consolas", 10)).pack(side="left", padx=(0, 12))
        keys = tk.Frame(parent, bg="#303638")
        self._display_keys = keys
        keys.pack(fill="x", pady=(5, 0))
        for label, command in (
            ("MEASURE", lambda: self.show_drawer(0)),
            ("ACQUIRE", lambda: self.show_drawer(1)),
            ("ZOOM +", self.zoom_in), ("ZOOM −", self.zoom_out),
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
        self.ax.set_xlabel("Time (ms)", color="#94a5a5", fontsize=9)
        self.ax.set_ylabel("Voltage (V)", color="#94a5a5", fontsize=9)
        if empty:
            self.ax.set_xlim(0, 10)
            self.ax.set_ylim(-4, 4)
            self.ax.text(.5, .5, "READY TO ACQUIRE",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color="#748886", fontsize=13, fontweight="bold")
            self.ax.text(.5, .44, "Connect your scope, then press CAPTURE",
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
        self.measurement_text = self.text_area(measure)
        for label, name, values, default, command in (
            ("Type", "acq_type", self.scope.ACQ_TYPES, "SAMPle", self.set_acquire_type),
            ("Averages", "acq_average", self.scope.AVG_COUNTS, "4", self.set_acquire_average),
            ("Memory", "mem_depth", self.scope.MEMORY_DEPTHS, "10K", self.set_memory_depth)):
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
            self.download_waveform()

    def download_waveform(self):
        if not self.ready():
            return
        if self._live_timer:
            self.root.after_cancel(self._live_timer)
            self._live_timer = None
        self._busy = True
        self.capture_state.set("ACQUIRING…")
        self.status_var.set("Downloading waveform…")
        def worker():
            try:
                self._results.put((self.scope.download_waveform_data(), None))
            except Exception as exc:
                self._results.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

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
                self.plot_waveform()
                self.capture_state.set("LIVE • 2 s" if self.auto_refresh_var.get() else "CAPTURED")
                self.status_var.set("Waveform acquired")
            else:
                self.capture_state.set("ACQUISITION FAILED")
                self.status_var.set(error or "Waveform download failed")
                self.log(error or "Waveform download failed", "ERROR")
                if self.auto_refresh_var.get():
                    self.toggle_live()
                self.capture_state.set("ACQUISITION FAILED")
            if self.auto_refresh_var.get():
                self._live_timer = self.root.after(2000, self.download_waveform)
        if not self._closing or self._busy:
            self._poll_timer = self.root.after(80, self.poll_capture)

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
        self.scope.disconnect()
        self.root.destroy()
