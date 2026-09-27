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

from modern_lab import PRIORITY_REFRESH, ModernLabUI
import os

import analysis
import waveform_export
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
        #: The panel's own vertical position per channel, in volts at the input.
        self._display_offset = {}
        #: Where each trace's 0 V sits, as last drawn: the panel's position control
        #: applied, per channel.
        self._zero_marks = []
        self._display_scale_is_auto = {}
        #: The probe convention the trace is SHOWN in, per channel. Unset means the
        #: instrument's own, and the trace is then drawn in tip volts.
        self._display_probe = {}
        #: What the cursors last measured, for the strip under the plot.
        self._cursor_reading = None
        #: Unattended recording: every capture written to a folder, untouched.
        self.recording = False
        self._records = 0

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

    def adopt_model(self):
        """Switch to the settings that belong to whatever just identified itself.

        Called after *IDN?, because the calibration, the probe and the bench
        reference describe an INSTRUMENT, and a bench can have more than one.
        """
        model = ""
        try:
            model = self.scope.model or ""
        except Exception:
            model = ""
        if not model:
            return None
        before = self.setup.values.get("calibration_trim")
        self.setup.use_model(model)
        try:
            self.setup.apply()
        except Exception as exc:
            self.log("Could not apply %s's settings: %s" % (model, exc), "ERROR")
            return None
        after = self.setup.values.get("calibration_trim")
        if after != before:
            self.log("%s has its own calibration: trim %s"
                     % (model, "untrimmed" if after is None else "x%.6g" % after))
        else:
            self.log("%s uses the settings already in place." % model)
        return model

    def connect_scope(self):
        address = self.conn_address.get().strip()
        conn_type = self.conn_type.get().strip().lower()
        port_text = getattr(self, "conn_port", None)
        port_value = port_text.get().strip() if port_text is not None else ""

        # The address box is not on screen for USB, and the instrument is found by
        # VID/PID (HDS) or auto-detected as a COM port (serial models), so nothing
        # typed for LAN can steer this. Which of several attached scopes to open IS
        # steerable, and that is the serial from the setup - worth having, because
        # the HID endpoint takes one owner at a time and opening the wrong
        # instrument looks like no scope.
        chosen = self.setup.usb_serial
        attached = attached_scopes()
        if conn_type == "usb" and len(attached) > 1:
            self.log("%d OWON USB devices attached; using %s. Pick one in SETUP."
                     % (len(attached), chosen or "the first found"))
        try:
            baudrate = int(port_value) if port_value else 115200
            port = int(port_value) if port_value else 3000
        except ValueError:
            messagebox.showerror("Connection Error", "Invalid port or baud rate")
            return

        # Opening the transport happens on the worker, like every other call. It is
        # not instant: a USB probe that has to try several ports takes seconds, and
        # on a button press that was seconds of a window that would not answer.
        self._set_status("Connecting\u2026")
        self.connect_btn.config(text="Connecting\u2026", state="disabled")
        self.report_later("connect",
                          lambda scope: self.open_transport(scope, conn_type, chosen,
                                                            baudrate, address, port),
                          lambda answer, error=None: self.finish_connect(conn_type, answer, error))

    @staticmethod
    def open_transport(scope, conn_type, chosen, baudrate, address, port):
        """Open the transport and ask the instrument what it is. On the worker.

        Returns (opened, what it said). The *IDN? is made here rather than after the
        hand-over because it is another round trip, and its answer is what the panel
        shows as the device.
        """
        if conn_type == "usb":
            if chosen and getattr(scope, "is_hds", False):
                opened = scope.connect_usb_hid(serial=chosen)
                if opened:
                    scope.identify_model()
            else:
                opened = scope.connect_usb("auto", baudrate)
        else:
            opened = scope.connect_lan(address or "10.1.1.131", port)
        if not opened:
            return False, ""
        try:
            return True, (scope.get_idn() or "")
        except Exception:
            # Connected but unwilling to identify itself: keep the connection and
            # let the other reads fill the panel in.
            return True, ""

    def finish_connect(self, conn_type, answer, error=None):
        """Wire the panel up to the instrument that answered, or say that it did not."""
        self.connect_btn.config(state="normal")
        opened, idn = (answer if isinstance(answer, tuple) else (bool(answer), ""))
        if error is not None or not opened:
            self.connect_btn.config(text="Connect")
            if error is not None:
                self.log(f"Connection failed: {error}", "ERROR")
                messagebox.showerror("Connection Error",
                                     f"Failed to connect via {conn_type.upper()}\n{error}")
            else:
                messagebox.showerror("Connection Error",
                                     f"Failed to connect via {conn_type.upper()}")
            self._set_status("Connection failed")
            return

        self.connect_btn.config(text="Disconnect")
        # What this instrument is decides which calibration applies to it. On the
        # USB path the model has just been read from *IDN?; on a transport that has
        # not identified itself yet this does nothing rather than guessing.
        self.adopt_model()
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
            self.tell_scope("CH%d display -> %s" % (channel, "ON" if state else "OFF"),
                            lambda scope, c=channel, s=state: scope.set_channel_display(c, s))

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
            self.tell_scope("CH%d coupling -> %s" % (channel, coupling),
                            lambda scope, c=channel, k=coupling: scope.set_channel_coupling(c, k))

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
        """Move this trace up and down in the view, in volts at the input.

        Not a write to the instrument, for the reason the scale control is not one:
        a vertical write is accepted, not acted on, and leaves the instrument's own
        readings out of step with its gain. So the position moves the drawing - a
        row of the grid stays the volts/div it was, and the trace moves up by the
        volts given - while the instrument's own position is set on its keys.

        This used to call a method that does not exist, so every press raised
        inside a Tk callback: the control did nothing on screen and the sync behind
        it never ran, which is what "the panel stops answering" looks like from
        the outside.
        """
        if not self._is_connected():
            return
        try:
            value = float(offset)
        except (TypeError, ValueError):
            self.log("CH%d position: %r is not a number; the trace was left where it "
                     "is." % (channel, offset), "WARNING")
            return
        self._display_offset[channel] = value
        self.log("CH%d position: the trace moved %+.3g V; the grid did not. The "
                 "instrument's own vertical position is set on its front panel."
                 % (channel, value))
        self.plot_waveform()

    def set_timebase(self):
        if self._is_connected():
            wanted = self.timebase_scale.get()
            self.tell_scope("Timebase -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_timebase_scale(value))

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
        text = self.timebase_position_text(seconds)
        self.tell_scope("Horizontal position -> %g div (%s)" % (divisions, text),
                        lambda scope, value=text: scope.set_timebase_offset(value))
        if self.scope.waveform_data.channels:
            self.plot_waveform()

    def set_trigger_mode(self):
        if self._is_connected():
            wanted = self.trigger_mode.get()
            self.tell_scope("Trigger mode -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_trigger_mode(value))

    def set_trigger_source(self):
        if self._is_connected():
            wanted = self.trigger_source.get()
            self.tell_scope("Trigger source -> %s" % wanted,
                            lambda scope, value=wanted: getattr(
                                scope, "set_edge_trigger_source", scope.set_trigger_source)(value))

    def set_trigger_slope(self):
        if self._is_connected():
            wanted = self.trigger_slope.get()
            self.tell_scope("Trigger slope -> %s" % wanted,
                            lambda scope, value=wanted: getattr(
                                scope, "set_edge_trigger_slope", scope.set_trigger_slope)(value))

    def set_trigger_level(self):
        if self._is_connected():
            value = self.trigger_level.get()
            try:
                numeric = float(value)
                value = int(numeric) if numeric.is_integer() else numeric
                self._trigger_level_v = float(numeric)
            except Exception:
                pass
            self.tell_scope("Trigger level -> %s" % self.trigger_level.get(),
                            lambda scope, value=value: getattr(
                                scope, "set_edge_trigger_level", scope.set_trigger_level)(value))
            if self.scope.waveform_data.channels:
                self.plot_waveform()

    def set_acquire_type(self):
        if self._is_connected():
            wanted = self.acq_type.get()
            self.tell_scope("Acquisition -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_acquire_type(value))

    def set_acquire_average(self):
        if self._is_connected():
            wanted = self.acq_average.get()
            self.tell_scope("Average count -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_acquire_average(value))
            self.log(f"Average count -> {self.acq_average.get()}")

    def set_memory_depth(self):
        if self._is_connected():
            wanted = self.mem_depth.get()
            self.tell_scope("Memory depth -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_memory_depth(value))
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
        """Ask for the multimeter's function and relative state.

        Three round trips, so they are read on the worker and the panel is filled
        in when they arrive - each reading failing on its own, which is how this
        subsystem has to be treated: it answers some queries and is silent for
        others on the same instrument.
        """
        if not self._is_connected():
            return
        self.report_later("dmm_state", self.read_dmm_state, self.apply_dmm_state)

    @staticmethod
    def read_dmm_state(scope):
        """Everything the multimeter panel mirrors. Runs on the worker."""
        state = {"report": {}, "function": None, "subtype": None,
                 "relative": None, "errors": []}
        try:
            state["report"] = scope.dmm_capabilities(refresh=True) or {}
        except Exception as exc:
            state["errors"].append("Multimeter probe failed: %s" % exc)
        try:
            state["function"], state["subtype"] = scope.get_dmm_function()
        except Exception as exc:
            state["errors"].append("Multimeter state failed: %s" % exc)
        try:
            state["relative"] = scope.get_dmm_relative()
        except Exception as exc:
            state["errors"].append("Multimeter relative state failed: %s" % exc)
        return state

    def apply_dmm_state(self, state, error=None):
        """Put a multimeter readback on the panel."""
        if error is not None:
            self.log("Multimeter state failed: %s" % error, "ERROR")
            return
        state = state or {}
        for message in state.get("errors") or []:
            self.log(message, "ERROR")
        report = state.get("report") or {}
        function, subtype = state.get("function"), state.get("subtype")
        self._dmm_caption = self.dmm_caption(function, subtype)
        if function:
            self.dmm_function.set("VOLT" if function == "VOLTage" else "AMP")
            self.dmm_type.set(subtype or self.dmm_type.get())
        relative = state.get("relative")
        if relative is not None:
            self.dmm_relative.set(relative is not False and relative is not None)
        silent = report.get("silent") if isinstance(report, dict) else None
        self.refresh_dmm_modes(report)
        if report and not report.get("reading"):
            self.dmm_note.set("this instrument did not answer the multimeter")
        elif silent and isinstance(silent, (list, tuple)):
            self.dmm_note.set("no answer for %s on this unit: use the instrument's own keys"
                              % ", ".join(str(name).replace("CONTinuity", "continuity")
                                           for name in silent))
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

    #: How the multimeter functions are labelled on the panel.
    DMM_MODE_LABELS = {"VOLTage": "VOLT DC/AC", "CURRent": "AMP DC/AC",
                       "RESistance": "RESISTANCE", "DIODe": "DIODE",
                       "CONTinuity": "CONTINUITY", "CAPacitance": "CAPACITANCE"}

    def refresh_dmm_modes(self, capabilities):
        """Offer the functions the instrument answers, and name the ones it does not.

        Probed rather than assumed: the series manual documents all six, and this
        unit answers two, so a panel that offered six would be offering four that
        do nothing.
        """
        if not isinstance(capabilities, dict):
            # A probe that did not come back as a report tells us nothing about what
            # is answerable, so nothing extra is offered on the strength of it.
            return []
        silent = list(capabilities.get("silent") or [])
        offered = [self.DMM_MODE_LABELS[name] for name in
                   ("VOLTage", "CURRent", "RESistance", "DIODe", "CONTinuity", "CAPacitance")
                   if name not in silent]
        if hasattr(self, "dmm_mode_box"):
            self.dmm_mode_box.configure(values=tuple(offered))
        return offered

    def set_dmm_mode(self, label=None):
        """Switch the multimeter to a function, and report what the instrument confirmed."""
        chosen = str(label or (self.dmm_mode.get() if hasattr(self, "dmm_mode") else "") or "").strip()
        if not chosen:
            self.log("Multimeter: no mode chosen.")
            return None
        wanted = chosen
        for name, text in self.DMM_MODE_LABELS.items():
            if text.lower() == chosen.lower():
                wanted = name
                break
        try:
            confirmed = self.scope.set_dmm_function(wanted)
        except Exception as exc:
            self.log("Multimeter mode failed: %s" % exc, "ERROR")
            return None
        function, subtype = (confirmed if isinstance(confirmed, tuple) else (confirmed, None))
        if function:
            self._dmm_caption = self.dmm_caption(function, subtype or "")
            self.dmm_note.set("")
            self.log("Multimeter set to %s%s" % (function, " %s" % subtype if subtype else ""))
        else:
            self.log("Multimeter did not take %s; it stayed as it was." % chosen, "WARNING")
        self.sync_dmm()
        if not function:
            # Said after the probe has had its turn, so the specific refusal is what
            # stays on screen: this subsystem accepts writes it then ignores.
            self.dmm_note.set("the instrument did not take %s: set it with its own keys" % chosen)
        return function

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
        """Ask for the multimeter readout; one query is ~32 ms on the worker.

        Read on this thread it was a 32 ms freeze in the middle of live
        acquisition, for a number that does not move quickly.
        """
        if not self._is_connected():
            self.dmm_value.set("—")
            return
        self.ask_scope("dmm", lambda scope: scope.get_dmm_reading(), coalesce=True,
                       priority=PRIORITY_REFRESH)

    def apply_dmm_reading(self, reading, error=None):
        """Show a multimeter reading, or say why there is not one."""
        if error is not None:
            self.dmm_value.set("—")
            self.log(f"Multimeter read failed: {error}", "ERROR")
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
            self.tell_scope("Instrument reset", lambda scope: scope.reset())
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

    def auto_scale(self):
        if self._is_connected():
            self.tell_scope("Auto scale enabled", lambda scope: scope.auto_scale(True))

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
        # On the worker like everything else: AUTO measures, decides and writes,
        # which is several round trips. It used to post to a queue that only the
        # capture path drained, so it could wait for a frame that never came.
        self.ask_scope("autoset", lambda scope: scope.auto_frame())

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
        """Refresh the panel from the instrument.

        A dozen reads, so they happen on the worker and the panel is filled in when
        the answer lands. Read here, this froze the window for about a third of a
        second - nothing on this panel reads the instrument on the Tk thread now.
        """
        if not self._is_connected():
            return
        self.status_var.set("Reading the instrument's settings…")
        self.report_later("states", self.read_all_states, self.apply_all_states)

    @staticmethod
    def read_all_states(scope):
        """Read every setting the panel mirrors. Runs on the worker.

        Static, and touching no widget on purpose: the thread that calls this is
        not the thread that made the widgets, and nothing here may reach them.
        """
        values = {
            "timebase": scope.get_timebase_scale(),
            "hpos": scope.get_horizontal_position_seconds(),
            "channels": [],
            "trigger": {"mode": scope.get_trigger_mode(),
                        "source": scope.get_trigger_source(),
                        "slope": scope.get_trigger_slope(),
                        "level": scope.get_trigger_level()},
            "acquire": {"type": scope.get_acquire_type(),
                        "average": scope.get_acquire_average(),
                        "memory": scope.get_memory_depth()},
        }
        for number in range(1, scope.get_channel_count() + 1):
            values["channels"].append({
                "number": number,
                "scale": scope.get_channel_scale(number),
                "coupling": scope.get_channel_coupling(number),
                "display": scope.get_channel_display(number),
                "probe": scope.get_channel_probe(number),
            })
        return values

    def apply_all_states(self, values, error=None):
        """Put a readback into the controls. Only what the instrument answered."""
        if error is not None:
            self.log(f"Query settings failed: {error}", "ERROR")
            self.status_var.set("Could not read the instrument's settings")
            return
        try:
            scale = self.scope.parse_scale(values.get("timebase") or "")
            if values.get("timebase") and values["timebase"] in self.scope.TIMEBASE_SCALES:
                self.timebase_scale.set(values["timebase"])
            position = values.get("hpos")
            if position is not None and scale:
                self._horizontal_position = position
                self.timebase_offset.delete(0, tk.END)
                self.timebase_offset.insert(0, "%g" % (position / scale))

            for channel in values.get("channels") or []:
                number = channel.get("number")
                entry = channel.get("scale")
                if entry and str(entry).lower() in self.scope.VOLTAGE_SCALES:
                    getattr(self, f"ch{number}_scale").set(str(entry).lower())
                coupling = channel.get("coupling")
                if coupling and coupling in self.scope.COUPLING_MODES:
                    getattr(self, f"ch{number}_coupling").set(coupling)
                # Only when the instrument actually answered: the HDS does not
                # implement :CHn:DISPlay?, and treating the empty reply as "off"
                # would untick a channel that is plainly on screen.
                display = channel.get("display")
                if display not in (None, ""):
                    getattr(self, f"ch{number}_display").set(
                        str(display).strip().upper() == "ON")
                # The probe is read back rather than left on a default the user
                # would then write over. The scope answers "10X" where the list
                # holds "X10".
                token = str(channel.get("probe") or "").strip().upper()
                if token and not token.startswith("X"):
                    token = "X" + token.rstrip("X")
                if token in self.scope.PROBE_ATTEN:
                    getattr(self, f"ch{number}_probe").set(token)
                # The instrument's own vertical position is deliberately NOT pushed
                # into the entry: that control applies whatever it holds, so a
                # readback put there would come straight back as a write - and this
                # family's position is a front-panel setting besides.

            trigger = values.get("trigger") or {}
            for key, attribute, choices in (
                    ("mode", "trigger_mode", self.scope.TRIGGER_MODES),
                    ("source", "trigger_source", self.scope.TRIGGER_SOURCES),
                    ("slope", "trigger_slope", self.scope.TRIGGER_SLOPES)):
                entry = trigger.get(key)
                if entry and entry in choices:
                    getattr(self, attribute).set(entry)
            if trigger.get("level") not in (None, ""):
                self.trigger_level.delete(0, tk.END)
                self.trigger_level.insert(0, str(trigger["level"]))

            acquire = values.get("acquire") or {}
            if acquire.get("type") and acquire["type"] in self.scope.ACQ_TYPES:
                self.acq_type.set(acquire["type"])
            if acquire.get("average") and str(acquire["average"]) in self.scope.AVG_COUNTS:
                self.acq_average.set(str(acquire["average"]))
            if acquire.get("memory") and str(acquire["memory"]) in self.scope.MEMORY_DEPTHS:
                self.mem_depth.set(str(acquire["memory"]))
            self.log("Settings refreshed")
            self.status_var.set("Settings read from the instrument")
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

        lines.append("From the instrument (referred to its own volts/div and probe "
                     "labels): reading…")
        self.write_measurement_text(channel, lines)
        # The instrument's block is another set of queries: read on the worker, and
        # written under the figures already on screen when it arrives.
        self.report_later("measurements",
                          lambda scope: scope.get_all_measurements(channel),
                          lambda payload, error, ch=channel, before=lines:
                          self.apply_instrument_measurements(ch, before, payload, error))

    def write_measurement_text(self, channel, lines):
        """Show the measurement lines, and keep the log honest about where they came from."""
        text = "\n".join(lines)
        self.measurement_text.delete("1.0", tk.END)
        self.measurement_text.insert(tk.END, text + "\n")
        self.measurement_text.see(tk.END)
        self.log("Measurements read from CH%d" % channel)

    def apply_instrument_measurements(self, channel, lines, payload, error=None):
        """Add the instrument's own block under the figures from the capture."""
        body = list(lines)
        if isinstance(body, list) and body and body[-1].endswith("reading…"):
            body.pop()
        body.append("From the instrument (referred to its own volts/div and probe labels):")
        if error is not None:
            body.append("  could not be read: %s" % error)
        elif isinstance(payload, dict) and payload:
            body.extend("  %s: %s" % (key, value) for key, value in sorted(payload.items()))
        else:
            body.append("  " + str(payload))
        self.write_measurement_text(channel, body)

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

    def display_offset(self, number):
        """This channel's vertical position in the view, in volts at the input.

        The panel's control, not the instrument's: it moves the drawing and leaves
        the volts per code and the grid alone, so a trace can be moved off a cursor
        without changing a single amplitude reading.
        """
        return (getattr(self, "_display_offset", {}) or {}).get(number, 0.0)

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

        The frame does not follow the panel's position control: that control moves
        the trace across this frame, the way a position knob moves a trace across a
        graticule that is fixed to the screen.

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
            # The frame is the instrument's screen, so the position control does
            # NOT move it: the trace moves across a grid that stays where it is,
            # which is what makes a row on screen mean the volts/div it reads.
            # Adding the offset here moved the window with the trace, so the two
            # cancelled and the grid slid while the trace held still - a visual
            # reading taken against a sliding grid is worth nothing.
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

    # ------------------------------------------------------------------
    # Views: one capture, four ways of reading it
    def capture_channels(self):
        """The channels of whatever is being shown - a live capture or an open file."""
        return list(getattr(self.scope.waveform_data, "channels", []) or [])

    def analysis_channel(self, name=None):
        """The channel dict the analysis views work on, by name or by the panel's pick."""
        wanted = str(name or (self.meas_source.get() if hasattr(self, "meas_source") else "CH1")).upper()
        channels = self.capture_channels()
        for channel in channels:
            if str(channel.get("name", "")).upper() == wanted:
                return channel
        return channels[0] if channels else None

    def plot_waveform(self):
        """Draw the capture in whichever view is selected.

        The views are four readings of the same samples, so the framing controls
        are synced once, here, rather than inside each one - they describe the
        capture, not the drawing.
        """
        view = getattr(self, "_view", "time")
        try:
            if view == "fft":
                self.plot_fft()
            elif view == "math":
                self.plot_math()
            elif view == "xy":
                self.plot_xy()
            else:
                self.plot_time()
        finally:
            self.refresh_marker_lines()
            self.update_cursor_readout()
            self._plot_cache = self.capture_channels()
            self.sync_vertical_controls()

    def plot_fft(self):
        """The spectrum of one channel, with the limits it was computed under.

        The frequency axis comes from the capture's own sample interval, so a peak
        lands where it belongs. The Nyquist limit and the bin width are written on
        the plot, because a spectrum drawn from 20 us samples invites reading
        meaning into frequencies it never saw.
        """
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        key = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        spectrum = analysis.fft_spectrum(key, window=options.get("window", "hanning"),
                                        fmt=options.get("format", "dBV"))
        if not spectrum:
            self.ax.text(0.5, 0.5, "No spectrum\nthis capture carries no sample interval",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.ax.set_xlabel("Frequency", color=palette["text"], fontsize=9)
            self.ax.set_ylabel("", color=palette["text"], fontsize=9)
            self.canvas.draw_idle()
            return
        frequencies, values = analysis.display_spectrum(spectrum)
        self.ax.plot(frequencies, values, color=palette["trace1"], linewidth=1.2)
        if options.get("log"):
            self.ax.set_xscale("log")
            self.ax.set_xlim(max(spectrum["bin_hz"], 1.0), spectrum["nyquist"])
        for peak in analysis.spectrum_peaks(spectrum, count=int(options.get("peaks", 5))):
            self.ax.annotate(analysis.format_hz(peak["frequency"]),
                             xy=(peak["frequency"], peak["value"]),
                             xytext=(4, 4), textcoords="offset points",
                             color=palette["math"], fontsize=8, fontweight="bold")
        self.ax.set_xlabel("Frequency (Hz)", color=palette["text"], fontsize=9)
        self.ax.set_ylabel("Magnitude (%s)" % spectrum["unit"], color=palette["text"], fontsize=9)
        self.ax.set_title("%s \u2022 %s window \u2022 %s \u2022 Nyquist %s \u2022 %.4g Hz/bin"
                          % (str(key.get("name", "CH1")).upper(), spectrum["window"].capitalize(),
                             analysis.format_rate(spectrum["sample_rate"]),
                             analysis.format_hz(spectrum["nyquist"]), spectrum["bin_hz"]),
                          color=palette["text"], fontsize=8, loc="left")
        self.canvas.draw_idle()

    def plot_math(self):
        """One trace computed from two: the vendor's Mathematics panel, in our own frame."""
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        options = getattr(self, "_math_options", {})
        first = self.analysis_channel()
        second = self.analysis_channel(options.get("second", "CH2"))
        op = options.get("op", "subtract")
        self.ax.set_xlabel("Time (s)", color=palette["text"], fontsize=9)
        self.ax.set_ylabel("Voltage (V)", color=palette["text"], fontsize=9)
        if first is None or (op != "invert" and second is None):
            self.ax.text(0.5, 0.5, "Nothing to compute: a channel is missing",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        try:
            computed = analysis.math_trace(analysis.samples(first),
                                           None if op == "invert" else analysis.samples(second), op)
        except ValueError as exc:
            # Two channels that came back with different lengths is a fact about the
            # capture, so it is reported as one rather than padded away.
            self.ax.text(0.5, 0.5, str(exc), transform=self.ax.transAxes, ha="center",
                         va="center", color="#ff9c5b", fontsize=10)
            self.canvas.draw_idle()
            return
        times = analysis.sample_times(first)
        label = analysis.math_label(op, str(first.get("name", "CH1")).upper(),
                                    str(second.get("name", "CH2")).upper())
        self.ax.plot(times[:computed.size], computed, color=palette["math"], linewidth=1.5, label=label)
        self.ax.legend(loc="upper right", fontsize=8, facecolor=palette["face"],
                       labelcolor=palette["text"], framealpha=0.85)
        self.ax.set_title("%s \u2022 %s" % (label, analysis.format_volts(
            float(max(computed) - min(computed)) if computed.size else 0.0) + " pk-pk"),
            color=palette["text"], fontsize=8, loc="left")
        self.canvas.draw_idle()

    def plot_xy(self):
        """CH1 against CH2: the Lissajous view, and the phase between them.

        It needs two channels at once, which is why it says so rather than drawing
        an empty frame when only one is available.
        """
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        first = self.analysis_channel(getattr(self, "_xy_options", {}).get("x", "CH1"))
        second = self.analysis_channel(getattr(self, "_xy_options", {}).get("y", "CH2"))
        self.ax.set_xlabel("%s (V)" % str((first or {}).get("name", "CH1")).upper(),
                           color=palette["text"], fontsize=9)
        self.ax.set_ylabel("%s (V)" % str((second or {}).get("name", "CH2")).upper(),
                           color=palette["text"], fontsize=9)
        if first is None or second is None or first is second:
            self.ax.text(0.5, 0.5, "XY needs two channels: CH1 or CH2 is not available",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        xs, ys = analysis.xy_pairs(analysis.samples(first), analysis.samples(second))
        if xs.size == 0:
            self.ax.text(0.5, 0.5, "No samples", transform=self.ax.transAxes, ha="center",
                         va="center", color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        self.ax.plot(xs, ys, color=palette["trace2"], linewidth=1.0)
        self.ax.set_title("XY \u2022 %d points" % xs.size, color=palette["text"],
                          fontsize=8, loc="left")
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Cursors
    def analysis_traces(self):
        """{(name): (times, volts)} exactly as the plot draws them.

        Cursors must read the trace the way it is shown, so the display ratio is
        applied here, once, and nowhere else.
        """
        traces = {}
        for channel in self.capture_channels():
            values = analysis.samples(channel)
            if values.size == 0:
                continue
            number = self.channel_number(channel)
            if number:
                values = values * self.display_ratio(number, channel)
            times = analysis.sample_times(channel)
            if times.size != values.size:
                times = np.arange(values.size, dtype=float)
            traces[str(channel.get("name", "CH1")).upper()] = (times, values)
        return traces

    def on_cursors_moved(self):
        self.update_cursor_readout()

    def update_cursor_readout(self):
        """Say what the markers measure, including the level of each trace at them."""
        cursors = getattr(self, "_cursors", {}) or {}
        reading = analysis.cursor_readings(self.analysis_traces(),
                                          vertical_times=(cursors.get("t1"), cursors.get("t2")),
                                          horizontal_volts=(cursors.get("v1"), cursors.get("v2")))
        self._cursor_reading = reading
        line = analysis.describe_cursors(reading)
        for name, entry in (reading.get("traces") or {}).items():
            if entry.get("at_t1") is not None and entry.get("at_t2") is not None:
                line += "   %s: %s \u2192 %s" % (name, analysis.format_volts(entry["at_t1"]),
                                                  analysis.format_volts(entry["at_t2"]))
        if hasattr(self, "cursor_text"):
            self.cursor_text.set(line)
        return reading

    def report_peaks(self):
        """Put the spectrum's peaks in the log, with the limits they were found under."""
        key = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        spectrum = analysis.fft_spectrum(key, window=options.get("window", "hanning"),
                                        fmt=options.get("format", "dBV"))
        self.log(analysis.describe_spectrum(spectrum))
        return spectrum

    # ------------------------------------------------------------------
    # Tables, files and the instrument's own language
    def table_data(self):
        """Columns and rows for the data table: index, time, then a column per channel."""
        traces = self.analysis_traces()
        if not traces:
            return [], []
        names = list(traces)
        columns = ["index", "time_s"] + names
        rows = []
        for index in range(max(len(times) for times, _ in traces.values())):
            row = [index]
            row.append("%.9g" % (index * analysis.point_interval(self.analysis_channel())))
            for name in names:
                times, values = traces[name]
                row.append("%.6g" % values[index] if index < values.size else "")
            rows.append(row)
        return columns, rows

    def load_capture_file(self, path):
        """Show a saved capture in place of a live one.

        The live loop is stopped first: it re-plots from the instrument every
        second, so a file opened underneath it would disappear before it could be
        read.
        """
        try:
            settings, channels = waveform_export.read_capture(path)
        except Exception as exc:
            self.log("Could not open %s: %s" % (path, exc), "ERROR")
            messagebox.showerror("Open capture", "Could not open that file:\n%s" % exc)
            return None
        if not channels:
            messagebox.showwarning("Open capture", "That file has no samples in it.")
            return None
        if self.auto_refresh_var.get():
            self.toggle_live()
        loaded = waveform_export.LoadedCapture(channels, settings, source=os.path.basename(path))
        self.scope.waveform_data = loaded
        self._loaded_capture = loaded
        self.capture_state.set("FILE \u2022 %s" % os.path.basename(path))
        self.device_info.set(loaded.describe())
        self.log("Opened %s (%s)" % (path, loaded.describe()))
        self.plot_waveform()
        return loaded

    def run_scpi_command(self, text, allow_writes):
        """Send one command to the instrument and hand back what it said.

        A write is refused unless it was asked for explicitly: this console sends
        whatever is typed, and a write on this unit can leave the instrument in a
        state where later captures report values that were never on the input.
        """
        if not self._is_connected():
            return "not connected"
        command = str(text or "").strip()
        if not command:
            return "nothing to send"
        is_query = "?" in command
        if not is_query and not allow_writes:
            return "refused: that looks like a write. Tick 'Allow writes' if you mean it."
        # A command is a round trip - 32 ms for a text query, far more for a capture
        # - so it goes to the worker and the console prints the reply when it lands.
        # Sent from here it froze the window for as long as the instrument took.
        work = (lambda scope: scope.query(command)) if is_query else \
               (lambda scope: scope.send_command(command))
        self.report_later("console", work,
                          lambda value, error, sent=command: self.deliver_scpi_reply(sent, value, error))
        return "sent: %s" % command

    def console_reply(self, text):
        """Hand a late reply to the console, if one is still open."""
        sink = getattr(self, "_console_sink", None)
        if sink is None:
            return
        try:
            sink(text)
        except tk.TclError:
            self._console_sink = None

    def deliver_scpi_reply(self, command, reply, error=None):
        """Print what the instrument said, or why there is nothing to print."""
        if error is not None:
            self.log("SCPI %s failed: %s" % (command, error), "ERROR")
            self.console_reply("SCPI %s failed: %s" % (command, error))
            return
        answer = "" if reply is None else str(reply).strip()
        self.log("SCPI %s \u2192 %s" % (command, answer or "(no reply)"))
        self.console_reply(answer or "(no reply)")

    @staticmethod
    def read_trigger_rows(scope):
        """What the instrument says about its trigger and framing, one node at a time.

        Runs on the worker: twelve queries are about a third of a second, which is
        a third of a second the window cannot repaint if they happen here.
        """
        rows = []
        for label, getter in (
            ("Trigger mode", scope.get_trigger_mode),
            ("Trigger source", scope.get_trigger_source),
            ("Trigger slope", scope.get_trigger_slope),
            ("Trigger coupling", scope.get_trigger_coupling),
            ("Trigger level", scope.get_trigger_level_volts),
            ("Horizontal position", scope.get_horizontal_position_seconds),
            ("Timebase", scope.get_timebase_scale),
            ("Acquire type", scope.get_acquire_type),
            ("Memory depth", scope.get_memory_depth),
            ("CH1 scale (label)", lambda: scope.get_channel_scale(1)),
            ("CH1 coupling", lambda: scope.get_channel_coupling(1)),
            ("CH1 probe", lambda: scope.get_channel_probe(1)),
        ):
            try:
                value = getter()
            except Exception as exc:
                value = "error: %s" % exc
            rows.append((label, "\u2014" if value in (None, "") else value))
        # Two things the vendor's remote panel has buttons for, which this family
        # does not document a command for. Saying so beats a button that may do
        # nothing - and the console is right there for anyone who wants to try.
        rows.append(("Run / Stop / Force", "no such command in the HDS200 set: run and stop on "
                                           "the instrument, or try it in the SCPI console"))
        rows.append(("Self correction", "on the instrument: UTILITY \u2192 Self Correct. It takes "
                                        "minutes and must not be interrupted."))
        return rows

    def capture_rows(self):
        """What the capture itself says - already in memory, so no instrument call."""
        rows = []
        for channel in self.capture_channels():
            name = str(channel.get("name", "CH1")).upper()
            rows.append(("Captured %s scale" % name, channel.get("volts_per_div")))
            rows.append(("Captured %s points" % name, len(channel.get("waveform_data") or [])))
            rows.append(("Captured %s interval" % name,
                         analysis.format_seconds(analysis.point_interval(channel))))
        return rows

    def trigger_state_rows(self, deliver):
        """Ask for the trigger readout, and add what the capture already knows.

        The instrument's rows are read on the worker; the rows describing the
        capture are drawn from memory on this side, so the dialog gets one list
        instead of waiting on a second round trip for numbers it already has.
        """
        def finish(rows, error=None):
            deliver(list(rows or []) + self.capture_rows(), error)

        self.report_later("readout", self.read_trigger_rows, finish)

    def refresh_trigger_state(self):
        self.log("Trigger and acquisition state read back.")

    def software_autoset(self):
        """Frame the time axis on the signal, and say what is left for the front panel.

        The vertical gain is not reachable from here - a volts/div write on this
        unit is inert and corrupts the instrument's own readout - so this does the
        half that works, using the one framing write that IS live, and says plainly
        what the other half needs.
        """
        if not self._is_connected():
            self.status_var.set("Connect the instrument first")
            return None
        self.status_var.set("Framing the time base from the signal…")
        self.report_later("autoset_time", self.read_autoset_target, self.apply_software_autoset)

    @staticmethod
    def read_autoset_target(scope):
        """Measure, decide a time/div, write it, and read it back. On the worker.

        All of it is instrument work - the measurement block, the ladder lookup and
        the one framing write this interface can actually make - and none of it
        touches a widget, which is what lets it run off the Tk thread.
        """
        report = {"frequency": None, "target": None, "requested": None,
                  "readback": None, "notes": []}
        try:
            measurements = scope.get_measurements_numeric(1) or {}
        except Exception as exc:
            report["notes"].append("Autoset could not read the measurements: %s" % exc)
            return report
        frequency = measurements.get("frequency")
        report["frequency"] = frequency
        if not frequency:
            report["notes"].append("Autoset: the instrument reports no frequency for CH1, "
                                   "so there is nothing to frame the time axis on.")
            return report
        target = analysis.timebase_for(frequency)
        report["target"] = target
        if target is None:
            return report
        try:
            requested = scope.nearest_timebase(target)
            scope.set_timebase_scale(requested)
            report["requested"] = requested
        except Exception as exc:
            report["notes"].append("Autoset could not set the timebase: %s" % exc)
            return report
        try:
            report["readback"] = scope.get_timebase_scale()
        except Exception:
            report["readback"] = None
        return report

    def apply_software_autoset(self, report, error=None):
        """Say what AUTO changed, what is left for the front panel, and recapture."""
        if error is not None:
            self.log("Autoset failed: %s" % error, "ERROR")
            self.status_var.set("Autoset failed")
            return None
        report = report or {}
        for note in report.get("notes") or []:
            self.log(note, "ERROR")
        if not report.get("frequency"):
            self.status_var.set("Autoset found no frequency to frame on")
            return None
        if not report.get("target") or not report.get("requested"):
            return None
        self.log("Autoset: %s measured, timebase set to %s (reads back %s). "
                 "Press AUTO on the instrument for the vertical - the volts/div write "
                 "is inert on this unit." % (analysis.format_hz(report["frequency"]),
                                             analysis.format_seconds(report["target"]),
                                             report.get("readback")))
        self.status_var.set("Time base framed from the signal")
        self.download_waveform()
        return report["target"]

    # ------------------------------------------------------------------
    # Unattended recording
    def toggle_recording(self):
        """Start or stop writing every capture to the record folder."""
        if self.recording:
            self.recording = False
            self.record_label.set("%d file(s) written" % self._records if self._records else "idle")
            self.log("Recording stopped after %d capture(s)." % self._records)
            return
        folder = (getattr(self.setup, "record_folder", "") or "").strip()
        if not folder:
            messagebox.showinfo("Record", "Set a record folder in SETUP first:\n\n"
                                          "SETUP \u2192 Record folder")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.log("Cannot use %s: %s" % (folder, exc), "ERROR")
            messagebox.showerror("Record", "Cannot write to %s:\n%s" % (folder, exc))
            return
        self.recording = True
        self._records = 0
        self.record_label.set("to %s" % folder)
        self.log("Recording every capture to %s" % folder)

    def record_capture(self):
        """Write the capture that just arrived, if recording is on.

        One file per frame, named by the second it was taken in: the point of
        leaving it running is to have the frames afterwards, so a failure to write
        stops the recording rather than going quiet.
        """
        if not self.recording:
            return None
        channels = self.capture_channels()
        if not channels:
            return None
        folder = (getattr(self.setup, "record_folder", "") or "").strip()
        name = time.strftime("capture-%Y%m%d-%H%M%S", time.localtime())
        path = os.path.join(folder, "%s-%04d.csv" % (name, self._records))
        provenance = self.provenance_block()
        self._records += 1
        # Written on the worker, because the folder may be a network share and a
        # frame does not wait for a file. A write that fails still stops the
        # recording, which is the point of leaving it running unattended.
        self.ask_scope("record",
                       lambda scope: waveform_export.write_csv(path, channels, provenance),
                       priority=PRIORITY_REFRESH)
        return path

    def provenance_block(self):
        """The settings an exported capture is only checkable with."""
        waveform = getattr(self.scope, "waveform_data", None)
        channel = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        return waveform_export.provenance(
            model=getattr(waveform, "model", "") or getattr(self.scope, "model", ""),
            serial=getattr(self.scope, "serial_number", ""),
            firmware=getattr(self.scope, "firmware", ""),
            timebase_scale_s=getattr(waveform, "timebase_scale", None),
            sample_rate=getattr(waveform, "sample_rate", ""),
            point_interval_s=analysis.point_interval(channel) if channel else None,
            points=len(channel.get("waveform_data") or []) if channel else 0,
            window=options.get("window"),
            fft_format=options.get("format"),
            view=getattr(self, "_view", "time"),
            calibration_trim=getattr(self.setup, "calibration_trim", None),
            volts_per_code=getattr(self.scope, "_calibrated_volts_per_code", None),
            note="probe %s, coupling %s" % (channel.get("probe") if channel else "",
                                            channel.get("coupling") if channel else ""))

    def plot_time(self):
        channels = self.capture_channels()
        self.ax.clear()
        face = self.palette()
        self.ax.set_facecolor(face["face"])
        self.ax.grid(True, alpha=0.25, color=face["grid"], linestyle=":")
        self.ax.set_ylabel("Voltage (V)", color=face["text"], fontsize=9)

        palette = {
            "CH1": face["trace1"],
            "CH2": face["trace2"],
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
        # (name, colour, where this trace's zero sits, volts per row of the grid),
        # for the 0 V markers drawn with the graticule below.
        references = []
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
                # The panel's vertical position, in the same volts the axis is
                # drawn in, so the trace moves and the grid does not.
                y = y + self.display_offset(number)
            x = np.arange(y.size, dtype=float) * float(channel.get("point_interval", 1.0) or 1.0) * factor
            self.ax.plot(x, y, color=color, linewidth=1.4)
            plotted = True
            references.append((name, color,
                               self.display_offset(number) if number else 0.0,
                               self.display_scale_value(number, channel) if number else None))

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
        self.draw_zero_markers(references if plotted else [])
        self.canvas.draw_idle()
        self._plot_cache = channels
        self.sync_vertical_controls()

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

    def refresh_cursors(self):
        """Ask for the two values the plot annotates.

        Two USB round trips, so they are read on the worker and applied by
        :meth:`apply_cursor_readings`. Called at the end of every frame, which is
        why they are coalesced: a readback that has been overtaken by a newer one
        is worth nothing.
        """
        self.ask_scope("cursors", self.read_cursor_values, coalesce=True,
                       priority=PRIORITY_REFRESH)

    @staticmethod
    def read_cursor_values(scope):
        """The two readings the plot annotates, each failing to None on its own."""
        try:
            level = scope.get_trigger_level_volts()
        except Exception:
            level = None
        try:
            position = scope.get_horizontal_position_seconds()
        except Exception:
            position = None
        return level, position

    def apply_cursor_readings(self, level, position):
        """Apply a readback. Only real numbers are kept, and each stands alone.

        A scope that answers with something unexpected must not take the plot
        down with it, so anything else leaves the previous cursor alone - and one
        bad value must not discard the other one, which is a reading the
        instrument did give.
        """
        self._trigger_level_v = level if isinstance(level, (int, float)) else self._trigger_level_v
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

    def draw_zero_markers(self, references):
        """A 0 V marker per enabled channel: the point that trace refers to.

        The waveform refers to this: a trace with no position applied has its zero
        on the middle row of the grid, and a trace that has been moved carries its
        zero with it. This is the only place the applied position is visible, since
        the grid deliberately does not move with it - and it is also what a peak is
        counted from, so a peak-to-peak read off the screen has a reference to
        start at.

        Drawn the way an instrument draws its ground reference: a thin dotted line
        across the graticule in the channel's own colour, labelled at the edge, CH1
        on the left and CH2 on the right so two traces cannot label over each
        other. Nothing here touches the frame - the marker moves, the grid does not.
        """
        self._zero_marks = []
        for index, (name, colour, offset, volts_per_div) in enumerate(references):
            right = bool(index % 2)
            moved = abs(offset) > 1e-12
            self.ax.axhline(offset, color=colour, linewidth=0.9, alpha=0.7, zorder=1.6,
                            linestyle=(0, (1.0, 1.6)) if moved else (0, (1.0, 3.4)))
            label = "%s 0V" % name
            if moved:
                label += "  %+.3g V" % offset
                if volts_per_div:
                    label += "  (%+.2f div)" % (offset / volts_per_div)
            self.ax.annotate(label, xy=(1.0 if right else 0.0, offset),
                             xycoords=("axes fraction", "data"),
                             xytext=(-5 if right else 5, 0), textcoords="offset points",
                             color=colour, fontsize=8, va="center",
                             ha="right" if right else "left",
                             annotation_clip=False)
            self._zero_marks.append({"name": name, "offset": offset, "label": label})

    def save_waveform(self):
        """Write the capture in whichever format the chosen filename asks for.

        Every format carries its provenance where it has room: a file of volts with
        no record of the scale, probe and calibration that produced them is a file
        nobody can check later.
        """
        channels = self.capture_channels()
        if not channels:
            messagebox.showinfo("Save capture", "No capture to save. Take one first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save capture",
            defaultextension=".csv",
            filetypes=[("CSV - samples", "*.csv"), ("JSON - samples and settings", "*.json"),
                       ("Excel workbook", "*.xlsx"), ("PNG image of the plot", "*.png"),
                       ("PDF of the plot", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            described = waveform_export.export_capture(path, channels,
                                                      self.provenance_block(), figure=self.fig)
        except Exception as exc:
            self.log("Save failed: %s" % exc, "ERROR")
            messagebox.showerror("Save capture", "Could not write that file:\n%s" % exc)
            return
        self.log("Saved %s" % described)
        messagebox.showinfo("Save capture", "Saved:\n%s\n\n%s" % (path, described))

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
