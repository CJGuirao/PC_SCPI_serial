# HDS200/300 SCPI coverage

What of `doc/HDS200_Series_SCPI_Protocol.pdf` this program actually implements, and
what the instrument in front of it actually answers.

Checked against **OWON HDS271, serial 25520161, firmware V1.3.0** (`*IDN?` →
`OWON,HDS271,25520161,V1.3.0`), one node at a time, read-only.

**HDS300-series and other HDS200 models:** the same command structure applies and the
app will work, but no HDS300 unit was available for testing.  The app logs an
"unverified model" notice on connect when the model/firmware is outside the confirmed
list.  Everything in this table is specific to the HDS271 at V1.3.0.

Reading the manual is not enough on this firmware, and the second half of this
table is the reason: whole subsystems the manual documents for the series are
absent from this instrument, and a node that is documented but unsupported
answers *nothing* rather than replying with an error. So each entry below is
marked with what was measured, not with what the PDF says.

| Mark | Meaning |
| --- | --- |
| **yes** | answers on the HDS271 and the program uses it |
| **yes (unused)** | answers, and the program can ask, but nothing in the UI needs it yet |
| **silent** | documented for the series, no answer at all from this instrument |
| **inert** | accepts the write, keeps ignoring it; the program reports the readback instead of claiming success |

## Root commands

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `*IDN?` | yes | `connect_scope`, device line |
| `*RST` | yes | UTILITY ▸ Reset scope |
| `:MODEL?` | yes | model fallback in `identify_model` |

## Channels — `:CH1:*` (`:CHANnel1:*` does not exist)

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:CH1:SCALe` / `?` | yes / **label only** | VERTICAL ▸ VOLTS/DIV. The readback follows the write, but the samples do not: one unchanged signal returned identical codes (113..149) at 200 mV, 500 mV, 1 V and 2 V/div, while the instrument's own Vpp reading scaled with the label. The write moves the label, and the zoom the instrument's screen applies to it — see "Display framing" below. |
| `:CH1:COUPling` / `?` | yes | VERTICAL ▸ Coupling |
| `:CH1:PROBe` / `?` | yes | VERTICAL ▸ Probe (X10 on this instrument) |
| `:CH1:OFFSet` / `?` | yes | VERTICAL ▸ POSITION (V), live |
| `:CH1:DISPlay` / `?` | **inert** / silent | CH1 DISPLAY button (send-only) |
| `:CH2:*` | silent | channel 2 does not exist here: the column is hidden and the probe is what decides |

Note the manual spells the channel nodes `:CHANnel<n>:`. This firmware does not
answer that spelling at all; `:CH1:` is the one that works.

## Horizontal

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:HORizontal:SCALe` / `?` | yes | HORIZONTAL ▸ TIME / DIV; verified against the trace (5 ms → 60 cycles, 500 µs → 6) |
| `:HORizontal:OFFSet` / `?` | yes | HORIZONTAL ▸ POSITION (div), entered in divisions, live; drawn as `HPOS` over the X axis |

## Trigger

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:TRIGger:SINGle:SWEEp` / `?` | yes | TRIGGER ▸ Mode (`AUTo` reads back) |
| `:TRIGger:SINGle:EDGE:SOURce` / `?` | yes | TRIGGER ▸ Source |
| `:TRIGger:SINGle:EDGe` / `?` | yes | TRIGGER ▸ Slope (`RISe`/`FALL`) |
| `:TRIGger:SINGle:EDGE:LEVel` / `?` | **inert** | TRIGGER LEVEL (V) and its cursor; writes of 0/1/−1/5 V all left the readback at 40.0 mV and the capture bit-identical, so the cursor is drawn from the instrument's own readback and a write that the instrument does not confirm is logged as unconfirmed |
| `:TRIGger:SINGle:COUPling` / `?` | yes | TRIGGER ▸ Coupling |
| `:TRIGger:STATus?` | yes | logged on connect (`TRIG`) |
| `:TRIGger:SINGle:SOURce?` | silent | — the program uses `:EDGE:SOURce?` |
| `:RUNning` / `?` | yes | RUN/STOP button; write `:RUNning RUN` or `:RUNning STOP`, query returns `RUN` or `STOP`. Verified: `:TRIGger:STATus?` changed from `TRIG`→`STOP`→`TRIG` on two writes |
| `:TRIGger:FORCe` and five other force-trigger candidates | **silent** | all six candidates probed; none changed `:TRIGger:STATus?`. Force trigger is front-panel only on this firmware |

## Acquisition

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:ACQuire:MODe` / `?` | yes | Acquisition ▸ Type (`SAMPle`/`PEAK`; the manual lists no `AVERage` for this series) |
| `:ACQuire:DEPMem` / `?` | yes | Acquisition ▸ Memory (`4K`/`8K`); worth knowing: it does not change the screen capture at all — `FULLSCREEN` stays 600 bytes at either setting |
| `:ACQ:MODE` / `?` | yes | same node, alternate spelling the manual also prints |

## Measurements

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:MEASurement` / `?` | **range-flagged** | the readout: values arrive as bounds (`>2.2290e+00`) when the instrument cannot measure them, and `Frequency` is refused outright, so the plot annotates `frame_from_trace()` instead |
| `:MEASurement:DISPlay` / `?` | yes | Acquisition ▸ on-screen readout on/off |
| `:MEASurement:CH` / `?` | silent | the source selector writes it; the readback is not available |

## Data / capture

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:DATa:WAVe:SCReen:HEAD?` | yes, 479 bytes | the header the plot is decoded with — scales, probe, timebase, point count, run status |
| `:DATa:WAVe:SCReen:CH1?` | yes, 604 bytes | 300 sample points, 8-bit each in two bytes |

## Multimeter — `:DMM:*`

The HDS271 does have one; it is a separate subsystem from the scope, so it is a
separate readout in the MEASUREMENTS tab.

| Node | HDS271 | Where it is used |
| --- | --- | --- |
| `:DMM:MEAS?` | yes | the multimeter readout, polled every ~0.6 s (`0.0000 V DC` with nothing on the DMM jacks) |
| `:DMM:CONFigure:VOLTage` / `?` | **inert** / yes (`DC`) | MULTIMETER ▸ VOLT/AMP + DC/AC; asked for `AMP`, the instrument reports `VOLTage DC`, and that is what the panel says |
| `:DMM:CONFigure:CURRent` / `?` | **inert** / answers `error` | as above |
| `:DMM:CONFigure?` | answers the literal `error` | not used — the active function is read from the typed sub-node instead |
| `:DMM:REL` / `?` | yes | MULTIMETER ▸ REL; the readback is the stored offset, and `0.0000e+00` means engaged with a zero offset, not off |
| `:DMM:CONFigure:RESistance` `:DIODe` `:CONTinuity` `:CAPacitance` | silent | absent on this instrument, so they are not offered |
| `:DMM:RANGE` / `?` | silent | — |
| `:AUTO` | DMM only | — |

## Not implemented, because the hardware does not have it

`FUNCtion:*` — the whole built-in generator: `FREQuency`, `AMPLitude`, `OFFSet`,
`WIDTh`, `RISing`, `FALing`, `HIGHt`, `LOW`, `LOAD`, `PERiod`, `SYMMetry`,
`RAMP:SYMMetry`, `PULSe:WIDTh`, `PULSe:DTYCycle`. Every one of those nodes is
silent on the HDS271, which means this unit has no AWG, not that the program is
missing a feature.

The same applies to a SAVE/BMP/deep-memory download path: no documented node
answers, and the bulk endpoint (`0x82`) returns nothing, so captures come from
the screen rather than from deep memory.

## Live capture: what the transfer costs, and the faster method

A frame is two payloads over a 64-byte, 32 ms interrupt endpoint:

| Stage | Bytes | Measured |
| --- | --- | --- |
| `:DATa:WAVe:SCReen:HEAD?` | 479 | 322 ms |
| `:DATa:WAVe:SCReen:CH1?` | 604 (300 points) | 416–640 ms |
| whole capture, fresh header | — | mean 806 ms, min 672 ms |
| whole capture, header reused | — | **mean 454 ms, min 320 ms** |

The header only changes when a setting does, so the live loop asks for it once
and then reuses it, which is **352 ms off every frame (44%)**. Any write drops
it (`send_command`), and the loop re-reads it every `LIVE_HEADER_EVERY` (12)
frames so a change made on the front panel still reaches the plot within a few
seconds.

The remaining ~450 ms is the instrument's floor: the transport already reads
exactly the announced length, so there is no idle timeout to trim, and the
endpoint cannot hand over more than 64 bytes per 32 ms report.

Measured in the running program, 14 s of live refresh:

| | before | after |
| --- | --- | --- |
| period | 1.00 s mean, 0.93 s best | **0.67 s mean, 0.61 s best** |
| rate | ~1.0 fps | **~1.5 fps** |

## Display framing

The plot draws a graticule of the same **12 columns by 8 rows** as the screen, so
a square means the same thing both places, and the axis labels name what is being
drawn (`Time (ms) — 200 us/div`, `Voltage (V) — CH1 5 V/div 10X`). The column
width is the selected time/div, which is a live write. The centre cross is dotted
more visibly than the rest of the grid, as on the instrument.

The row height is the **software's own display scale**, set on the panel's
volts/div control (the knob or the dropdown). That control is deliberately not a
write — see below — so it is the app's setting, and it is what makes the control
do something visible: a finer row is worth less volts, so the same signal fills
more rows, and `rows x volts-per-row` comes back to the same amplitude at every
setting because the amplitude is decoded from the capture rather than from the
scale. Left on **Auto** it follows the scale the capture decoded to, snapped to a
setting the control itself offers, so the axis and the control always name the
same number. Autoscaling to the data instead cancels a volts/div change out — the
trace keeps the same size on screen while the instrument's display visibly
changes — which is what "the knob moves the instrument but not the software"
looks like from the front panel.

The probe dropdown is a display convention too: the divider is physical, so
selecting 1X on a 10X capture shows connector volts, a tenth of the tip volts, and
the axis label says which convention is in force.

Where the volts/div sits in that split matters, and getting it wrong is worth a
factor of ten. The sample codes are **absolute**: they carry the input, not the
display. The volts/div that `:CHANn:SCALe?` reports is **cosmetic** on this
firmware — it changes without the gain changing.

The measurement that settles it, on one unchanged signal (an HDS271, 10X probe):

| | earlier | later |
| --- | --- | --- |
| Volts/div reported | 500 mV/div (5 V/div at tip) | 5 V/div (50 V/div at tip) — **10x** |
| Codes the signal spanned | 33 | 176 — **5.3x** |
| Volts per code | ~0.145 V | ~0.145 V — **unchanged** |
| The instrument's own Vpp | 4.46 V | **25.6 V** |

A 10x change of the reported scale left the volts per code where it was, while the
code span tracked the signal itself (about 4.5 Vpp to 25.6 Vpp). The instrument's
own readings are computed from the real gain, and they describe the input: it
called that 25.6 Vpp signal **25.6 V** while its own label claimed 50 V/div.

So a code cannot be turned into volts from the label. The mapping comes from, in
order:

1. **A calibration against the instrument's own readings** — the app's path.
   `calibrate_capture()` calls `calibrate_from_measurements(pin_extremes=True)`,
   which fits the slope to (lowest code, Vmin) and (highest code, Vmin + Vpp).
   The result is remembered in `_calibrated_volts_per_code` and passed into every
   later `parse_hds_capture()`, so the volts stay right without re-reading the
   measurement block each frame. Verified against the instrument on a 25.6 Vpp
   signal: the grid read **25.60 Vpp** to its 25.6 V (0.0% and 0.8% on two
   captures, at a decoded 5.6 V/div while the label claimed 50 V/div).
2. **The label, as a last resort.** It scales the picture and the numbers are
   right only while the label happens to match the gain. The decode records which
   path it took in `volts_per_code_source`, and the capture carries both figures:
   `volts_per_div` (what the instrument claims) and `true_volts_per_div` (the
   decoded volts per code times `HDS_CODES_PER_DIVISION = 32`).

The grid's rows are `true_volts_per_div`, so rows × volts-per-row recovers the
signal: 4.50 rows × 5.69 V/div = 25.60 Vpp. `vertical_frame()` names the
instrument's claim in the axis label when the two disagree by more than 5%, so the
disagreement is visible instead of silent.

The position control is a **display shift**, not part of the acquisition: writing
one label-division of it moved the captured codes by **0**, not 32. It neither
moves the decode nor the axis.

256 codes across 8 divisions is **32 codes per division** by the geometry of the
capture; the code range and the drawn screen cover the same window. They stay
separate constants (`HDS_CODE_RANGE_DIVISIONS` vs `HDS_VERTICAL_DIVISIONS`)
because a code range is not a screen height, even when the numbers land together.

**A framing write is not in the next capture yet.** Measured with the known
5.00 Vpp signal: a capture started 50 ms after a volts/div write returns the
*previous* setting's frame — 34 codes where the new setting gives 36 — and
150 ms is clean. `download_waveform_data()` therefore waits out
`FRAMING_SETTLE_SECONDS = 0.25` after any framing write (`set_channel_scale`,
`set_channel_probe`, `set_channel_offset`, `set_timebase_scale`, `set_timebase_offset`).
Without it a volts/div change shows the old shape for a frame, which is
indistinguishable from the control not working — the same symptom the calibration
was fixed for, arriving by a different route.

**The measurement panel says which numbers are which.** `get_measurements()`
prints the capture's own figures first, headed "From the capture (calibrated -
this is what the grid shows)", then the instrument's block headed "From the
instrument (referred to its own volts/div and probe labels)". For the known
5.00 Vpp signal at 100 mV/div those two read 4.93 V and 0.888 V; printed side by
side without labels, the panel invites measuring the wrong one.

Both directions are covered:

* **Software to instrument**: a volts/div, probe or position write drops the
  cached capture header and recaptures, so the new framing is drawn at once
  rather than at the next refresh.
* **Instrument to software**: the capture header already carries the scale and
  probe in use, so a front-panel change reaches the panel's own controls through
  `sync_vertical_controls()` without another query. A value the app wrote but the
  instrument did not take is corrected back to the instrument's figure, and the
  correction is logged.

The vertical scale is drawn from the *reported* volts/div. Because a SCPI scale
write does not change the acquisition gain on this firmware, the codes at a
coarser setting are the same codes shown on a wider axis, exactly as the
instrument's own screen shows them — the reading follows the instrument rather
than second-guessing it. A capture with no usable scale (raw codes) falls back to
autoscaling instead of inventing a frame.

## Known limits

* Vertical scale, trigger level, channel display and the multimeter function are
  accepted-and-ignored by firmware V1.3.0. They are still written (the
  instrument may honour them on other firmware), but the UI reports what the
  instrument reads back, and the volts axis is calibrated in software
  (`HDS_CODES_PER_DIVISION = 34.5`, anchored on a 5.00 Vpp signal into a 10X
  probe: mean reading 5.00 V across 100 mV…1 V/div, spread ±0.22 V) rather than
  trusted from a header that can go stale.
* There is no autoset node on this firmware. AUTO frames in software from the
  decoded trace.
* An unsupported-command burst can reset the firmware and drop the device off
  the USB bus; probes must stay small and sequenced.
* The instrument's own measurement readouts are **label-referred**, so they run
  behind the trace: with the scope at 500 mV/div and a 10X probe it reads a
  signal the decoded trace puts at 6.35 V as 4.46 V, and the gap follows the
  volts/div and probe labels (switching the probe 10X → 1X scales the readout by
  10x without touching the signal). Treat them as the instrument's own opinion,
  not as a reference for the plot; the grid is the authority.
