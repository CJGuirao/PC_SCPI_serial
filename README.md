# OWON HDS Oscilloscope — PC Software

A Python PC oscilloscope for **OWON HDS200 and HDS300 series** handheld scopes, connected over USB.

> **Verified hardware:** HDS271, firmware V1.3.0. HDS300-series and other HDS200 models share the same command structure and will work; the app logs an "unverified model" notice on connect.

---

## Features

### Acquisition
- Live streaming capture at ~1.5 fps (USB HID hardware floor)
- RUN / STOP, CAPTURE, SINGLE trigger, AUTO frame (software, from measured frequency)
- Auto-reconnect watchdog after USB stall
- Software averaging (2–64 frames) to reduce noise

### Waveform display
- 12 × 8 graticule matching the instrument screen
- Calibrated voltage decode (fitted against the instrument's own Vpp reading)
- Reference trace overlay — save/compare before and after
- Trace persistence / digital phosphor (4–64 ghost frames)
- Right-click drag to zoom any region; ZOOM OUT to reset

### Live measurement overlay
17 measurements computed every frame from the captured samples — no extra USB queries:

| | | |
|---|---|---|
| Vpp · Vmax · Vmin | Vmean · Vrms | Vamp · Vtop · Vbase |
| Frequency · Period | Duty % | Width + / − |
| Rise time · Fall time | Overshoot % · Undershoot % | |

Displayed as a floating badge on the plot; slots are user-configurable (MEASURE drawer → ON-PLOT).

### Analysis
- **FFT** — Hanning / Hamming / Blackman / Rectangle, dBV / Vrms / V, linear or log frequency, peaks labelled
- **Harmonic analysis + THD** — marks fundamental and 2H–8H, shows THD % and dB
- **Math** — 8 ops: add, subtract, multiply, invert, abs, integrate (V·s), differentiate (V/s), square
- **XY / Lissajous** (two-channel scopes)

### Cursors
- V1/V2 time cursors and H1/H2 voltage cursors
- FFT view: F1/F2 frequency + M1/M2 magnitude cursors
- Readout bar: ΔT, 1/ΔT, ΔV, signal level at each cursor

### Pass / Fail mask
- Set a tolerance % envelope around a reference capture
- Every live frame tested; PASS/FAIL badge + running P/F counter on the plot

### Protocol decode
Decoded frames shown as coloured bands on the waveform:
- **UART** — baud auto-detect, 8N1/8E1, RS-232 invert
- **SPI** — Mode 0–3, MOSI + MISO, CS gating
- **I²C** — 7-bit and 10-bit addressing, START/STOP/ACK/NAK
- **CAN** — Classical CAN base frame, 11-bit ID

### Multimeter (HDS271)
- DC/AC voltage, live reading, REL mode
- DMM trend graph — rolling history up to 1200 readings

### Other
- Measurement statistics table (min / max / mean, rolling window)
- Scope presets — save/recall named panel configurations
- PNG image export (one click, timestamped, I key)
- CSV / JSON / XLSX / PDF waveform export
- SCPI console
- Keyboard shortcuts: Space=RUN/STOP · A=AUTO · S=SINGLE · C=CAPTURE · Z=ZOOM OUT · R=SAVE REF · I=IMG · Esc=CLEAR CURSORS

---

## Requirements

- Windows 10/11 64-bit
- Python 3.9+
- numpy, matplotlib, pillow, pyusb, libusb-package, pypdf
- tkinter (bundled with CPython on Windows)
- **libusb driver** bound to the scope via [Zadig](https://zadig.akeo.ie/) — select the OWON device (VID 5345 / PID 1234) and install WinUSB or libusb-win32

---

## Installation from source

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

### Run

```bat
set "TCL_LIBRARY=" && set "TK_LIBRARY=" && .venv\Scripts\python.exe main.py
```

```bash
# Git Bash
unset TCL_LIBRARY TK_LIBRARY && ./.venv/Scripts/python.exe main.py
```

> The TCL_LIBRARY / TK_LIBRARY variables must be cleared — some installed software exports them machine-wide, which prevents tkinter from starting.

---

## Portable release

Download **OWONScope-vX.Y.Z-windows-x64.zip** from the [Releases](../../releases) page, extract anywhere, and run OWONScope.exe. No installation needed.

---

## Tests

427 tests, headless (no instrument attached):

```bash
unset TCL_LIBRARY TK_LIBRARY
./.venv/Scripts/python.exe -m unittest discover -s tests -t .
```

---

## Project structure

```
main.py                    launcher
ui/                        front panel (widgets, dialogs, ModernLabUI)
app/                       App and its instrument mixins
  mixins/                  connection, controls, dmm, framing, views,
                           readback, autoset, recording, files
modernlab/
  instrument/transport/    USB HID transport (pyusb)
  instrument/capture/      payload to volts (calibration, decode)
  instrument/dialect/      HDS SCPI command tables
  analysis/                spectrum, math, cursors, measurements
  decode/                  UART / SPI / I2C / CAN protocol decoders
  storage/                 CSV, JSON, XLSX, PNG, PDF export
  settings/                bench configuration
  app/                     io_worker - single instrument owner
tools/                     diagnostics, calibration helper
tests/                     427-test headless suite
doc/                       ARCHITECTURE.md, SCPI_COVERAGE.md
```

---

## USB interface notes

Verified on HDS271 firmware V1.3.0. The instrument is VID 0x5345 / PID 0x1234, composite:

| Interface | Class | Endpoints | Role |
|---|---|---|---|
| 0 | HID (0x03) | 0x81 IN / 0x01 OUT interrupt, 64-byte reports | ASCII SCPI |
| 1 | Mass storage (0x08) | 0x82 IN / 0x02 OUT bulk | 8 MB volume — unused |

Key facts learned on hardware:

- **Samples wrap at 255.** The offset can push a signal across the 0/255 boundary; the decoder un-wraps before converting to volts.
- **Volts/div label is cosmetic.** Calibration pins decoded extremes to the instrument's own MIN/MAX/Vpp readings.
- **Volts/div and vertical position cannot be written over USB.** Both are accepted and ignored. The timebase write is live.
- **AUTO key is not reachable over USB.** Nine candidates were probed; none changed any setting. The AUTO button frames the time axis from the measured frequency.
- **300 samples per frame** via :DATa:WAVe:SCReen:CH1? at ~1.5 fps. Deep-memory download is silent on this firmware.
