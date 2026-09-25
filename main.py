import json
import logging
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from modern_lab import ModernLabUI
from owon_controller import OWONScopeController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


class App(ModernLabUI):
    """Modern front panel for OWON HDS200/HDS300 scopes."""

    def __init__(self, root):
        self.root = root
        self.root.title("OWON HDS SCPI Oscilloscope Control")
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
        self._clock_running = True

        self.setup_gui()
        self.capture_state.set("NO ACQUISITION")
        self.status_var.set("Ready | Disconnected")
        self.update_time()

    # ------------------------------------------------------------------
    # UI helpers
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

    # ------------------------------------------------------------------
    # Connection handling
    def toggle_connection(self):
        if self._is_connected():
            self.disconnect_scope()
        else:
            self.connect_scope()

    def connect_scope(self):
        address = self.conn_address.get().strip()
        conn_type = self.conn_type.get().strip().lower()
        port_text = getattr(self, "conn_port", None)
        port_value = port_text.get().strip() if port_text is not None else ""

        try:
            if conn_type == "usb":
                baudrate = int(port_value) if port_value else 115200
                usb_target = address if address.lower().startswith(("com", "/dev", "usb", "auto")) else "auto"
                success = self.scope.connect_usb(usb_target or "auto", baudrate)
            else:
                port = int(port_value) if port_value else 3000
                success = self.scope.connect_lan(address or "10.1.1.131", port)
        except ValueError:
            messagebox.showerror("Connection Error", "Invalid port or baud rate")
            return

        if not success:
            messagebox.showerror("Connection Error", f"Failed to connect via {conn_type.upper()}")
            self._set_status("Connection failed")
            return

        self.connect_btn.config(text="Disconnect")
        idn = self.scope.get_idn()
        self.device_info.set(idn or f"Connected via {conn_type.upper()}")
        self._set_status(f"Connected via {conn_type.upper()}")
        self.query_all_states()

    def disconnect_scope(self):
        self.scope.disconnect()
        self.connect_btn.config(text="Connect")
        self.device_info.set("Not Connected")
        self.status_var.set("Ready | Disconnected")
        self.capture_state.set("NO ACQUISITION")
        self.log("Disconnected")

    # ------------------------------------------------------------------
    # Instrument commands
    def set_channel_display(self, channel, state):
        if self._is_connected():
            self.scope.set_channel_display(channel, state)
            self.log(f"CH{channel} display -> {'ON' if state else 'OFF'}")

    def set_channel_scale(self, channel, scale):
        if self._is_connected():
            self.scope.set_channel_scale(channel, scale)
            self.log(f"CH{channel} scale -> {scale}")

    def set_channel_coupling(self, channel, coupling):
        if self._is_connected():
            self.scope.set_channel_coupling(channel, coupling)
            self.log(f"CH{channel} coupling -> {coupling}")

    def set_channel_probe(self, channel, probe):
        if self._is_connected():
            self.scope.set_channel_probe(channel, probe)
            self.log(f"CH{channel} probe -> {probe}")

    def set_channel_offset(self, channel, offset):
        if self._is_connected():
            self.scope.set_channel_offset(channel, offset)
            self.log(f"CH{channel} offset -> {offset}")

    def set_timebase(self):
        if self._is_connected():
            self.scope.set_timebase_scale(self.timebase_scale.get())
            self.log(f"Timebase -> {self.timebase_scale.get()}")

    def set_timebase_offset(self):
        if self._is_connected():
            self.scope.set_timebase_offset(self.timebase_offset.get())
            self.log(f"Timebase offset -> {self.timebase_offset.get()}")

    def set_trigger_mode(self):
        if self._is_connected():
            self.scope.set_trigger_mode(self.trigger_mode.get())
            self.log(f"Trigger mode -> {self.trigger_mode.get()}")

    def set_trigger_source(self):
        if self._is_connected():
            handler = getattr(self.scope, "set_edge_trigger_source", self.scope.set_trigger_source)
            handler(self.trigger_source.get())
            self.log(f"Trigger source -> {self.trigger_source.get()}")

    def set_trigger_slope(self):
        if self._is_connected():
            handler = getattr(self.scope, "set_edge_trigger_slope", self.scope.set_trigger_slope)
            handler(self.trigger_slope.get())
            self.log(f"Trigger slope -> {self.trigger_slope.get()}")

    def set_trigger_level(self):
        if self._is_connected():
            value = self.trigger_level.get()
            try:
                numeric = float(value)
                value = int(numeric) if numeric.is_integer() else numeric
            except Exception:
                pass
            handler = getattr(self.scope, "set_edge_trigger_level", self.scope.set_trigger_level)
            handler(value)
            self.log(f"Trigger level -> {self.trigger_level.get()}")

    def set_acquire_type(self):
        if self._is_connected():
            self.scope.set_acquire_type(self.acq_type.get())
            self.log(f"Acquire type -> {self.acq_type.get()}")

    def set_acquire_average(self):
        if self._is_connected():
            self.scope.set_acquire_average(self.acq_average.get())
            self.log(f"Average count -> {self.acq_average.get()}")

    def set_memory_depth(self):
        if self._is_connected():
            self.scope.set_memory_depth(self.mem_depth.get())
            self.log(f"Memory depth -> {self.mem_depth.get()}")

    def reset_scope(self):
        if not self._is_connected():
            return
        if messagebox.askyesno("Reset scope", "Reset oscilloscope to defaults?"):
            self.scope.reset()
            time.sleep(0.2)
            self.query_all_states()
            self.log("Scope reset")

    def single_trigger(self):
        if self._is_connected():
            self.scope.single_trigger()
            self.log("Single trigger armed")

    def auto_scale(self):
        if self._is_connected():
            self.scope.auto_scale(True)
            self.log("Auto scale enabled")

    def zoom_in(self):
        if not self.ax.lines:
            return
        xmin, xmax = self.ax.get_xlim()
        xmid = (xmin + xmax) / 2.0
        span = (xmax - xmin) * 0.5
        self.ax.set_xlim(xmid - span / 2.0, xmid + span / 2.0)
        self.canvas.draw_idle()

    def zoom_out(self):
        if not self.ax.lines:
            return
        xmin, xmax = self.ax.get_xlim()
        xmid = (xmin + xmax) / 2.0
        span = (xmax - xmin) * 2.0
        self.ax.set_xlim(xmid - span / 2.0, xmid + span / 2.0)
        self.canvas.draw_idle()

    def clear_plot(self):
        self.ax.clear()
        self.ax.set_facecolor("#101719")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Voltage")
        self.ax.grid(True, alpha=0.25, color="#526060", linestyle=":")
        self.canvas.draw_idle()
        self._plot_cache = []

    # ------------------------------------------------------------------
    # Query / visualization
    def query_all_states(self):
        if not self._is_connected():
            return

        try:
            tb_scale = self.scope.get_timebase_scale()
            if tb_scale and tb_scale in self.scope.TIMEBASE_SCALES:
                self.timebase_scale.set(tb_scale)
            tb_offset = self.scope.get_timebase_offset()
            if tb_offset not in (None, ""):
                self.timebase_offset.delete(0, tk.END)
                self.timebase_offset.insert(0, str(tb_offset))

            for ch in (1, 2):
                scale = self.scope.get_channel_scale(ch)
                coupling = self.scope.get_channel_coupling(ch)
                display = self.scope.get_channel_display(ch)
                offset = self.scope.get_channel_offset(ch)
                if scale and scale.lower() in self.scope.VOLTAGE_SCALES:
                    getattr(self, f"ch{ch}_scale").set(scale.lower())
                if coupling and coupling in self.scope.COUPLING_MODES:
                    getattr(self, f"ch{ch}_coupling").set(coupling)
                if display is not None:
                    getattr(self, f"ch{ch}_display").set(str(display).strip().upper() == "ON")
                if offset not in (None, ""):
                    entry = getattr(self, f"ch{ch}_offset")
                    entry.delete(0, tk.END)
                    entry.insert(0, str(offset))

            trig_mode = self.scope.get_trigger_mode()
            if trig_mode and trig_mode in self.scope.TRIGGER_MODES:
                self.trigger_mode.set(trig_mode)
            trig_source = self.scope.get_trigger_source()
            if trig_source and trig_source in self.scope.TRIGGER_SOURCES:
                self.trigger_source.set(trig_source)
            trig_slope = self.scope.get_trigger_slope()
            if trig_slope and trig_slope in self.scope.TRIGGER_SLOPES:
                self.trigger_slope.set(trig_slope)
            trig_level = self.scope.get_trigger_level()
            if trig_level not in (None, ""):
                self.trigger_level.delete(0, tk.END)
                self.trigger_level.insert(0, str(trig_level))

            acq_type = self.scope.get_acquire_type()
            if acq_type and acq_type in self.scope.ACQ_TYPES:
                self.acq_type.set(acq_type)
            avg = self.scope.get_acquire_average()
            if avg and str(avg) in self.scope.AVG_COUNTS:
                self.acq_average.set(str(avg))
            memory = self.scope.get_memory_depth()
            if memory and str(memory) in self.scope.MEMORY_DEPTHS:
                self.mem_depth.set(str(memory))
            self.log("Settings refreshed")
        except Exception as exc:
            self.log(f"Query settings failed: {exc}", "ERROR")

    def get_measurements(self):
        if not self._is_connected():
            return
        channel = 1
        try:
            selected = self.meas_source.get().strip().upper()
            if selected.startswith("CH"):
                channel = int(selected[2:])
        except Exception:
            channel = 1

        payload = self.scope.get_all_measurements(channel)
        if isinstance(payload, dict) and payload:
            lines = [f"{key}: {value}" for key, value in sorted(payload.items())]
        else:
            lines = [str(payload)]
        text = "\n".join(lines)
        self.measurement_text.delete("1.0", tk.END)
        self.measurement_text.insert(tk.END, text + "\n")
        self.measurement_text.see(tk.END)
        self.log(f"Measurements read from CH{channel}")

    def plot_waveform(self):
        channels = getattr(self.scope.waveform_data, "channels", []) or []
        self.ax.clear()
        self.ax.set_facecolor("#101719")
        self.ax.grid(True, alpha=0.25, color="#526060", linestyle=":")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Voltage")

        palette = {
            "CH1": "#ffe33e",
            "CH2": "#26d7e8",
            "CH3": "#ff9c5b",
            "CH4": "#c792ea",
        }
        plotted = False
        for channel in channels:
            name = str(channel.get("name", "CH1")).upper()
            color = palette.get(name, "#9cdc9c")
            y = np.asarray(channel.get("waveform_data", []), dtype=float)
            if y.size == 0:
                continue
            x = np.arange(y.size, dtype=float) * float(channel.get("point_interval", 1.0) or 1.0)
            self.ax.plot(x, y, color=color, linewidth=1.4)
            plotted = True

        if not plotted:
            self.ax.text(0.5, 0.5, "No waveform data", transform=self.ax.transAxes,
                         ha="center", va="center", color="#94a5a5")
        else:
            self.ax.relim()
            self.ax.autoscale_view()
        self.canvas.draw_idle()
        self._plot_cache = channels

    def save_waveform(self):
        channels = getattr(self.scope.waveform_data, "channels", []) or []
        if not channels:
            messagebox.showinfo("Save waveform", "No waveform data to save")
            return
        path = filedialog.asksaveasfilename(
            title="Save waveform",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if suffix == ".json":
            payload = {
                "header": self.scope.waveform_data.header,
                "channels": channels,
            }
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        else:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write("channel,index,value\n")
                for channel in channels:
                    name = channel.get("name", "CH1")
                    for index, value in enumerate(channel.get("waveform_data", [])):
                        fh.write(f"{name},{index},{value}\n")
        self.log(f"Waveform saved to {path}")
        messagebox.showinfo("Save waveform", f"Waveform saved to:\n{path}")

    # ------------------------------------------------------------------
    # Shutdown
    def close_panel(self):
        self._clock_running = False
        if self._clock_timer:
            try:
                self.root.after_cancel(self._clock_timer)
            except Exception:
                pass
        super().close_panel()


def main():
    root = tk.Tk()
    app = App(root)
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.mainloop()


if __name__ == "__main__":
    main()
