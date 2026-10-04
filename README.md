# OWON HDS Scope

PC oscilloscope software for OWON HDS200 and HDS300 series handheld scopes.  
Connect via USB and get a full-featured scope display on your PC — better than the software that ships with the instrument.

> Tested on HDS271 firmware V1.3.0. Other HDS200/300 models should work.

---

## Quick start

1. Install the libusb driver with [Zadig](https://zadig.akeo.ie/) — select your OWON scope and choose **WinUSB**
2. Download the latest release from the [Releases](../../releases) page
3. Unzip and run **OWONScope.exe** — no installation needed
4. Click **Connect**, then **LIVE**

---

## What it does

**Waveform**  
The main display shows a calibrated waveform with a proper 12×8 graticule matching what you see on the scope screen.
Voltage is decoded against the instrument's own readings, not the cosmetic volts/div label.

**Live measurements overlay**  
Frequency, period, Vpp, RMS, duty cycle, rise time and more are computed from every captured frame and shown as a badge on the plot. You choose which measurements to display (up to 8 slots).

**FFT**  
Spectrum view with Hanning/Hamming/Blackman/Rectangle windows, dBV/Vrms/V output, linear or log frequency axis, peak labels. Enable **THD** to identify harmonics and get a Total Harmonic Distortion readout.

**Math**  
Eight operations on the waveform: add, subtract, multiply, invert, abs, **integrate**, **differentiate**, square.

**Cursors**  
Time and voltage cursors with ΔT, 1/ΔT and ΔV readout. In FFT view they switch to frequency and magnitude automatically.

**Pass / Fail mask**  
Capture a reference waveform, set a tolerance %, and the scope tests every live frame against the envelope — useful for production checks.

**Protocol decode**  
Click DECODE to decode digital signals directly from the waveform: UART, SPI, I²C, CAN. Decoded bytes appear as labelled bands on the plot.

**Multimeter**  
The HDS271's built-in DMM is shown in the Measurements panel, with a live trend graph that tracks readings over time.

**Export**  
Save the waveform as CSV, JSON, Excel, PNG or PDF. One-click timestamped PNG screenshot with the **I** key.

**Presets**  
Save and recall named panel configurations (timebase, scale, trigger settings) from the Utility drawer.

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| Space | RUN / STOP |
| A | AUTO frame |
| S | SINGLE capture |
| C | CAPTURE one frame |
| Z | Zoom out |
| R | Save reference trace |
| I | Save plot image |
| Esc | Clear cursors |

---

## Hardware requirements

- OWON HDS200 or HDS300 series scope connected by USB
- Windows 10 or 11, 64-bit
- libusb driver bound to the scope via [Zadig](https://zadig.akeo.ie/) (WinUSB)

---

## Running from source

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
unset TCL_LIBRARY TK_LIBRARY
.venv/Scripts/python.exe main.py
```

---

## Known limits

The hardware sets a ceiling on what's possible:

- ~1.5 fps live capture (USB HID floor — 300 samples per frame over a 64-byte endpoint)
- 8-bit vertical resolution
- No AWG/signal generator on the HDS271
- Hardware AUTO and trigger force not reachable over USB — both are implemented in software
