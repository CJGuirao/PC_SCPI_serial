"""Writing a capture out, with the settings that made its numbers true.

A CSV of volts with no provenance is the file nobody can check later: it does not
say which scope, at what volts/div, with which probe, calibrated how. So every
export here carries its settings, and the two formats with room for them - JSON and
XLSX - get a settings sheet rather than a comment line a parser would choke on.

The XLSX writer is deliberately minimal: this project has no spreadsheet library
and does not need one to write a sheet of numbers and a sheet of labels. It emits
the smallest valid workbook, which is a zip of XML.
"""

from __future__ import annotations

import csv
import json
import os
import re
import zipfile
from pathlib import Path

#: Suffixes this module can write, and what each one is for.
EXPORT_FORMATS = ((".csv", "CSV (samples)"),
                  (".json", "JSON (samples and settings)"),
                  (".xlsx", "Excel workbook (samples and settings)"),
                  (".png", "PNG image of the plot"),
                  (".pdf", "PDF of the plot"))

#: The settings a capture is exported with, in the order they are written out.
PROVENANCE_FIELDS = ("model", "serial", "firmware", "timebase_scale_s", "sample_rate",
                     "point_interval_s", "points", "window", "fft_format", "view",
                     "calibration_trim", "volts_per_code", "note")


def provenance(**values):
    """The settings block, with absent entries kept as empty rather than dropped.

    An empty field is information - "this export does not know its sample rate" -
    and a missing one reads as an oversight.
    """
    return {field: values.get(field) for field in PROVENANCE_FIELDS}


def channel_rows(channels):
    """Rows of (channel, index, time, volts) for every channel that has samples."""
    rows = []
    for channel in channels or []:
        name = str(channel.get("name", "CH1"))
        interval = float(channel.get("point_interval", 0) or 0)
        for index, value in enumerate(channel.get("waveform_data") or []):
            rows.append((name, index, index * interval, float(value)))
    return rows


def write_csv(path, channels, settings=None):
    """Every sample on one line, with the time it was taken at.

    The time column is computed from the interval the capture reported, so a
    reader does not have to know it to plot the file.
    """
    path = str(path)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["channel", "index", "time_s", "volts"])
        for name, index, when, value in channel_rows(channels):
            writer.writerow([name, index, "%.9g" % when, "%.9g" % value])
    return path


def write_json(path, channels, settings=None):
    """The whole capture: samples, settings, and the instrument's own header."""
    payload = {"settings": dict(settings or {}), "channels": list(channels or [])}
    Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# A minimal XLSX writer
_XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}
</Types>"""

_XLSX_SHEET_OVERRIDE = ('<Override PartName="/xl/worksheets/sheet%d.xml" '
                        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')

_XLSX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

_XLSX_WORKBOOK_SHEET = '<sheet name="{name}" sheetId="{id}" r:id="rId{id}"/>'

_XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""

_XLSX_WORKBOOK_REL = ('<Relationship Id="rId{id}" Type="http://schemas.openxmlformats.org/'
                      'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{id}.xml"/>')

_XLSX_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>{rows}</sheetData>
</worksheet>"""


def column_letter(index):
    """1 -> A, 27 -> AA: the spreadsheet's own column naming."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def sheet_name(text, used=None):
    """A sheet name Excel will accept: 31 characters, none of []:*?/\\ , and unique."""
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", str(text or "Sheet")).strip() or "Sheet"
    cleaned = cleaned[:31]
    if used is None:
        return cleaned
    candidate, suffix = cleaned, 2
    while candidate in used:
        tail = " (%d)" % suffix
        candidate = cleaned[:31 - len(tail)] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _cell(reference, value):
    if value is None or value == "":
        return '<c r="%s"/>' % reference
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (reference, repr(float(value)))
    text = (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (reference, text)


def _sheet_xml(rows):
    body = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(_cell("%s%d" % (column_letter(column), row_index), value)
                        for column, value in enumerate(row, start=1))
        body.append('<row r="%d">%s</row>' % (row_index, cells))
    return _XLSX_SHEET.format(rows="".join(body))


def write_xlsx(path, channels, settings=None, sheet_label="Samples"):
    """A workbook with the samples and the settings that made them true.

    Two sheets rather than one, because the settings are a table of their own and
    a spreadsheet is the one place a reader will actually look for them.
    """
    used = set()
    samples_name = sheet_name(sheet_label, used)
    settings_name = sheet_name("Settings", used)

    sample_rows = [["channel", "index", "time_s", "volts"]]
    sample_rows += [[name, index, when, value] for name, index, when, value in channel_rows(channels)]
    per_channel = [["channel", "volts_per_div", "probe", "coupling", "points"]]
    for channel in channels or []:
        per_channel.append([str(channel.get("name", "CH1")),
                            channel.get("volts_per_div") or channel.get("scale") or "",
                            channel.get("probe") or "",
                            channel.get("coupling") or "",
                            len(channel.get("waveform_data") or [])])
    settings_rows = [["setting", "value"]]
    for key, value in (settings or {}).items():
        settings_rows.append([key, value])
    settings_rows += per_channel

    sheets = [(samples_name, _sheet_xml(sample_rows)), (settings_name, _sheet_xml(settings_rows))]
    overrides = "".join(_XLSX_SHEET_OVERRIDE % (index + 1) for index in range(len(sheets)))
    workbook_sheets = "".join(_XLSX_WORKBOOK_SHEET.format(name=name, id=index + 1)
                              for index, (name, _) in enumerate(sheets))
    rels = "".join(_XLSX_WORKBOOK_REL.format(id=index + 1) for index in range(len(sheets)))

    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES.format(sheets=overrides))
        book.writestr("_rels/.rels", _XLSX_ROOT_RELS)
        book.writestr("xl/workbook.xml", _XLSX_WORKBOOK.format(sheets=workbook_sheets))
        book.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS.format(rels=rels))
        for index, (_, xml) in enumerate(sheets, start=1):
            book.writestr("xl/worksheets/sheet%d.xml" % index, xml)
    return str(path)


def read_xlsx_sheets(path):
    """What was written: {sheet name: [[cell, ...], ...]}. Used by the tests.

    Not a general reader - it understands this module's own output, which is what
    makes it a check rather than a second implementation to keep in step.
    """
    import xml.etree.ElementTree as ET
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(str(path)) as book:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        names = [sheet.get("name") for sheet in workbook.iter(namespace + "sheet")]
        sheets = {}
        for index, name in enumerate(names, start=1):
            root = ET.fromstring(book.read("xl/worksheets/sheet%d.xml" % index))
            rows = []
            for row in root.iter(namespace + "row"):
                cells = []
                for cell in row.iter(namespace + "c"):
                    value = cell.find(namespace + "v")
                    if value is not None:
                        cells.append(float(value.text))
                        continue
                    text = cell.find(namespace + "is/" + namespace + "t")
                    cells.append(text.text if text is not None else None)
                rows.append(cells)
            sheets[name] = rows
    return sheets


def save_figure(figure, path, dpi=200, facecolor=None):
    """The plot as an image or a PDF, at a resolution worth printing."""
    figure.savefig(str(path), dpi=dpi, facecolor=facecolor or figure.get_facecolor(),
                   bbox_inches="tight")
    return str(path)


def export_capture(path, channels, settings=None, figure=None):
    """Write one capture in whichever format the path asks for.

    Returns a short description of what was written, for the log.
    """
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".csv":
        write_csv(path, channels, settings)
        what = "%d sample rows" % len(channel_rows(channels))
    elif suffix == ".json":
        write_json(path, channels, settings)
        what = "%d channels" % len(channels or [])
    elif suffix == ".xlsx":
        write_xlsx(path, channels, settings)
        what = "samples and settings sheets"
    elif suffix in (".png", ".pdf"):
        if figure is None:
            raise ValueError("no figure to write for %s" % suffix)
        save_figure(figure, path)
        what = "plot image"
    else:
        raise ValueError("cannot write %s" % (suffix or "a file with no extension"))
    size = os.path.getsize(str(path)) if os.path.exists(str(path)) else 0
    return "%s (%s, %s)" % (os.path.basename(str(path)), what, human_size(size))


def human_size(size):
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f kB" % (size / 1024.0)
    return "%.1f MB" % (size / (1024.0 * 1024.0))


# ----------------------------------------------------------------------
# Reading a capture back
class LoadedCapture:
    """A capture read from a file, shaped like the controller's waveform data.

    The app draws whatever this looks like; giving it the same attribute names is
    what lets a saved file be shown by the same code that shows a live frame,
    rather than by a second renderer that drifts away from the first.
    """

    def __init__(self, channels, settings=None, source=""):
        self.channels = list(channels or [])
        self.settings = dict(settings or {})
        self.source = source
        self.model = self.settings.get("model") or "FILE"
        self.serial_number = self.settings.get("serial") or ""
        self.firmware = self.settings.get("firmware") or ""
        self.timebase_scale = self.settings.get("timebase_scale_s")
        self.sample_rate = self.settings.get("sample_rate") or ""
        self.run_status = "FILE"
        self.datatype = "FILE"
        self.header = json.dumps(self.settings, default=str)

    def describe(self):
        return "%s: %d channel(s), %d points" % (
            self.source or self.model,
            len(self.channels),
            sum(len(channel.get("waveform_data") or []) for channel in self.channels))


def read_capture(path):
    """Read one of this module's own exports back: (settings, channels).

    The CSV carries one row per sample with its time, so the interval is measured
    from the file rather than assumed; an older export without the time column
    comes back with no interval, and a spectrum made from it says so instead of
    inventing an axis.
    """
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".json":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        channels = payload.get("channels") or []
        settings = payload.get("settings") or {}
        if not settings and payload.get("header"):
            settings = {"note": "older export, header kept as text"}
        return settings, channels
    if suffix == ".csv":
        return _read_csv(path)
    raise ValueError("cannot read %s" % (suffix or "a file with no extension"))


def _read_csv(path):
    order, samples, times = [], {}, {}
    with open(str(path), newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("channel") or "CH1").strip()
            if name not in samples:
                order.append(name)
                samples[name] = []
                times[name] = []
            value = row.get("volts", row.get("value"))
            try:
                samples[name].append(float(value))
            except (TypeError, ValueError):
                continue
            try:
                times[name].append(float(row.get("time_s")))
            except (TypeError, ValueError):
                times[name].append(None)
    channels = []
    for name in order:
        stamps = [stamp for stamp in times[name] if stamp is not None]
        interval = 0.0
        if len(stamps) >= 2:
            gaps = sorted(stamps[index + 1] - stamps[index] for index in range(len(stamps) - 1))
            interval = gaps[len(gaps) // 2]                  # the median gap, not the first
        channels.append({"name": name, "waveform_data": samples[name],
                         "point_interval": interval, "num_points": len(samples[name]),
                         "whole_screen_points": len(samples[name])})
    return {}, channels
