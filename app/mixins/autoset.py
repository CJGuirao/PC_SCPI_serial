"""Auto range and software autoset: bring a signal into frame."""

from modernlab import analysis


class AutosetMixin:
    """Auto range and software autoset for the OWON panel."""

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
        # the sort of thing this firmware returns. The guard is what makes that
        # true rather than merely intended: these are the panel's own controls
        # and they apply what they hold, so a value put in one here would be on
        # its way to the instrument as a write the moment the guard lifts.
        depth = getattr(self, "_sync_depth", 0)
        self._sync_depth = depth + 1
        try:
            if "timebase" in (report.get("applied") or []):
                self.timebase_scale.set(report["timebase"])
            if "trigger level" in (report.get("applied") or []):
                level = self.scope.parse_scale(report.get("trigger_level_readback"))
                if level is not None:
                    self.sync_entry(self.trigger_level, "%g" % level)
        finally:
            self._sync_depth = depth
        for note in report.get("notes") or []:
            self.log("AUTO: " + note)
        # Then the rest of the panel: the framing above is only the half AUTO
        # moves here, and a control left showing what it held before AUTO is a
        # panel that has stopped describing the instrument. The instrument is
        # asked what it is set to NOW, after the framing work, and the answer is
        # written into the controls without a write going back.
        self.read_back_panel_settings()

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
