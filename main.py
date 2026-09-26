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

from modern_lab import ModernLabUI
from owon_controller import OWONScopeController
from scope_setup import ScopeSetup, attached_scopes
from waveform_data import HDS_HORIZONTAL_DIVISIONS, HDS_VERTICAL_DIVISIONS, WaveformData

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
        self._display_scale_is_auto = {}
        #: The probe convention the trace is SHOWN in, per channel. Unset means the
        #: instrument's own, and the trace is then drawn in tip volts.
        self._display_probe = {}

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
    def sync_connection_fields(self):
        """Show the field the selected transport actually uses.

        USB finds the instrument by VID/PID, so there is nothing to type: the
        address box is removed and replaced by a plain hint. Choosing LAN brings
        the address back in the same place, before the Connect button.
        """
        for widget in (self.conn_address, self.conn_hint):
            widget.pack_forget()
        if self.conn_type.get().strip().lower() == "usb":
            self.conn_hint.pack(side="left", padx=5, before=self.connect_btn)
        else:
            self.conn_address.pack(side="left", padx=5, before=self.connect_btn)

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
                # The address box is not on screen for USB, and the instrument is
                # found by VID/PID (HDS) or auto-detected as a COM port (serial
                # models), so nothing typed for LAN can steer this. Which of several
                # attached scopes to open IS steerable, and that is the serial from
                # the setup - worth having, because the HID endpoint takes one owner
                # at a time and opening the wrong instrument looks like no scope.
                chosen = self.setup.usb_serial
                attached = attached_scopes()
                if len(attached) > 1:
                    self.log("%d OWON USB devices attached; using %s. Pick one in SETUP."
                             % (len(attached), chosen or "the first found"))
                if chosen and getattr(self.scope, "is_hds", False):
                    success = self.scope.connect_usb_hid(serial=chosen)
                    if success:
                        self.scope.identify_model()
                else:
                    success = self.scope.connect_usb("auto", baudrate)
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
        # Hide channels this instrument does not have, and prime the cursors.
        try:
            self.scope.forget_channel_count()
            self.apply_channel_count()
        except Exception as exc:
            self.log(f"Channel probe failed: {exc}", "ERROR")
        self.refresh_cursors()
        self.query_all_states()
        self.sync_dmm()
        self.report_trigger_state()

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
        """Set the volts one row of the grid is worth - the software's display scale.

        Not a write to the instrument, and the measurement is why. With a 25 Vpp
        signal on screen: writing 10 V/div left the code span at 209 codes where a
        real gain change would have halved it to ~97, and writing 2 V/div left it at
        121 where a real change would have saturated it - while the instrument's own
        reading collapsed from 25.6 V to 17.76 V on an input that never moved. The
        write tells the instrument its label changed, not its gain, and it degrades
        what the instrument then reports.

        So the gain is set on the instrument's own keys, and this control is the
        software's display scale: 5 V/div means a row of the grid is 5 V, so a
        25.6 Vpp signal is drawn 5.1 rows tall. The amplitude comes from the capture,
        so it does not move with the control - only the row and the height do.
        """
        if not self._is_connected():
            return
        self._display_scale[channel] = scale
        self._display_scale_is_auto[channel] = False
        self.log("CH%d display scale: %s/div. One row of the grid is now that many "
                 "volts at the input; the amplitude still comes from the capture, so "
                 "it does not change with the control." % (channel, scale))
        self.plot_waveform()

    def set_channel_coupling(self, channel, coupling):
        if self._is_connected():
            self.scope.set_channel_coupling(channel, coupling)
            self.log(f"CH{channel} coupling -> {coupling}")

    def set_channel_probe(self, channel, probe):
        """Record the probe in use. It does not scale this instrument's amplitudes.

        Worth knowing, and worth not "fixing": with a 1X probe on the input the
        instrument announced 10X and still read the 25 Vpp signal as 25.6 V, so its
        readings are the volts at the BNC whatever the label says. Scaling by the
        label would put every amplitude out by ten - which is one of the ways this
        panel has read the wrong number before.
        """
        if not self._is_connected():
            return
        self._display_probe[channel] = probe
        self.log("CH%d probe recorded as %s. The amplitudes do not change with it: "
                 "this instrument reads real volts at the BNC (a 1X-fed 25 V signal "
                 "read 25.6 V while it announced 10X), and the label does not scale "
                 "them." % (channel, probe))
        self.plot_waveform()

    def set_channel_offset(self, channel, offset):
        if self._is_connected():
            self._framing_is_front_panel_only(channel, "position", offset)
            self.sync_vertical_controls()

    def set_timebase(self):
        if self._is_connected():
            self.scope.set_timebase_scale(self.timebase_scale.get())
            self.log(f"Timebase -> {self.timebase_scale.get()}")

    def set_timebase_offset(self):
        if self._is_connected():
            self.scope.set_timebase_offset(self.timebase_offset.get())
            self.log(f"Timebase offset -> {self.timebase_offset.get()}")

    @staticmethod
    def timebase_position_text(seconds):
        """Seconds spelled the way the instrument accepts a time: '1.5ms', '-500us'."""
        if not seconds:
            return "0"
        magnitude = abs(seconds)
        for factor, suffix in ((1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
            if magnitude >= factor:
                return "%g%s" % (seconds / factor, suffix)
        return "%gs" % seconds

    def set_timebase_position(self):
        """Apply the horizontal position, which the knob enters in divisions.

        Divisions are what a scope front panel uses, and they avoid the entry box
        showing a raw second figure like 0.0005. The conversion needs the current
        time/div, so the position is recomputed whenever the timebase changes.
        """
        if not self._is_connected():
            return
        try:
            divisions = float(self.timebase_offset.get())
        except ValueError:
            self.status_var.set("Horizontal position must be a number of divisions")
            return
        scale = self.scope.parse_scale(self.scope.get_timebase_scale() or "")
        if not scale:
            return
        seconds = divisions * scale
        self._horizontal_position = seconds
        self.scope.set_timebase_offset(self.timebase_position_text(seconds))
        self.log("Horizontal position -> %g div (%s)" % (divisions, self.timebase_position_text(seconds)))
        if self.scope.waveform_data.channels:
            self.plot_waveform()

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
                self._trigger_level_v = float(numeric)
            except Exception:
                pass
            handler = getattr(self.scope, "set_edge_trigger_level", self.scope.set_trigger_level)
            handler(value)
            self.log(f"Trigger level -> {self.trigger_level.get()}")
            if self.scope.waveform_data.channels:
                self.plot_waveform()

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

    # ------------------------------------------------------------------
    # Multimeter
    # The HDS271 has a DMM beside the scope, on its own subsystem: :DMM:MEAS?
    # reads it, :DMM:REL and :DMM:CONFigure set it up. It answered for voltage,
    # current and REL and was silent for resistance, diode, continuity and
    # capacitance, so the panel offers what the instrument has rather than what
    # the manual lists for the whole series.
    DMM_FUNCTIONS = {"V": "VOLTage", "VOLT": "VOLTage", "VOLTAGE": "VOLTage",
                     "A": "CURRent", "AMP": "CURRent", "CURRENT": "CURRent"}
    DMM_UNITS = {"VOLTage": "V", "CURRent": "A"}

    def dmm_caption(self, function, subtype):
        """The unit and type shown after a reading, e.g. 'V DC'."""
        return ("%s %s" % (self.DMM_UNITS.get(function or "", ""), subtype or "")).strip()

    def sync_dmm(self):
        """Read the multimeter's function and relative state from the instrument."""
        if not self._is_connected():
            return
        report = {}
        try:
            report = self.scope.dmm_capabilities(refresh=True) or {}
        except Exception as exc:
            self.log(f"Multimeter probe failed: {exc}", "ERROR")
        function, subtype = None, None
        try:
            function, subtype = self.scope.get_dmm_function()
        except Exception as exc:
            self.log(f"Multimeter state failed: {exc}", "ERROR")
        self._dmm_caption = self.dmm_caption(function, subtype)
        if function:
            self.dmm_function.set("VOLT" if function == "VOLTage" else "AMP")
            self.dmm_type.set(subtype or self.dmm_type.get())
        try:
            state = self.scope.get_dmm_relative()
            self.dmm_relative.set(state is not False and state is not None)
        except Exception as exc:
            self.log(f"Multimeter relative state failed: {exc}", "ERROR")
        silent = report.get("silent") if isinstance(report, dict) else None
        if report and not report.get("reading"):
            self.dmm_note.set("this instrument did not answer the multimeter")
        elif not function:
            self.dmm_note.set("no multimeter function reported")
        else:
            self.dmm_note.set("")
        self.log("Multimeter: %s%s" % (function or "not reported",
                                       (" " + subtype) if subtype else ""))
        if silent:
            self.log("Multimeter has no %s on this instrument." % ", ".join(silent).lower())

    def set_dmm_function(self):
        """Apply the function and type the user picked, and say what the instrument did.

        Voltage/current switching is accepted but not applied on this firmware,
        the way the vertical scale is, so the log reports what the instrument
        says afterwards instead of claiming the change landed.
        """
        if not self._is_connected():
            return
        wanted = self.dmm_function.get()
        subtype = self.dmm_type.get()
        node = self.DMM_FUNCTIONS.get(wanted.strip().upper(), wanted)
        try:
            function, confirmed = self.scope.set_dmm_function(node, subtype)
        except Exception as exc:
            self.log(f"Multimeter function failed: {exc}", "ERROR")
            return
        self._dmm_caption = self.dmm_caption(function, confirmed)
        landed = (function and str(function).lower() == node.lower()
                  and (not subtype or (confirmed or "").upper() == str(subtype).upper()))
        if landed:
            self.dmm_note.set("")
            self.log(f"Multimeter -> {wanted} {subtype}")
        else:
            got = ("%s %s" % (function or "nothing", confirmed or "")).strip()
            self.dmm_note.set(f"asked for {wanted} {subtype}, instrument reports {got}")
            self.log(f"Multimeter function was written as {wanted} {subtype} but the "
                     f"instrument reports {got}.", "WARNING")

    def toggle_dmm_relative(self):
        """Turn relative mode on or off; ``:DMM:REL?`` answers OFF or the offset."""
        if not self._is_connected():
            self.dmm_relative.set(False)
            return
        wanted = not self.dmm_relative.get()
        try:
            state = self.scope.set_dmm_relative(wanted)
        except Exception as exc:
            self.log(f"Multimeter relative failed: {exc}", "ERROR")
            return
        # ``state`` is the stored offset - 0.0 is a real reading, not "off".
        engaged = state is not False and state is not None
        self.dmm_relative.set(engaged)
        if wanted and not engaged:
            self.dmm_note.set("REL did not engage")
            self.log("Multimeter REL was written as ON but the instrument reads back OFF.",
                     "WARNING")
        else:
            self.dmm_note.set("")
            self.log("Multimeter REL -> %s" % ("%g V" % state if engaged else "off"))

    def poll_dmm(self):
        """Refresh the multimeter readout; one query is ~32 ms."""
        if not self._is_connected():
            self.dmm_value.set("—")
            return
        try:
            reading = self.scope.get_dmm_reading()
        except Exception as exc:
            self.dmm_value.set("—")
            self.log(f"Multimeter read failed: {exc}", "ERROR")
            return
        if not isinstance(reading, (int, float)) or isinstance(reading, bool):
            self.dmm_value.set("—")
            return
        prefix = "REL " if self.dmm_relative.get() else ""
        self.dmm_value.set(("%s%.4f %s" % (prefix, reading, self._dmm_caption)).strip())

    def report_trigger_state(self):
        """Log what the trigger is doing; neither query captures a frame."""
        if not self._is_connected():
            return
        try:
            status = self.scope.get_trigger_status()
            sweep = self.scope.get_trigger_sweep()
        except Exception as exc:
            self.log(f"Trigger state failed: {exc}", "ERROR")
            return
        if status or sweep:
            self.log("Trigger: %s (%s)" % (status or "unknown", sweep or "unknown"))

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

    def last_capture_amplitude(self):
        """The peak-to-peak of the last capture as the app shows it, in volts.

        This is the figure a calibration is derived against, so it is read the way
        the panel reads it: the channel it is already drawing, with the trim in
        force included.
        """
        for entry in getattr(self.scope.waveform_data, "channels", []) or []:
            volts = entry.get("waveform_data") or []
            if len(volts) > 1:
                return float(max(volts) - min(volts))
        return None

    def open_setup(self):
        """Show the bench setup: which scope, and the numbers that calibrate it."""
        from modern_lab import SetupDialog
        if getattr(self, "_setup_window", None) is not None:
            try:
                self._setup_window.lift()
                return
            except tk.TclError:
                self._setup_window = None
        devices = attached_scopes()
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

    def auto_scale(self):
        if self._is_connected():
            self.scope.auto_scale(True)
            self.log("Auto scale enabled")

    def auto_frame(self):
        """Front panel AUTO: search for the signal and frame it.

        The instrument exposes no autoset command over SCPI, so the framing is
        computed from the instrument's own measurements; see
        OWONScopeController.auto_frame for what is verifiable on this firmware.
        """
        if not self.ready():
            return
        self._busy = True
        self._auto_report = None
        self.capture_state.set("AUTO…")
        self.status_var.set("Searching for the signal and framing it…")

        def worker():
            try:
                self._auto_report = self.scope.auto_frame()
                self._results.put((True, None))
            except Exception as exc:
                self._auto_report = None
                self._results.put((False, str(exc)))

        self.start_worker(worker, "AUTO")

    def report_auto_frame(self):
        """Log what AUTO actually changed, and anything it could not."""
        report = getattr(self, "_auto_report", None)
        self._auto_report = None
        if not report:
            return
        applied = []
        if "timebase" in (report.get("applied") or []):
            applied.append("time/div %s" % report["timebase"])
        if "trigger level" in (report.get("applied") or []):
            applied.append("trigger %s" % report["trigger_level"])
        if applied:
            self.log("AUTO set " + ", ".join(applied))
        else:
            self.log("AUTO could not change anything", "WARNING")
        # Follow only the changes the instrument confirmed. Writing an
        # unconfirmed readback into the live trigger box would have the panel
        # send that value straight back - and a readback like "4293V" is exactly
        # the sort of thing this firmware returns.
        if "timebase" in (report.get("applied") or []):
            self.timebase_scale.set(report["timebase"])
        if "trigger level" in (report.get("applied") or []):
            level = self.scope.parse_scale(report.get("trigger_level_readback"))
            if level is not None:
                self.trigger_level.delete(0, tk.END)
                self.trigger_level.insert(0, "%g" % level)
        for note in report.get("notes") or []:
            self.log("AUTO: " + note)

    # ZOOM +/- used to live here. They moved the axes directly, and the live loop
    # re-frames the plot from every capture, so a zoom either survived a fraction of
    # a second or fought the framing: either way the view jumped. Until there is a
    # zoom that lives in the framing itself, the volts/div and time/div controls
    # are the way to change what is on screen.

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
            tb_offset = self.scope.get_horizontal_position_seconds()
            tb_scale = self.scope.parse_scale(self.scope.get_timebase_scale() or "")
            if tb_offset is not None:
                self._horizontal_position = tb_offset
                divisions = tb_offset / tb_scale if tb_scale else 0.0
                self.timebase_offset.delete(0, tk.END)
                self.timebase_offset.insert(0, "%g" % divisions)

            for ch in range(1, self.scope.get_channel_count() + 1):
                scale = self.scope.get_channel_scale(ch)
                coupling = self.scope.get_channel_coupling(ch)
                display = self.scope.get_channel_display(ch)
                offset = self.scope.get_channel_offset(ch)
                probe = self.scope.get_channel_probe(ch)
                if scale and scale.lower() in self.scope.VOLTAGE_SCALES:
                    getattr(self, f"ch{ch}_scale").set(scale.lower())
                if coupling and coupling in self.scope.COUPLING_MODES:
                    getattr(self, f"ch{ch}_coupling").set(coupling)
                # Only when the instrument actually answered: the HDS does not
                # implement :CHn:DISPlay?, and treating the empty reply as "off"
                # would untick a channel that is plainly on screen.
                if display not in (None, ""):
                    getattr(self, f"ch{ch}_display").set(str(display).strip().upper() == "ON")
                # The probe turns a volts/div figure into a real amplitude, so it
                # is read back rather than left on a default the user would then
                # write over. The scope answers "10X" where the list holds "X10".
                token = str(probe or "").strip().upper()
                if token and not token.startswith("X"):
                    token = "X" + token.rstrip("X")
                if token in self.scope.PROBE_ATTEN:
                    getattr(self, f"ch{ch}_probe").set(token)
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

        lines = []
        # The capture is what the plot and the grid show, and it is calibrated
        # against a signal of known amplitude, so its figures describe the signal.
        # The instrument's own block is referred to its volts/div and probe LABELS:
        # at a fine setting it reads a fraction of the same signal (a known 5.00 Vpp
        # signal comes back as 0.888 V at 100mV/div), so the two are labelled instead
        # of being printed side by side as though they agreed.
        trace = self.scope.frame_from_trace(channel)
        if isinstance(trace, dict) and trace:
            lines.append("From the capture (calibrated - this is what the grid shows):")
            for label, key, unit in (("Vpp", "vpp", "V"), ("Vmax", "vmax", "V"),
                                     ("Vmin", "vmin", "V"), ("Vmean", "vmean", "V"),
                                     ("Frequency", "frequency", "Hz")):
                value = trace.get(key)
                if value is not None:
                    lines.append("  %s: %.4g %s" % (label, value, unit))
            lines.append("")

        payload = self.scope.get_all_measurements(channel)
        lines.append("From the instrument (referred to its own volts/div and probe labels):")
        if isinstance(payload, dict) and payload:
            lines.extend("  %s: %s" % (key, value) for key, value in sorted(payload.items()))
        else:
            lines.append("  " + str(payload))

        text = "\n".join(lines)
        self.measurement_text.delete("1.0", tk.END)
        self.measurement_text.insert(tk.END, text + "\n")
        self.measurement_text.see(tk.END)
        self.log(f"Measurements read from CH{channel}")

    @staticmethod
    def time_axis_units(span_seconds):
        """The unit that keeps time-axis numbers readable, with its factor.

        The captures come back in seconds (20us per point at 500us/div), so a
        bare "Time" axis reads as 0.000..0.006 with no unit at all.
        """
        if span_seconds and span_seconds < 1e-3:
            return 1e6, "us"
        if span_seconds and span_seconds < 1.0:
            return 1e3, "ms"
        return 1.0, "s"

    def choice_values(self, widget):
        """The choices a selector widget offers, whatever Tk hands back."""
        if widget is None:
            return []
        values = widget.cget("values")
        if isinstance(values, str):
            values = values.replace("{", "").replace("}", "").split()
        return list(values)

    def nearest_choice(self, value, choices):
        """The choice-list entry closest to a value.

        A reported figure round-trips through the probe factor, so it lands
        beside the control's own units rather than exactly on them ('500mv', not
        '0.4999997').
        """
        best, best_gap = None, None
        for choice in choices:
            parsed = OWONScopeController.parse_scale(choice)
            if parsed is None:
                continue
            gap = abs(parsed - value)
            if best_gap is None or gap < best_gap:
                best, best_gap = choice, gap
        return best

    def sync_vertical_controls(self):
        """Follow a framing change made on the instrument.

        The capture carries the probe the instrument is using and the scale it was
        decoded at, so a change made on the instrument reaches these controls
        without another query. The volts/div control shows the DECODED scale, not
        the volts/div the instrument reports, and it only moves while the user has
        not pinned a value of their own on it.
        """
        for channel in getattr(self.scope.waveform_data, "channels", []) or []:
            name = str(channel.get("name", "")).upper()
            if not name.startswith("CH") or not name[2:].isdigit():
                continue
            number = int(name[2:])
            # The panel's volts/div is the software's display scale. Left on Auto it
            # follows what the capture DECODED to, never the volts/div the instrument
            # reports: that label moves without the gain moving - a 10x change of it
            # left the volts per code at ~0.145 V - so mirroring it would put the
            # control 10x away from what the trace actually is.
            decoded = channel.get("true_volts_per_div")
            scale_widget = getattr(self, "ch%d_scale" % number, None)
            if decoded and scale_widget is not None and self._display_scale_is_auto.get(number, True):
                choices = self.choice_values(scale_widget) or list(OWONScopeController.VOLTAGE_SCALES)
                scale = self.nearest_choice(decoded, choices)
                if scale and scale_widget.get() != scale:
                    scale_widget.set(scale)
                    self.log("CH%d display scale auto: %s/div (the capture decodes to "
                             "%.4g V/div at the tip)" % (number, scale, decoded))
            probe_widget = getattr(self, "ch%d_probe" % number, None)
            if self._display_probe.get(number) is None and probe_widget is not None:
                # The instrument ANNOUNCES 10X on this bench while a 1X probe feeds it,
                # and reads the input as real volts anyway, so mirroring its label would
                # put a misleading number in the panel. X1 states the input as it is.
                for candidate in self.choice_values(probe_widget):
                    if WaveformData._probe_factor(candidate) == 1.0:
                        if probe_widget.get() != candidate:
                            probe_widget.set(candidate)
                            self.log("CH%d probe shown as %s - the instrument announces %s, "
                                     "but it reads real volts at the BNC either way, so the "
                                     "amplitudes do not depend on it"
                                     % (number, candidate, channel.get("attenuation") or "?"))
                        break

    def framing_changed(self):
        """A framing change was seen on the instrument: follow it here.

        The panel's volts/div and probe controls mirror the instrument, so they
        must show what it now reports - otherwise the control that "does nothing"
        is this one, while the time base, drawn from the instrument's own label
        every frame, appears to work.
        """
        self.apply_framing_signature(getattr(self, "_framing_signature", None))
        self.sync_vertical_controls()
        self.refresh_after_framing_change()

    def apply_framing_signature(self, signature):
        """Move the mirror controls to the framing the instrument just reported.

        The capture that will carry the same values arrives up to a frame later,
        so the controls are moved from the cheap reads instead of waiting for it -
        a control that stays put for a second after the instrument has changed
        reads as a control that does not work.
        """
        values = {name: value for name, value
                  in zip(self.scope.FRAMING_NAMES, signature or [])}
        scale_reply = values.get("scale")
        scale_widget = getattr(self, "ch1_scale", None)
        if scale_reply and scale_widget is not None:
            choices = self.choice_values(scale_widget) or list(OWONScopeController.VOLTAGE_SCALES)
            if scale_reply not in choices:
                scale_reply = self.nearest_choice(
                    WaveformData._scale_to_float(scale_reply) or 0.0, choices)
            if scale_reply and scale_widget.get() != scale_reply:
                scale_widget.set(scale_reply)

        probe_reply = WaveformData._probe_factor(values.get("probe"))
        probe_widget = getattr(self, "ch1_probe", None)
        if probe_reply and probe_widget is not None:
            for candidate in self.choice_values(probe_widget):
                if WaveformData._probe_factor(candidate) == probe_reply:
                    if probe_widget.get() != candidate:
                        probe_widget.set(candidate)
                    break

    def refresh_after_framing_change(self):
        """Recapture so the display shows the framing the instrument now has.

        A volts/div, probe or position change moves the instrument's own screen at
        once, so the software must not keep drawing the old frame - and when the
        app is not live there would be no next capture to correct it.
        """
        # The live loop re-captures on its own, and the write dropped the cached
        # header, so its next frame picks the new framing up. With refresh paused
        # there is no next frame, so ask for one.
        if self.auto_refresh_var.get():
            return
        if getattr(self.scope.waveform_data, "channels", None):
            self.download_waveform()

    @staticmethod
    def channel_number(channel):
        """The channel number a capture entry belongs to, or None."""
        name = str(channel.get("name", "")).upper()
        return int(name[2:]) if name[:2] == "CH" and name[2:].isdigit() else None

    def captured_channel(self, number):
        """The capture entry for a channel number, or an empty one."""
        for entry in getattr(self.scope.waveform_data, "channels", []) or []:
            if self.channel_number(entry) == number:
                return entry
        return {}

    def display_probe_value(self, channel, entry=None):
        """The probe factor the trace is SHOWN in: the user's choice, else the instrument's.

        The capture carries the instrument's probe, so the live query is only the
        fallback - one fewer round trip on a path that runs on every redraw.
        """
        chosen = self._display_probe.get(channel)
        if chosen is not None:
            return WaveformData._probe_factor(chosen) or 1.0
        entry = entry if entry is not None else self.captured_channel(channel)
        instrument = WaveformData._probe_factor(entry.get("attenuation"))
        if instrument:
            return instrument
        return WaveformData._probe_factor(self.scope.get_channel_probe(channel)) or 1.0

    def display_ratio(self, number, entry):
        """Displayed volts per volt read: always 1.0 on this instrument.

        The probe label does NOT scale its readings. Measured with a 1X probe on the
        input: the instrument announced 10X and read the 25 Vpp signal as 25.6 V, so
        its volts are the real volts at the BNC, and multiplying by the announced
        probe would have put every amplitude out by ten. This is here so the probe
        control has exactly one place to change its mind, and nothing else in the
        drawing code has to know.
        """
        return 1.0

    def display_scale_value(self, number, entry):
        """The volts one row of the grid is worth - straight, one division of it.

        The panel's control means exactly what it says: 5 V/div means the row is
        5 V, so a 25.6 Vpp signal is 5.1 divisions of the grid, at 10 V/div it is
        2.6, at 1 V/div it is 25.6. Nothing else is folded in - not the probe label,
        which does not scale this instrument's readings (a 1X-fed 25 V signal reads
        25.6 V while it announces 10X), and not the volts/div it reports, which moves
        without the gain moving.

        Left on Auto it uses the scale the capture was DECODED at, snapped to a
        setting the control offers, so the axis and the control name one number.
        The decode gets its volts from the instrument's own readings, so the
        amplitude does not move with this control - only the row and the height do.
        """
        chosen = self._display_scale.get(number)
        if chosen:
            value = WaveformData._scale_to_float(chosen)
            if value:
                return value
        decoded = entry.get("true_volts_per_div")
        if decoded:
            widget = getattr(self, "ch%d_scale" % number, None)
            choices = self.choice_values(widget) if widget is not None else None
            if choices:
                snapped = self.nearest_choice(decoded, choices)
                value = WaveformData._scale_to_float(snapped) if snapped else None
                if value:
                    return value
            return decoded
        return None

    def vertical_frame(self, channels):
        """The volts the trace is drawn against, in the software's own display scale.

        A row of the grid is the volts/div the panel's control is set to - or, on
        Auto, the scale the capture was decoded at, which comes from the
        instrument's own readings rather than from the volts/div it reports. The
        amplitude is decoded from the capture, so it does not move with the control:
        only the height of the trace and the value of a row do, and rows x volts-per-row
        comes back to the same signal either way.

        Returns {"low", "high", "step", "label"} or None when the capture carries
        no usable scale (raw codes, or a failed scale query), in which case the
        caller falls back to autoscaling.
        """
        spans, labels, steps = [], [], []
        for channel in channels:
            number = self.channel_number(channel)
            volts_per_div = self.display_scale_value(number, channel) if number else None
            samples = channel.get("waveform_data") or []
            if not volts_per_div or not samples or channel.get("units") != "V":
                continue
            half = (HDS_VERTICAL_DIVISIONS / 2.0) * volts_per_div
            spans.append((-half, half))
            note = ""
            claim = channel.get("volts_per_div")
            decoded = channel.get("true_volts_per_div")
            # A real disagreement means a factor, not a few percent: the decode
            # itself carries the spread of the calibration's span measurement, which
            # is around ten percent, so a 5% threshold fired on its own noise.
            if claim and decoded and abs(claim - decoded) > 0.25 * decoded:
                note += " (the instrument's label claims %.4g V/div)" % claim
            probe_in_use = self._display_probe.get(number)
            if probe_in_use:
                note += " %s" % probe_in_use
            if decoded and abs(decoded - volts_per_div) > 0.1 * volts_per_div:
                note += " (decoded %.4g V/div)" % decoded
            labels.append("%s %.4g V/div%s" % (
                str(channel.get("name", "")).upper(), volts_per_div, note))
            steps.append(volts_per_div)
        if not spans:
            return None
        return {
            "low": min(low for low, _ in spans),
            "high": max(high for _, high in spans),
            "step": min(steps),
            "label": ", ".join(labels),
        }

    def show_vertical_frame(self, framing):
        """Draw the instrument's vertical frame, gridded in its divisions."""
        self.ax.set_ylim(framing["low"], framing["high"])
        step = framing.get("step")
        if step and step > 0:
            # Ticks where the instrument draws its divisions, so the axis reads in
            # the same units as the front panel. A frame too tall for that many
            # ticks keeps the automatic ones rather than a wall of labels.
            first = int(np.ceil(framing["low"] / step))
            last = int(np.floor(framing["high"] / step))
            ticks = [k * step for k in range(first, last + 1)]
            if 1 < len(ticks) <= 13:
                self.ax.set_yticks(ticks)
        self.ax.set_ylabel("Voltage (V) — " + framing["label"])

    def show_graticule(self, framing, window, unit, factor=1.0):
        """Draw the instrument's screen: 12 columns by 8 rows of real divisions.

        A square has to mean the same thing here as on the instrument, so the
        column width is the selected time/div and the row height the selected
        volts/div (at the probe tip, as the instrument labels it). The centre
        cross of the graticule is dotted a little more visibly than the rest, the
        way the instrument draws it.
        """
        if framing:
            self.show_vertical_frame(framing)
            low, high = framing["low"], framing["high"]
        else:
            # No usable scale (raw codes, or a failed scale query): show the data
            # rather than inventing a frame for it.
            self.ax.relim()
            self.ax.autoscale_view()
            low, high = self.ax.get_ylim()

        if window and window > 0:
            stride = window / HDS_HORIZONTAL_DIVISIONS
            self.ax.set_xlim(0.0, window)
            self.ax.set_xticks([k * stride for k in range(int(HDS_HORIZONTAL_DIVISIONS) + 1)])
            # The division is named in its own unit - "200 us/div", not the
            # "0.2 ms/div" the whole span's unit would give.
            stride_factor, stride_unit = self.time_axis_units(stride / (factor or 1.0))
            self.ax.set_xlabel("Time (%s) — %.4g %s/div"
                               % (unit, stride / (factor or 1.0) * stride_factor, stride_unit))

        self.ax.grid(True, which="major", linestyle=":", linewidth=0.7,
                     color="#46585a", alpha=0.9)
        # zorder keeps the cross under the trace, where the graticule belongs.
        if window and window > 0:
            self.ax.axvline(window / 2.0, linestyle=":", linewidth=1.0, color="#8ea3a4",
                            alpha=0.95, zorder=0.5)
        self.ax.axhline((low + high) / 2.0, linestyle=":", linewidth=1.0, color="#8ea3a4",
                        alpha=0.95, zorder=0.5)

    def plot_waveform(self):
        channels = getattr(self.scope.waveform_data, "channels", []) or []
        self.ax.clear()
        self.ax.set_facecolor("#101719")
        self.ax.grid(True, alpha=0.25, color="#526060", linestyle=":")
        self.ax.set_ylabel("Voltage (V)")

        palette = {
            "CH1": "#ffe33e",
            "CH2": "#26d7e8",
            "CH3": "#ff9c5b",
            "CH4": "#c792ea",
        }
        # Every channel shares one timebase, so the unit is chosen from the first
        # trace that has data and applied to all of them.
        factor, unit, span = 1.0, "s", None
        for channel in channels:
            samples = channel.get("waveform_data") or []
            interval = float(channel.get("point_interval", 0) or 0)
            if samples and interval:
                factor, unit = self.time_axis_units(interval * (len(samples) - 1))
                span = interval * (len(samples) - 1) * factor
                break
        self.ax.set_xlabel(f"Time ({unit})")

        plotted = False
        for channel in channels:
            name = str(channel.get("name", "CH1")).upper()
            color = palette.get(name, "#9cdc9c")
            y = np.asarray(channel.get("waveform_data", []), dtype=float)
            if y.size == 0:
                continue
            # The amplitudes are the instrument's own volts, already at the BNC:
            # nothing is folded in here. Keeping the call makes the one place a
            # convention could change explicit rather than scattered through the
            # drawing code.
            number = self.channel_number(channel)
            if number:
                y = y * self.display_ratio(number, channel)
            x = np.arange(y.size, dtype=float) * float(channel.get("point_interval", 1.0) or 1.0) * factor
            self.ax.plot(x, y, color=color, linewidth=1.4)
            plotted = True

        # The vertical frame comes from the instrument's own framing, so the trace
        # is drawn at the size the scope is showing it.
        framing = self.vertical_frame(channels) if plotted else None

        # The graticule is the instrument's screen: 12 columns wide, so the window
        # is 12 columns at the selected time/div. The capture says what the
        # timebase is, which keeps a square exactly one division; without it the
        # samples themselves decide the window.
        timebase = getattr(self.scope.waveform_data, "timebase_scale", None)
        window = span
        if isinstance(timebase, (int, float)) and timebase:
            window = float(timebase) * HDS_HORIZONTAL_DIVISIONS * factor

        if not plotted:
            self.ax.text(0.5, 0.5, "No waveform data", transform=self.ax.transAxes,
                         ha="center", va="center", color="#94a5a5")
        else:
            self.show_graticule(framing, window, unit, factor)
        self.draw_reference_lines(plotted)
        self.canvas.draw_idle()
        self._plot_cache = channels
        self.sync_vertical_controls()

    def refresh_cursors(self):
        """Read back the two values the plot annotates.

        Only real numbers are kept: a scope that answers with something
        unexpected must not take the plot down with it, so anything else leaves
        the previous cursor alone.
        """
        level = None
        try:
            level = self.scope.get_trigger_level_volts()
        except Exception:
            level = None
        self._trigger_level_v = level if isinstance(level, (int, float)) else None

        position = None
        try:
            position = self.scope.get_horizontal_position_seconds()
        except Exception:
            position = None
        if isinstance(position, (int, float)):
            self._horizontal_position = position

    def draw_reference_lines(self, plotted):
        """Trigger level on the Y axis, horizontal position above the X axis.

        The trigger level is a voltage the instrument reports, so it is drawn as
        a cursor across the plot. The horizontal position is only annotated:
        this firmware reports the position, but the captured samples were not
        observed to shift with it, and a line across the trace would imply a
        sample alignment that is not there.
        """
        if not plotted:
            return
        level = getattr(self, "_trigger_level_v", None)
        if isinstance(level, (int, float)):
            # A level outside the data's range is exactly when a scope must still
            # show it, so the Y limits are widened rather than the cursor dropped.
            low, high = self.ax.get_ylim()
            if level < low or level > high:
                span = (high - low) or 1.0
                self.ax.set_ylim(min(low, level - 0.05 * span), max(high, level + 0.05 * span))
            self.ax.axhline(level, color="#ff7b72", linestyle="--", linewidth=1.0, alpha=0.85)
            self.ax.annotate("TRIG %g V" % level, xy=(0.0, level),
                             xycoords=("axes fraction", "data"),
                             xytext=(4, 0), textcoords="offset points",
                             color="#ff7b72", fontsize=8, va="center", ha="left",
                             fontweight="bold")
        position = getattr(self, "_horizontal_position", None)
        if isinstance(position, (int, float)) and position:
            self.ax.set_title("HPOS %s" % self.timebase_position_text(position),
                              color="#7bd6ff", fontsize=8, loc="right", pad=6)

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
