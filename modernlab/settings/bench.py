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

from modernlab import analysis

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
        from modernlab.instrument.transport.usb_hid import list_owon_devices
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
        # Where an unattended recording writes, and how often the live loop asks for
        # a frame. The capture itself takes about half a second, so a shorter
        # interval changes nothing except how hard the instrument is polled.
        "record_folder": "",
        "live_interval_s": 0.5,
        # How the plot is drawn, and how the spectrum is computed. These are the
        # vendor software's display options, kept where the rest of the bench
        # settings are so they survive a restart.
        "palette": "Dark",
        "fft_window": "hanning",
        "fft_format": "dBV",
        # Each model's own calibration, keyed by the name :IDN? reports. The values
        # above are the default a model with no section of its own starts from.
        "per_model": {},
        # The last amplitude a calibration was derived from, kept so the field is
        # filled in next time.
        "known_amplitude": None,
    }

    def __init__(self, values=None, path=None):
        self.path = path or DEFAULT_PATH
        self.values = dict(self.DEFAULTS)
        # The per-model sections are nested, so the shallow copy above is not enough:
        # sharing that mapping with the class default would let one object's models
        # appear in the next one made in the same process - which is exactly how a
        # test passes alone and fails in a suite.
        self.values["per_model"] = dict(self.DEFAULTS.get("per_model") or {})
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

    #: The plot palettes; modern_lab draws one per name, and a test keeps the two
    #: lists equal rather than trusting them to stay in step.
    PALETTE_NAMES = ("Dark", "Light", "Print")

    #: The settings that belong to a MODEL rather than to a bench. A volts-per-code,
    #: a probe factor and a calibration trim describe an instrument: a second scope
    #: on the same machine must not inherit the first one's numbers, which is
    #: exactly how a plausible-looking factor ends up applied to the wrong unit.
    CALIBRATION_KEYS = ("calibration_trim", "reference_volts_per_code", "probe",
                        "known_amplitude")

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
        if key == "per_model":
            # Forgiving: a section that is not a mapping is dropped, and each value in
            # one is validated by the same rules as the top level.
            cleaned = {}
            for model, section in (value or {}).items():
                if not isinstance(section, dict):
                    continue
                entry = {}
                for sub_key, sub_value in section.items():
                    if sub_key in self.CALIBRATION_KEYS:
                        entry[sub_key] = self.coerce(sub_key, sub_value)
                if entry:
                    cleaned[str(model).strip().upper()] = entry
            return cleaned
        if key == "record_folder":
            return ("" if value is None else str(value)).strip()
        if key == "live_interval_s":
            # A floor rather than a refusal: a capture costs about half a second,
            # so anything faster is a request the instrument cannot meet anyway.
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return default
            return max(0.05, min(60.0, seconds))
        if key == "palette":
            text = ("" if value is None else str(value)).strip()
            match = [name for name in self.PALETTE_NAMES if name.lower() == text.lower()]
            return match[0] if match else default
        if key in ("fft_window", "fft_format"):
            allowed = analysis.WINDOWS if key == "fft_window" else analysis.FFT_FORMATS
            text = ("" if value is None else str(value)).strip()
            match = [name for name in allowed if str(name).lower() == text.lower()]
            return match[0] if match else default
        return value

    # ------------------------------------------------------------------ state
    # ------------------------------------------------------------------ models
    def calibration_values(self):
        """The four settings that describe an instrument, as they stand now."""
        return {key: self.values.get(key) for key in self.CALIBRATION_KEYS}

    def has_calibration(self):
        """True when any of them has been set away from its default."""
        return any(self.values.get(key) != self.DEFAULTS.get(key)
                   for key in self.CALIBRATION_KEYS)

    def use_model(self, model):
        """Adopt the settings that belong to this instrument, by name.

        A file written before models were kept apart has one set of calibration
        values and no model name in it. Rather than lose them, or apply them to
        whatever is plugged in next, they are given to the first instrument that
        identifies itself - which on a one-scope bench is the one that wrote them.
        A model with no section after that starts from the neutral defaults.
        """
        name = str(model or "").strip().upper()
        if not name:
            return None
        self.active_model = name
        sections = self.values.setdefault("per_model", {})
        if not isinstance(sections, dict):
            sections = self.values["per_model"] = {}
        if name not in sections:
            if not sections and self.has_calibration():
                # The pre-model file, and this is the instrument it describes.
                sections[name] = self.calibration_values()
            else:
                for key in self.CALIBRATION_KEYS:
                    self.values[key] = self.DEFAULTS.get(key)
                sections[name] = self.calibration_values()
        for key, value in (sections.get(name) or {}).items():
            if key in self.values:
                self.values[key] = value
        return sections.get(name)

    def remember_model(self):
        """File the current calibration under the active model, ready to be saved."""
        name = str(getattr(self, "active_model", "") or "").strip().upper()
        if not name:
            return None
        sections = self.values.setdefault("per_model", {})
        if isinstance(sections, dict):
            sections[name] = self.calibration_values()
        return sections.get(name)

    def __getattr__(self, name):
        """A stored setting reads as an attribute: ``setup.record_folder``.

        Only keys that are actually stored, so a typo raises here rather than
        handing back None from somewhere far away.
        """
        values = self.__dict__.get("values") or {}
        if name in values:
            return values[name]
        raise AttributeError(name)

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
        return ("scope=%s, model=%s, calibration=%s, reference=%s V/code, probe=%s"
                % (self.usb_serial or "first attached",
                   getattr(self, "active_model", None) or "unknown",
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
        # The calibration finally lives under the model it was measured on, so the
        # file says which instrument its numbers describe.
        self.remember_model()
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
            from modernlab.instrument.capture import waveform
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
