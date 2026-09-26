"""Bench settings: which scope to talk to, and the numbers that calibrate it.

Saved as JSON beside the app (``scope_setup.json``), because these describe one
instrument on one bench and are not source: the instrument's serial, the vertical
calibration factor, the bench reference, and the probe that is fitted.

The defaults leave the app exactly as it ships - uncalibrated against the
instrument's own readings, no particular scope chosen - so a missing or damaged
file is not a condition that needs fixing, only a note in the log.
"""

import json
import logging
import os

#: Where the settings live when no path is given. Beside the app, so it is easy to
#: find, edit, and copy to another machine.
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scope_setup.json")

#: Bumped when the meaning of a stored key changes, so an old file is understood
#: rather than silently misread.
SETTINGS_VERSION = 1

#: The probe choices the panel offers, for validating a stored value.
PROBE_CHOICES = ("X1", "X10", "X100", "X1000", "X10000")


def attached_scopes():
    """Every attached OWON USB device, or [] when the USB stack is unavailable.

    Kept here rather than imported at module scope so that importing this module -
    which the app does on start-up - cannot fail because pysusb or libusb is
    missing on a machine that only ever uses the LAN transport.
    """
    try:
        from hds_usb import list_owon_devices
        return list_owon_devices()
    except Exception as exc:                                  # pragma: no cover - env
        logging.info("could not list USB scopes: %s", exc)
        return []


def device_label(info):
    """A one-line label for a device dict from ``list_owon_devices``."""
    info = info or {}

    def clean(value):
        # Descriptors are NUL-padded on some firmware; a label with invisible
        # padding in it is a label nobody can match by eye.
        return (value or "").replace("\x00", "").strip()

    name = clean(info.get("product")) or "OWON scope"
    serial = clean(info.get("serial"))
    ids = "%04x:%04x" % (info.get("vid") or 0, info.get("pid") or 0)
    if serial:
        return "%s - serial %s (%s)" % (name, serial, ids)
    return "%s - no serial (%s)" % (name, ids)


class ScopeSetup:
    """The saved bench settings, with per-key validation on the way in."""

    #: Nothing here changes the app's shipped behaviour.
    DEFAULTS = {
        "version": SETTINGS_VERSION,
        # Which scope: its USB serial. None means "the only one, or the first".
        "usb_serial": None,
        # Volts gain trim: 1.0 leaves the decode pinned to the instrument's own
        # readings; anything else says the generator is the reference instead.
        "calibration_trim": 1.0,
        # The uncalibrated fallback, in volts per code. None keeps the value the
        # module carries, which is where it is documented.
        "reference_volts_per_code": None,
        # The probe fitted to the input. Does not scale this instrument's readings,
        # so it is recorded rather than applied.
        "probe": "X1",
        # The last amplitude a calibration was derived from, kept so the field is
        # filled in next time.
        "known_amplitude": None,
    }

    def __init__(self, values=None, path=None):
        self.path = path or DEFAULT_PATH
        self.values = dict(self.DEFAULTS)
        for key, value in (values or {}).items():
            if key not in self.DEFAULTS:
                # Dropped rather than carried: a file edited by hand or written by a
                # future version should come back out normalised, not accumulating
                # keys this build does not understand.
                logging.info("scope setup: ignoring unknown setting %r", key)
                continue
            self.values[key] = self.coerce(key, value)

    # ------------------------------------------------------------------ input
    @staticmethod
    def _positive_or_none(key, value):
        """A positive float, or None. Rejects junk rather than storing it."""
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            logging.warning("scope setup: %s is not a number (%r); ignoring it", key, value)
            return None
        if number <= 0:
            logging.warning("scope setup: %s must be positive (got %r); ignoring it",
                            key, value)
            return None
        return number

    def coerce(self, key, value):
        """Validate one stored value, warning and falling back to the default."""
        default = self.DEFAULTS.get(key)
        if key not in self.DEFAULTS:
            logging.info("scope setup: ignoring unknown setting %r", key)
            return None
        if key in ("calibration_trim", "reference_volts_per_code", "known_amplitude"):
            number = self._positive_or_none(key, value)
            if key == "known_amplitude":
                return number
            return default if number is None else number
        if key == "usb_serial":
            text = ("" if value is None else str(value)).strip()
            return text or None
        if key == "probe":
            text = ("" if value is None else str(value)).strip().upper()
            if text and not text.startswith("X"):
                text = "X%s" % text
            return text if text in PROBE_CHOICES else default
        if key == "version":
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return value

    # ------------------------------------------------------------------ state
    @property
    def calibration_trim(self):
        """The factor to apply, or None when the decode should stay untrimmed."""
        trim = self.values.get("calibration_trim") or 1.0
        return None if abs(float(trim) - 1.0) < 1e-12 else float(trim)

    @property
    def usb_serial(self):
        return self.values.get("usb_serial") or None

    def as_text(self):
        """One line for the log: what this bench is set to."""
        trim = self.calibration_trim
        return ("scope=%s, calibration=%s, reference=%s V/code, probe=%s"
                % (self.usb_serial or "first attached",
                   "untrimmed" if trim is None else "x%.6g" % trim,
                   self.values.get("reference_volts_per_code") or "module default",
                   self.values.get("probe")))

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path=None):
        """Read the settings file. Missing or unreadable is not an error."""
        target = path or DEFAULT_PATH
        try:
            with open(target, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            logging.info("scope setup: no %s yet; using defaults", target)
            return cls(path=target)
        except (OSError, ValueError) as exc:
            logging.warning("scope setup: %s could not be read (%s); using defaults",
                            target, exc)
            return cls(path=target)
        if not isinstance(stored, dict):
            logging.warning("scope setup: %s does not hold an object; using defaults", target)
            return cls(path=target)
        version = stored.get("version")
        if version not in (None, SETTINGS_VERSION):
            logging.warning("scope setup: %s was written by version %s, this build reads %s",
                            target, version, SETTINGS_VERSION)
        return cls(values=stored, path=target)

    def save(self, path=None):
        """Write the settings. Returns True on success."""
        target = path or self.path
        payload = dict(self.values)
        payload["version"] = SETTINGS_VERSION
        try:
            directory = os.path.dirname(os.path.abspath(target))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            return True
        except OSError as exc:
            logging.error("scope setup: could not write %s (%s)", target, exc)
            return False

    # ------------------------------------------------------------------ apply
    def apply(self, waveform=None):
        """Push the settings into the decode. Returns what changed, for the log."""
        if waveform is None:
            import waveform_data as waveform
        changed = []
        waveform.HDS_MANUAL_CALIBRATION_TRIM = self.calibration_trim
        changed.append("calibration %s" % ("untrimmed" if self.calibration_trim is None
                                          else "x%.6g" % self.calibration_trim))
        reference = self.values.get("reference_volts_per_code")
        if reference:
            waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X = float(reference)
            changed.append("reference %.6g V/code" % reference)
        return changed

    # ------------------------------------------------------------ calibration
    @staticmethod
    def trim_from(true_amplitude, measured, current=1.0):
        """The trim that would make ``measured`` read as ``true_amplitude``.

        ``measured`` is what the app SHOWS, so it already includes whatever trim is
        in force: the new factor multiplies the old one rather than replacing it,
        otherwise calibrating twice would undo the first correction.

        Returns None when the figures cannot support one, so the caller can say so
        rather than storing a nonsense factor. A displayed amplitude of a real
        capture is always positive, so a non-positive reading means the capture was
        flat or never happened.
        """
        try:
            true_amplitude = float(true_amplitude)
            measured = float(measured)
            current = float(current or 1.0)
        except (TypeError, ValueError):
            return None
        if measured <= 0 or true_amplitude <= 0 or current <= 0:
            return None
        return current * (true_amplitude / measured)
