# Vendor software review — OWON "Oscilloscope Software" (DS_Wave / HOS)

What the software that ships with the instrument does, what is worth taking into
this project, what is not, and what has to be proven on the HDS271 before any of it
is promised in the UI.

## What was inspected, and what could not be

| | |
|---|---|
| Path | `C:\Program Files (x86)\DS_Wave\HOS` |
| Application | `com.owon.uppersoft.hds` **1.2.11** (build 2024-09-27, `1.2.11.v202409271425`), Eclipse RCP, Java, 205 MB with a bundled JRE |
| Evidence used | its own i18n label tables (**202** strings in `OSCMsgLib_en.properties`, **187** in `OSCMsgLib2_en.properties`), the per-model FFT parameter tables, the class inventory of both plugins (612 + 134 entries), `pref/default.ini`, `plugin.xml` |
| Evidence not available | `Locale\en\Oscilloscope_en.pdf`, the software's own manual: the file is vendor-encrypted (header `88 7D 1C B3`, not `%PDF`), so this review is written from the binaries rather than the manual |
| Not done | I did not run it against the instrument. It claims the same single-owner HID endpoint as this app, and two owners corrupt each other's replies — so this review makes no claim about the quality of its decode. |

The app's own layout: `communication/` (USB via `ch.ntb.usb` **0.5.9** plus a private
"rapid" bulk channel, serial via `RXTXcomm` **2.1.7**, LAN), `manipulate/` (the
remote-control panels), `chart/` (drawing, FFT, math), `data/`, `autoplay/`,
`scpi/` (20 classes), with `jxl 2.6.6` for `.xls` export and SWT 3.108 for the UI.
`default.ini` dates the shared framework to 2015 and lists 10 locales.

Its transports are the same three this project offers — USB, serial, LAN — and it
independently arrived at the same conclusion about USB that we measured: it has a
bulk "rapid" path alongside the ordinary one.

## The three pillars

1. **Capture and analyse** — waveform, FFT, math, cursors, data table, XY, and a
   "WaveForm Info" panel.
2. **File and export** — `.bin` waveform, `.csv` (deep memory), `.xls`, `.bmp`
   screen, print and print preview.
3. **Remote control and automation** — a panel per subsystem (channels, timebase,
   trigger, sample, FFT, multimeter, generator, network), a SCPI console,
   "Continue Data Download", and a waveform player.

---

## Tier A — client-side, portable now (no new instrument support needed)

### A1. Measurement cursors — the highest-value item for this bench

*Theirs:* a cursor panel with a type selector, markers (All / Horizontal / Vertical
/ None) and a divisions field — `Center.cursor`, `Center.cursorType`,
`Center.markAll|markH|markV|markNone`, `Center.num`, `Center.zoom`.

*Ours:* `App.refresh_cursors` annotates the **trigger level** and the **horizontal
position** — instrument state, not measurement cursors. There is no way to put a
marker on a point of the trace and read it.

*The gap this closes:* the person at this bench is reading amplitude and time off
the grid **by eye**, which is exactly what the absolute-code model made
trustworthy and what a cursor pair would make exact. Two vertical markers give ΔV
and the level of each; two horizontal markers give ΔT, 1/ΔT, and the time of each.

*Where:* the marker maths belongs in `waveform_data.py` beside the rest of the
decode; the interaction in `modern_lab.py`; the state in `main.py`.

*Verify:* a synthetic capture (a known 25 Vpp, 1 kHz sine) — put a marker on the
peak and the trough, assert 25.000 V and 1.0000 ms.

### A2. An FFT display

*Theirs:* computed **in Java on the PC**, not on the instrument —
`chart/model/fft/`: `FFTUtil`, `FFTFunction`, `InplaceFFT`, `Complex`,
`DbFFTDrawCompute`, `VrmsFFTDrawCompute`, `IFFTCompute`, `WndType`,
`FFTWaveFormCurve`, and `TimeBaseRef` feeding per-model parameter tables. Windows:
**Rectangle, Hanning, Hamming, Blackman**. Formats: dB, Vrms, and back **To
Original WaveForm**. Axes: freq/div and dB/div, with the full-scale ladder running
from **250 MHz/DIV down to 0.0025 Hz/DIV** in 1-2-5 steps.

Those parameter tables are keyed by model, and the HDS ones are `hds1022mn.txt`
(HDS1000 series), `hds2062mn.txt` (**HDS200 series** — this instrument's family) and
`hds3102mn.txt` (HDS300 series), alongside the MSO, PDS and SDS sets. **There is no
HDS271 entry**, so even the vendor's own software has to fall back to a family
table for this instrument — which is precisely the case our decode avoids by
measuring `point_interval` on the capture we actually received.

*Ours:* nothing. But `numpy 2.5.3` is already a dependency, and the decode already
carries `point_interval` — seconds per point — on every capture. So the FFT is
arithmetic on data we already have.

**The one thing to get right**, and the reason this needed measuring rather than
assuming: derive the frequency axis from **`point_interval` (what was actually
received)**, never from the instrument's announced sample rate. Measured on the
HDS271 just now:

```
model=HDS271  runstatus=TRIG  datatype=SCREEN
announced sample rate : '1MSa/s'
capture               : 300 points, point_interval 2e-05 s -> 50 kSa/s
timebase              : 0.0005 s/div (500 us/div), 12 divisions = 6 ms
```

The instrument samples at 1 MSa/s and sends 300 points: the useful record is
**6 ms at 50 kSa/s**, so **Nyquist is 25 kHz and one bin is ≈167 Hz**. An FFT
labelled from the announced `1MSa/s` would put every peak 20× too high — the same
class of mistake as reading the volts/div label, and it is worth a test that pins
it.

*So:* a real spectrum analyser for line frequency, audio, PWM and switching work;
honestly not an RF instrument, and the panel should say so rather than let a
20 µs sample interval imply more than it can deliver.

*Note:* the HDS271 has no on-instrument FFT to switch on, so client-side is the
only route to this feature — it is not a matter of finding the right SCPI node.

*Verify:* a synthetic 3 kHz sine in a 50 kSa/s record through a Hanning window —
assert one peak, at 3000 Hz, within a bin.

### A3. Math traces

*Theirs:* `chart/model/math/Arithmetic`, `IMath`, `MathWaveFormCurve`,
`data/math/MathWF`, with `Center.tomath`, `Center.torms` (to Vrms) and
`Center.todb` (to dB).

*Ours:* nothing. *To build:* CH1±CH2, CH1×CH2, invert, and the two transforms.
Client-side, same shape as the FFT work, and a fifth curve on the same axes.

### A4. Unattended logging

*Theirs:* "Continue Data Download" with a **Loop delay** bounded at 10 ms / 2000 ms
/ 100000 ms (min / default / max, from `default.ini`), auto-saving each capture to
a chosen directory as `.bin`, `.csv` or `.bmp`, plus a warning that the file count
is limited by the filesystem and that the instrument's own storage should be off.

*Ours:* live capture exists, but nothing writes to disk on its own.

*To build:* a "record to folder" option in SETUP with an interval. Our floor is
higher than theirs — a capture costs ≈454 ms here — so the interval should start
around 500 ms and the panel should show what was actually achieved, not what was
asked for.

### A5. Export breadth

*Theirs:* `.csv` (and its own note that deep-memory data needs CSV because only
the current page fits in a spreadsheet), **`.xls`** via the bundled `jxl`, `.bin`,
`.bmp`, print and print preview with paper-background and whole-page options.

*Ours:* CSV and JSON from `App.save_waveform`.

*To build, cheapest first:* a **PNG of the plot**, a **PDF/print** through
matplotlib's `PdfPages`, then `.xlsx`. The export should carry the settings that
made the numbers true — scale, probe, timebase, calibration trim — because a CSV
with no provenance is exactly the file nobody can check later.

### A6. Waveform player

*Theirs:* `AutoPlay` — open a folder of recordings, play with a time delay,
reverse, page. *Ours:* none. A player doubles as our offline harness: the same
screen, fed from a file, is how a plot bug gets reproduced without the bench.

### A7. Data table

*Theirs:* `data/DataTableDialog` — a virtualised per-channel table of sample values
with select-all/select-none and Save As. *Ours:* CSV export only. The table is a
nicer home for the cursor readouts than the log is.

### A8. XY (Lissajous) mode

*Theirs:* `MF.waveXY`, with an explicit guard — "ch1 or ch2 is not available, XY
mode is not supported!". We capture both channels, so this is a plotting exercise.

### A9. Display options

*Theirs:* background colour, grid colour, grid lines on/off, data line, data point,
and a 4k window. *Ours:* a fixed dark theme, deliberately. Low priority — worth
revisiting only if a light, printer-friendly palette is wanted.

### A10. Per-model parameter tables — the structural lesson

Theirs are files in `chart/model/fft/params/`, one per model, loaded by
`TimeBaseRef`. Ours is a single `scope_setup.json` for whoever is at the bench.

**This is the pattern that would have prevented the worst bugs in this project.**
The volts-per-code, the probe factor and the sample rate are properties of a
*model*, not of a bench session: our 10X/1X confusion, and the "50 V/div" axis that
disagreed with a `5v` label, both came from treating an assumption as universal.

*To build:* key the saved settings by the instrument's own identity (`:IDN?`
already gives `OWON,HDS271,25520161,V1.3.0`) so a second scope on the bench cannot
inherit the first one's trim — and so a per-model note can record what has actually
been measured on that model.

---

## Tier B — instrument-side; prove each write on the HDS271 before it reaches the UI

The standing rule this project earned: **one write, then read it back, on this
unit, before it appears in the interface.** The volts/div experiments are the
cautionary tale — a plausible-looking write that was inert *and* corrupted the
instrument's own readout.

* **B1. Trigger panel.** Theirs: Edge / Pulse / Slope / Video, Auto / Normal /
  Single / Alternate, holdoff, high/low levels, coupling — plus the warning "In
  trigger ALT, the command single and normal is invalid". Ours: read-only getters,
  and a trigger-level node that has answered erratically. A read panel is safe
  today; writes need per-command verification.
* **B2. Channel and sample panels.** Coupling, probe rate, reverse, voltbase;
  sample Average / Peak detect. Same rule. **Volts/div stays guidance-only on this
  unit** — measured inert and corrupting.
* **B3. Autoset, Set 50%, Set 0, Force trigger, Run/Stop, Self Correct.** Theirs
  are buttons, and self-correct carries "may take a few minutes ... don't do any
  operations until it's done". Ours has no SCPI autoset. Worth offering as a
  *software* autoset instead: the timebase write **is** live on this unit, so we
  can frame the time axis ourselves and tell the person to press the instrument's
  own AUTO for the vertical — which is honest, and useful.
* **B4. Multimeter panel.** Theirs is a full panel — DCV, ACV, DCA, ACA, R, C,
  diode, buzzer, close, autoscale, read — plus its own SCPI console, and the guard
  "Multimeter SCPI is not support for the machine". Ours already reads
  `:DMM:MEAS?` and configures DC volts. The modes the HDS271 actually answers are
  worth finding one at a time, because a DMM panel is a genuinely useful thing to
  have on the same screen as the trace.
* **B5. Network settings.** Theirs: IP, gateway, submask, port, and a reboot — with
  the warning that rebooting stops the data download. This app already offers a LAN
  address field, so the question is only whether the HDS271's LAN dialect exposes
  the read/write nodes. Test, then decide.
* **B6. Deep memory and stored files.** Theirs: "High Memory Depth" on getting, and
  a browser for files stored in the instrument with checkboxes, retries and a
  destination path. Our capture reports `datatype=SCREEN` with 300 points, and
  earlier probing found the bulk read empty — so deep memory is not offered by this
  unit. Two things are worth keeping anyway:
  * the wording of their caveat — "Only CURRENT PAGE of data is saved out because
    of Deep Memory, you can export all data out as '.csv' file to instead" — which
    tells the truth about a lossy export instead of hiding it;
  * their error text exposing an instrument setting that changes what a download
    *is*: "[DISP SET]-[Carry]-[Vectors]" decides between a waveform and an image.
    Worth testing on ours, because a mode that silently changes the payload is
    exactly the kind of thing that looks like a decoder bug.
* **B7. A SCPI console.** Theirs: `MF.scpi`, `SendCmdWin`, with the note "This
  command does not support the old machine" and a `CMDAvailMode` setting. Ours:
  none — and this is the cheapest high-value item in Tier B, because a console that
  sends a query and shows the raw reply is precisely how every fact in
  `doc/SCPI_COVERAGE.md` was established. It should ship read-only by default, with
  the destructive-write warning we have already learned to respect.

---

## Tier C — not applicable, and why

* **The generator panel** (`Mp.*`: sine, square, ramp, DC, noise, arbitrary,
  AM/FM/PM/PWM/FSK, sweep, burst, counter). The HDS271 has no built-in generator;
  that panel is for the AG and MSO models. Nothing to take except the layout.
* **Their private "rapid" USB bulk protocol and its `.bin` container.** Adopting it
  would tie us to their implementation and cost the portability across HDS models
  that the documented SCPI path gives us.
* **RCP/p2 self-update and the update server.** Irrelevant here.
* **Their PDF manual.** Encrypted, so it cannot be shipped or quoted.

---

## What this project already does that theirs does not

* Decodes from the capture's **own absolute codes**, with a per-bench trim
  (verified: 25.000 Vpp for a 25 Vpp signal), so a cosmetic volts/div label cannot
  mislead the plot.
* Follows a front-panel change within about a second, via the framing watch.
* Documents a measured transport model — 300 points, 20 µs per point, 64-byte HID
  transfers, one owner of the endpoint — rather than a reverse-engineered one.
* 194 tests, including the ones that pin the vertical model and the framing rules.

---

## Suggested order

All of Tier A before any of Tier B, since Tier A needs nothing proven on the
instrument:

1. Measurement cursors (A1) — closest to what the bench actually does by hand.
2. FFT display (A2) — with the axis from `point_interval`, and a test that pins it.
3. Export breadth (A5) — PNG, PDF, then XLSX, each carrying its provenance.
4. Math traces (A3).
5. SCPI console (B7) — read-only first.
6. Data table (A7).
7. Unattended logging (A4).
8. Waveform player (A6).
9. Per-model settings (A10).
10. Multimeter modes (B4) — one mode at a time, verified.
11. XY mode (A8).
12. Trigger read panel (B1), then guarded writes.

## How each one gets accepted

* **Client-side (Tier A):** a unit test on a synthetic capture with a known answer —
  a sine that must peak at its own frequency, a pair of markers that must read a
  known Vpp and period.
* **Instrument-side (Tier B):** the command is sent, read back, and only then
  offered in the UI; anything that changes instrument state gets a warning first,
  and anything that has ever corrupted a readout is documented as such.
