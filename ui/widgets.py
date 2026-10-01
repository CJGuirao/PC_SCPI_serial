"""Standalone widgets and the palette they draw in.

Nothing here knows what an instrument is: these are the pieces the panel
composes, and the drawing helpers it needs to build them.
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

PANEL = "#d5d4cf"
INK = "#252a2c"
SCREEN = "#101719"
YELLOW = "#ffe33e"
CYAN = "#26d7e8"
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "modern_lab"

#: Gap between the end of one live capture and the start of the next. A screen
#: capture costs about 0.5 s on this instrument even with the header reused -
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


#: What the state indicator says, and the colour it says it in: running, a single
#: capture in hand, or nothing being acquired.
LIVE_GREEN = "#63d471"
PAUSE_RED = "#ff6b6b"
HOLD_AMBER = "#f3bd75"


def transport_icon(kind, colour, size=16, scale=4):
    """Draw a play, pause or refresh glyph as an image.

    Drawn rather than typed: whether a font carries a pause bar, and whether the
    window manager substitutes something else for it, is not something the state of
    an acquisition should depend on. Drawn large and reduced, so the edges are
    clean at the size it is shown.
    """
    box = size * scale
    mid = box / 2.0
    image = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if kind == "play":
        draw.polygon([(mid - 0.30 * box, mid - 0.44 * box),
                      (mid + 0.44 * box, mid),
                      (mid - 0.30 * box, mid + 0.44 * box)], fill=colour)
    elif kind == "pause":
        bar = 0.21 * box
        draw.rectangle([mid - 0.42 * box, mid - 0.44 * box,
                        mid - 0.42 * box + bar, mid + 0.44 * box], fill=colour)
        draw.rectangle([mid + 0.21 * box, mid - 0.44 * box,
                        mid + 0.21 * box + bar, mid + 0.44 * box], fill=colour)
    else:                                                   # refresh
        radius = 0.38 * box
        draw.arc([mid - radius, mid - radius, mid + radius, mid + radius],
                 start=35, end=325, fill=colour, width=max(2, int(0.15 * box)))
        head = 0.17 * box
        draw.polygon([(mid + 0.30 * box, mid - radius - head),
                      (mid + 0.30 * box + head * 1.7, mid - radius + head * 0.5),
                      (mid + 0.04 * box, mid - radius + head * 0.9)], fill=colour)
    return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))


class Tooltip:
    """Show a small pop-up label after the pointer hovers over a widget.

    Usage:
        Tooltip(widget, "What this button does")

    The tip appears after ``delay`` ms and disappears when the pointer
    leaves the widget or the widget is destroyed.
    """

    def __init__(self, widget, text, delay=1200):
        self._widget = widget
        self._text = text
        self._delay = delay
        self._id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel, add="+")
        widget.bind("<ButtonPress>", self._cancel, add="+")
        widget.bind("<Destroy>", self._cancel, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._id = self._widget.after(self._delay, self._show)

    def _cancel(self, _event=None):
        if self._id:
            self._widget.after_cancel(self._id)
            self._id = None
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _show(self):
        x = self._widget.winfo_rootx() + self._widget.winfo_width() // 2
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry("+%d+%d" % (x, y))
        lbl = tk.Label(
            self._tip, text=self._text, justify="left",
            background="#ffffcc", foreground="#333333",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 9), wraplength=280, padx=6, pady=4,
        )
        lbl.pack()

