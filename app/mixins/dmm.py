"""The multimeter: function, mode, relative and the live reading."""

from modernlab.app.io_worker import PRIORITY_REFRESH


class DmmMixin:
    """Multimeter controls for the OWON panel."""

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
        # Feed the DMM trend buffer.
        self._push_dmm_trend(float(reading))

    # ------------------------------------------------------------------
    # DMM trend graph
    def _push_dmm_trend(self, value):
        """Add one reading to the rolling DMM trend buffer and refresh the graph."""
        from collections import deque
        depth = getattr(self, "_dmm_trend_depth", 300)
        buf = getattr(self, "_dmm_trend_buf", None)
        if buf is None:
            self._dmm_trend_buf = deque(maxlen=depth)
            buf = self._dmm_trend_buf
        buf.append(value)
        self._draw_dmm_trend()

    def _draw_dmm_trend(self):
        """Redraw the DMM trend mini-plot."""
        ax = getattr(self, "_dmm_ax", None)
        canvas = getattr(self, "_dmm_canvas", None)
        buf = getattr(self, "_dmm_trend_buf", None)
        if ax is None or canvas is None or not buf:
            return
        import numpy as np
        vals = np.array(list(buf), dtype=float)
        ax.clear()
        ax.set_facecolor("#101719")
        ax.tick_params(labelsize=6, colors="#7a9090")
        for spine in ax.spines.values():
            spine.set_edgecolor("#2a4040")
        ax.plot(vals, color="#9cdc9c", linewidth=0.9)
        if vals.size >= 2:
            lo, hi = vals.min(), vals.max()
            pad = max(abs(hi - lo) * 0.1, 1e-6)
            ax.set_ylim(lo - pad, hi + pad)
        ax.axhline(float(vals[-1]), color="#ffd166", linewidth=0.6, linestyle=":")
        try:
            canvas.draw_idle()
        except Exception:
            pass
