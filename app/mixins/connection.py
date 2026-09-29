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
        """Show the field the selected transport actually uses.

        USB finds the instrument by VID/PID, so there is nothing to type: the
        address box is removed and replaced by a plain hint. Choosing LAN brings
        the address back in the same place, before the Connect button, plus a
        note that the LAN/SDS path is legacy and not re-verified.
        """
        for widget in (self.conn_address, self.conn_hint,
                       getattr(self, "conn_lan_note", None)):
            if widget is not None:
                widget.pack_forget()
        if self.conn_type.get().strip().lower() == "usb":
            self.conn_hint.pack(side="left", padx=5, before=self.connect_btn)
        else:
            self.conn_address.pack(side="left", padx=5, before=self.connect_btn)
            lan_note = getattr(self, "conn_lan_note", None)
            if lan_note is not None:
                lan_note.pack(side="left", padx=(0, 5), before=self.connect_btn)

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
        attached = _attached_scopes()
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
            self.update_state_indicator()
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
