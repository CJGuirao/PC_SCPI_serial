# OWON HDS Scope — v1.0.0

PC oscilloscope software for **OWON HDS200 and HDS300 series** handheld scopes (USB HID).

## Installation

1. Download `OWONScope-v1.0.0-windows-x64.zip`
2. Extract anywhere — no installer needed
3. Run `OWONScope.exe`
4. Connect the scope via USB, click **Connect**

**Requires:** Windows 10/11 64-bit · libusb driver bound to the scope via [Zadig](https://zadig.akeo.ie/) (WinUSB or libusb-win32)

---

## Features

### Acquisition
- Live streaming capture at ~1.5 fps (hardware USB floor)
- RUN / STOP, CAPTURE, SINGLE trigger, AUTO frame
- Auto-reconnect watchdog after USB stall
- Software averaging (2–64 frames) to reduce noise

### Waveform display
- 12 × 8 graticule matching the instrument screen
- Calibrated voltage decode (fitted against instrument's own Vpp reading)
- Reference trace overlay (save/compare before/after)
- Trace persistence / digital phosphor (4–64 ghost frames)
- Right-click drag to zoom any region; ZOOM OUT to reset

### Live measurement overlay
17 measurements computed every frame from the captured samples — no extra USB queries:

| | | |
|---|---|---|
| Vpp · Vmax · Vmin | Vmean · Vrms | Vamp · Vtop · Vbase |
| Frequency · Period | Duty % | Width + / − |
| Rise time · Fall time | Overshoot % · Undershoot % | |

Displayed as a floating badge on the plot; slots are user-configurable.

### Analysis
- **FFT** — Hanning / Hamming / Blackman / Rectangle windows, dBV / Vrms / V output, log frequency axis, peak labels
- **Harmonic analysis + THD** — marks F, 2H…8H, shows THD % and dB
- **Math** — 8 operations: add, subtract, multiply, invert, abs, **integrate** (V·s), **differentiate** (V/s), square
- **XY / Lissajous** (two-channel scopes)
- **FFT cursors** — F1/F2 frequency + magnitude, ΔF, 1/ΔF

### Cursors
- V1/V2 vertical time cursors, H1/H2 horizontal voltage cursors
- FFT view: F1/F2 frequency cursors + M1/M2 magnitude cursors
- Cursor readout bar: ΔT, 1/ΔT, ΔV, signal level at each cursor

### Pass / Fail mask
- Set a tolerance % envelope around any captured reference
- Every live frame tested; PASS/FAIL badge + running P/F counter

### Protocol decode
Annotates decoded frames as coloured bands on the waveform plot:
- **UART** — baud auto-detect, 8N1/8E1, RS-232 invert
- **SPI** — Mode 0–3, MOSI + MISO, CS gating
- **I²C** — START/STOP/ACK/NAK, 7-bit and 10-bit addressing
- **CAN** — Classical CAN base frame, 11-bit ID, DLC + data bytes

### Multimeter (HDS271)
- DC/AC voltage, live reading, REL mode
- **DMM trend graph** — rolling history of up to 1200 readings

### Other
- Measurement statistics table (min / max / mean, rolling window)
- Scope presets — save/recall named panel configurations
- PNG image export (one click, timestamped)
- CSV / JSON / XLSX / PDF capture export
- SCPI console
- Keyboard shortcuts: Space=RUN/STOP · A=AUTO · S=SINGLE · C=CAPTURE · Z=ZOOM OUT · R=SAVE REF · I=IMG · Esc=CLEAR CURSORS

---

## Hardware notes

Verified on **OWON HDS271 firmware V1.3.0**.  
HDS200/HDS300 series should work; unverified models show a banner on connect.  
The HDS271 has no AWG, no deep-memory download, and no hardware autoset command — the software works around all three in software.
