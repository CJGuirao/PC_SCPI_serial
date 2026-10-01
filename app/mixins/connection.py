"""Connection handling: opening, identifying and closing a transport."""

from tkinter import messagebox

from modernlab.settings.bench import attached_scopes


def _attached_scopes():
    """Look up attached_scopes the way the old single-file app did.

    It used to be a module global of main, so ``patch("main.attached_scopes")`` in the
    tests rebound the exact name connect_scope and open_setup read. After the split the
    name lives here, where such a patch does not reach, so it is resolved through the
    imported namespace at call time instead - a plain ``import main`` at module scope
    would be a circular import (main imports this module).
    """
    import sys
    launcher = sys.modules.get("main")
    getter = getattr(launcher, "attached_scopes", None)
    return getter() if callable(getter) else attached_scopes()


class ConnectionMixin:
    """Connection handling for the OWON panel."""

    def sync_connection_fields(self):
        """No-op: only USB is supported; nothing to switch."""

    def toggle_connection(self):
        if self._is_connected():
            self.disconnect_scope()
        else:
            self.connect_scope()

    _VERIFIED_MODELS = {
        ("HDS271", "V1.3.0"),   # the bench unit all features were measured against
    }

    def adopt_model(self):
        """Switch to the settings that belong to whatever just identified itself.

        Called after *IDN?, because the calibration, the probe and the bench
        reference describe an INSTRUMENT, and a bench can have more than one.

        Logs an unverified-model notice when the connected unit is outside the
        set of models that were actually tested, so the user knows what they
        are working with.
        """
        model = ""
        try:
            model = self.scope.model or ""
        except Exception:
            model = ""
        if not model:
            return None
        firmware = ""
        try:
            firmware = self.scope.firmware or ""
        except Exception:
            pass
        # Check against the verified list.  Log once on connect so the notice
        # is in the session log and in the status bar, but does not block use.
        if (model, firmware) not in self._VERIFIED_MODELS:
            self.log(
                "Unverified model: %s firmware %s. "
                "Every feature was measured on HDS271 V1.3.0; behaviour on other "
                "models or firmware versions is untested. If something does not "
                "work, please report it with the model and firmware version."
                % (model or "unknown", firmware or "unknown"),
                "WARNING"
            )
            self.status_var.set(
                "Connected — %s %s (unverified model; see log)" % (model, firmware)
            )
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
        """Connect to the OWON scope via USB HID (VID/PID 5345:1234)."""
        chosen = self.setup.usb_serial
        attached = _attached_scopes()
        if len(attached) > 1:
            self.log("%d OWON USB devices attached; using %s. Pick one in SETUP."
                     % (len(attached), chosen or "the first found"))
        elif not attached:
            self.log("No OWON USB device found. Is the scope powered and plugged in?", "WARNING")

        self._set_status("Connecting\u2026")
        self.connect_btn.config(text="Connecting\u2026", state="disabled")
        self.report_later("connect",
                          lambda scope: self.open_transport(scope, chosen),
                          lambda answer, error=None: self.finish_connect(answer, error))

    @staticmethod
    def open_transport(scope, chosen):
        """Open USB HID and identify the instrument. On the worker."""
        if chosen and getattr(scope, "is_hds", False):
            opened = scope.connect_usb_hid(serial=chosen)
            if opened:
                scope.identify_model()
        else:
            opened = scope.connect_usb("auto", 115200)
        if not opened:
            return False, ""
        try:
            return True, (scope.get_idn() or "")
        except Exception:
            return True, ""

    def finish_connect(self, answer, error=None):
        """Wire the panel up to the instrument that answered, or say that it did not."""
        self.connect_btn.config(state="normal")
        opened, idn = (answer if isinstance(answer, tuple) else (bool(answer), ""))
        if error is not None or not opened:
            self.connect_btn.config(text="Connect")
            if error is not None:
                self.log(f"Connection failed: {error}", "ERROR")
                messagebox.showerror("Connection Error",
                                     f"Failed to connect via USB\n{error}")
            else:
                messagebox.showerror("Connection Error",
                                     "Failed to connect via USB.\n"
                                     "Is the scope powered and plugged in?")
            self._set_status("Connection failed")
            self.update_state_indicator()
            return

        self.connect_btn.config(text="Disconnect")
        # What this instrument is decides which calibration applies to it.
        self.adopt_model()
        self.device_info.set(idn or "Connected via USB")
        self._set_status("Connected via USB")
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
        # Live as soon as it answers. This is a panel for watching a scope: the
        # first thing anyone does after connecting is press LIVE, and the first
        # frame arrives sooner if the panel asks for it itself.
        self.update_state_indicator()
        if not self.auto_refresh_var.get():
            self.toggle_live()

    def disconnect_scope(self):
        # Stop the live loop first: it would otherwise go on asking an instrument
        # that is no longer there, and would keep the indicator saying LIVE.
        if self.auto_refresh_var.get():
            self.toggle_live()
        self.scope.disconnect()
        self.connect_btn.config(text="Connect")
        self.device_info.set("Not Connected")
        self.status_var.set("Ready | Disconnected")
        self.capture_state.set("NO ACQUISITION")
        self.update_state_indicator()
        self.log("Disconnected")
