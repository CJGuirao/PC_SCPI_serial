"""Readback: panel state, instrument state, trigger rows and measurements."""

from modernlab.instrument.controller import OWONScopeController
from modernlab import analysis
import tkinter as tk


class ReadbackMixin:
    """State readback for the OWON panel."""

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
                if hasattr(self, "acq_average"):
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
            self._accum_meas_stats(trace)
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
        rows.append(("Run / Stop", ":RUNning RUN / :RUNning STOP — verified HDS271 V1.3.0. "
                                   "Use the RUN/STOP button in the toolbar."))
        rows.append(("Force trigger", "no command found on HDS271 V1.3.0: all six candidates "
                                      "(:TRIGger:FORCe, :TRIGger:SINGle:FORCe, :FORCetrig, "
                                      ":TRIGger:FORCetrig, :TRIGger:FORC, :TRIG:FORC) were silent "
                                      "and left :TRIGger:STATus? unchanged. "
                                      "Use the instrument's front panel FORCE button."))
        rows.append(("Self correction", "on the instrument: UTILITY \u2192 Self Correct. It takes "
                                        "minutes and must not be interrupted."))
        return rows

    def capture_rows(self):
        """What the capture itself says - already in memory, so no instrument call."""
        rows = []
        for channel in self.capture_channels():
            name = str(channel.get("name", "CH1")).upper()
            rows.append(("Captured %s scale" % name, channel.get("volts_per_div")))
            rows.append(("Captured %s points" % name, len(channel.get("waveform") or [])))
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

    PANEL_READBACK_ROWS = (
        ("trigger_mode", "Trigger mode"),
        ("trigger_source", "Trigger source"),
        ("trigger_slope", "Trigger slope"),
        ("trigger_coupling", "Trigger coupling"),
        ("trigger_level", "Trigger level"),
        ("acq_type", "Acquire type"),
        ("mem_depth", "Memory depth"),
    )

    @staticmethod
    def read_panel_settings(scope, channels=None):
        """What the instrument is set to now, for every control AUTO leaves on screen.

        Static, and touching no widget on purpose: this runs on the worker, which
        is not the thread that made them. The trigger and acquisition values come
        from read_trigger_rows - the same reader the readback dialog uses - so the
        two agree by construction; only the per-channel fields it does not cover
        for a second channel are read here, through the same controller getters it
        calls for the first.

        ``channels`` is the numbers the panel is showing; left out, the instrument
        is asked. The panel passes them because this firmware over-reports its own
        count - the HDS271 is a single-channel instrument that answers for four -
        and reading controls that are not on screen is four round trips for
        nothing.
        """
        rows = dict(ReadbackMixin.read_trigger_rows(scope))
        values = {name: rows.get(label) for name, label in ReadbackMixin.PANEL_READBACK_ROWS}
        if channels is None:
            try:
                count = int(scope.get_channel_count())
            except Exception:                                          # noqa: BLE001
                count = 1
            channels = range(1, max(count, 1) + 1)
        values["channels"] = []
        for number in channels:
            entry = {"number": number}
            if number == 1:
                # Already read, and read through the panel's own accessors, which
                # apply this family's dialect and match the reply to the ladder.
                entry["scale"] = rows.get("CH1 scale (label)")
                entry["coupling"] = rows.get("CH1 coupling")
            else:
                for key, getter in (("scale", scope.get_channel_scale),
                                    ("coupling", scope.get_channel_coupling)):
                    try:
                        entry[key] = getter(number)
                    except Exception:                                  # noqa: BLE001
                        entry[key] = None
            try:
                entry["display"] = scope.get_channel_display(number)
            except Exception:                                          # noqa: BLE001
                entry["display"] = None
            values["channels"].append(entry)
        return values

    def apply_panel_settings(self, values, error=None):
        """Put a whole-instrument readback into the controls, writing nothing back.

        AUTO has just run the instrument, so what it answers now is what the panel
        should be showing - and most of these are live controls, which apply what
        they hold. Written with live application on, a readback would be sent
        straight back as a write and the control would fight the instrument it
        just read: the depth of the guard is what stops that, and it covers the
        whole block so no value in it can leak out as a write.

        Only what the instrument actually answered is written. A readback this
        family does not implement (it is silent for :CHn:DISPlay?) leaves its
        control alone rather than appearing to report a setting it never gave.
        """
        if error is not None:
            self.log("AUTO: could not read the settings back: %s" % error, "WARNING")
            return
        values = values or {}
        depth = getattr(self, "_sync_depth", 0)
        self._sync_depth = depth + 1
        try:
            for name, fallback in (("trigger_mode", "TRIGGER_MODES"),
                                   ("trigger_source", "TRIGGER_SOURCES"),
                                   ("trigger_slope", "TRIGGER_SLOPES"),
                                   ("trigger_coupling", "TRIGGER_COUPLING"),
                                   ("acq_type", "ACQ_TYPES"),
                                   ("mem_depth", "MEMORY_DEPTHS")):
                widget = getattr(self, name, None)
                # A selector's settings come back as text; anything else is a
                # read that did not answer, and is left alone rather than shown.
                reply = self.readback_token(values.get(name))
                if widget is None or reply is None:
                    continue
                # Against the widget's own list, not the class constant: an HDS
                # takes different acquisition modes and depths from the older
                # dialect, and the widget is holding the ones it was built with.
                choices = self.choice_values(widget) or list(getattr(self.scope, fallback, ()) or ())
                if reply not in choices:
                    match = OWONScopeController.match_choice(reply, choices)
                    if match is None:
                        continue
                    reply = match
                if widget.get() != reply:
                    widget.set(reply)

            # A level is a number, not a word, so it is the one value here that
            # is not read as text: the reader has already parsed it to volts.
            level = values.get("trigger_level")
            if isinstance(level, (int, float)) and not isinstance(level, bool):
                self.sync_entry(self.trigger_level, "%g" % level)

            for channel in values.get("channels") or []:
                number = channel.get("number")
                scale = getattr(self, "ch%d_scale" % number, None)
                reply = self.readback_token(channel.get("scale"))
                if scale is not None and reply is not None:
                    # Only a reply that IS one of the readings the control offers.
                    # This family answers a dummy 0.0000e+00 for a node it does not
                    # implement, and snapping that to the nearest rung would put a
                    # real-looking 2mv on a channel that never answered.
                    choices = self.choice_values(scale) or list(self.scope.VOLTAGE_SCALES)
                    if reply not in choices:
                        matched = OWONScopeController.match_scale(reply, choices)
                        if matched is None:
                            continue
                        reply = matched
                    if scale.get() != reply:
                        scale.set(reply)

                coupling = getattr(self, "ch%d_coupling" % number, None)
                reply = self.readback_token(channel.get("coupling"))
                if coupling is not None and reply in self.scope.COUPLING_MODES:
                    if coupling.get() != reply:
                        coupling.set(reply)

                # Only a plain ON or OFF. This family is silent for :CHn:DISPlay?
                # and answers a dummy number where it has no such setting, so a
                # reply that is not one of those two words is an absence: read as
                # "off" it would untick a channel that is plainly on screen.
                display = getattr(self, "ch%d_display" % number, None)
                reply = self.readback_token(channel.get("display"))
                if display is not None and reply in ("ON", "OFF"):
                    shown = reply == "ON"
                    if bool(display.get()) != shown:
                        display.set(shown)
        finally:
            self._sync_depth = depth
        self.log("Panel shows the instrument's own settings")

    @staticmethod
    def readback_token(reply):
        """A setting as the text a control holds, or None if it did not answer.

        Text only, deliberately: every setting this readback fills is a word or a
        ladder entry, and this family answers a dummy ``0.0000e+00`` for the nodes
        it does not implement. Accepting that as a value would put a number where
        a word belongs and, worse, look like an answer - so a non-text reply is
        treated as the absence it is and leaves its control alone.
        """
        if not isinstance(reply, str):
            return None
        text = reply.strip()
        return text or None

    def read_back_panel_settings(self):
        """Ask the instrument what it is set to, and fill the controls when it lands.

        Every control the panel mirrors, not just the framing: after AUTO the
        framing values were followed and the trigger, acquisition and channel
        controls were left showing whatever they held before, which is a panel
        that has stopped describing the instrument.

        Only the channels the panel is showing are read: a column that is not on
        screen has no control to fill, and this firmware answers for more channels
        than it has.
        """
        if not self._is_connected():
            return None
        visible = [number for number, frame in getattr(self, "_channel_frames", {}).items()
                   if frame.winfo_manager()]
        return self.report_later(
            "panel_readback",
            lambda scope: self.read_panel_settings(scope, visible or None),
            self.apply_panel_settings)

    def refresh_trigger_state(self):
        self.log("Trigger and acquisition state read back.")

    # ------------------------------------------------------------------
    # Measurement statistics
    def _accum_meas_stats(self, trace):
        """Accumulate one trace measurement snapshot into the rolling stats."""
        from collections import deque
        depth = getattr(self, "_meas_stats_depth", 100)
        stats = getattr(self, "_meas_stats", {})
        for key in ("vpp", "vmax", "vmin", "vmean", "frequency"):
            v = trace.get(key)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if key not in stats:
                stats[key] = deque(maxlen=depth)
            stats[key].append(v)
        self._meas_stats = stats
        if hasattr(self, "_update_stats_display"):
            self._update_stats_display()

    def _update_stats_display(self):
        """Refresh the stats table widget if it exists."""
        import math
        widget = getattr(self, "stats_text", None)
        if widget is None:
            return
        stats = getattr(self, "_meas_stats", {})
        if not stats:
            return
        rows = []
        header = "%-12s %9s %9s %9s %9s %6s" % (
            "Metric", "Last", "Min", "Max", "Mean", "N")
        rows.append(header)
        rows.append("-" * len(header))
        UNITS = {"vpp": "V", "vmax": "V", "vmin": "V", "vmean": "V", "frequency": "Hz"}
        LABELS = {"vpp": "Vpp", "vmax": "Vmax", "vmin": "Vmin",
                  "vmean": "Vmean", "frequency": "Freq"}
        for key in ("vpp", "vmax", "vmin", "vmean", "frequency"):
            buf = stats.get(key)
            if not buf:
                continue
            vals = list(buf)
            n = len(vals)
            last = vals[-1]
            lo = min(vals)
            hi = max(vals)
            mean = sum(vals) / n
            unit = UNITS.get(key, "")
            label = LABELS.get(key, key)
            from modernlab import analysis
            fmt = analysis.format_hz if key == "frequency" else analysis.format_volts
            rows.append("%-12s %9s %9s %9s %9s %6d" % (
                label,
                fmt(last), fmt(lo), fmt(hi), fmt(mean), n))
        try:
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", "\n".join(rows) + "\n")
            widget.config(state="disabled")
        except Exception:
            pass

    def reset_meas_stats(self):
        """Clear the accumulated statistics."""
        self._meas_stats = {}
        if hasattr(self, "_update_stats_display"):
            self._update_stats_display()
        self.log("Measurement statistics cleared.")
