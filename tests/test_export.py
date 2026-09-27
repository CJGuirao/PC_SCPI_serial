"""Export: the samples, and the settings that make them checkable.

The XLSX writer is the one piece here worth testing carefully, because it is a file
format written by hand. `read_xlsx_sheets` reads this module's own output back and
the tests assert on the values - a workbook that opens in Excel is not something a
test can see, but a workbook whose XML says the wrong number is.
"""

import csv
import json
import os
import tempfile
import unittest
import zipfile

import waveform_export as export

SETTINGS = {"model": "HDS271", "serial": "25520161", "firmware": "V1.3.0",
            "timebase_scale_s": 5e-4, "sample_rate": 50000.0, "point_interval_s": 2e-05,
            "points": 4, "calibration_trim": 0.968992, "note": "25 Vpp square, X1 probe"}


def channels():
    return [{"name": "CH1", "point_interval": 2e-05, "volts_per_div": 5.0, "probe": "X1",
             "coupling": "DC", "waveform_data": [0.0, 12.5, -12.5, 6.0]},
            {"name": "CH2", "point_interval": 2e-05, "volts_per_div": 5.0, "probe": "X1",
             "coupling": "DC", "waveform_data": [1.0, 1.0, 1.0, 1.0]}]


class TempMixin(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="waveform-export-")
        self.addCleanup(self._dir.cleanup)

    def path(self, name):
        return os.path.join(self._dir.name, name)


class CsvTests(TempMixin):
    def test_csv_carries_the_time_of_each_sample(self):
        path = export.write_csv(self.path("capture.csv"), channels(), SETTINGS)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["channel", "index", "time_s", "volts"])
        self.assertEqual(len(rows), 1 + 4 + 4)
        self.assertEqual(rows[1][0], "CH1")
        self.assertEqual(rows[1][1], "0")
        self.assertEqual(float(rows[2][2]), 2e-05)          # one interval along
        self.assertAlmostEqual(float(rows[2][3]), 12.5)

    def test_csv_holds_only_data_so_a_parser_can_read_it(self):
        path = export.write_csv(self.path("capture.csv"), channels(), SETTINGS)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        for row in rows[1:]:                      # not the header, which is words
            self.assertEqual(len(row), 4)
            float(row[1]), float(row[2]), float(row[3])


class JsonTests(TempMixin):
    def test_json_round_trips_the_channels_and_keeps_the_settings(self):
        path = export.write_json(self.path("capture.json"), channels(), SETTINGS)
        payload = json.loads(open(path, encoding="utf-8").read())
        self.assertEqual(payload["settings"]["model"], "HDS271")
        self.assertEqual(payload["settings"]["calibration_trim"], 0.968992)
        self.assertEqual(len(payload["channels"]), 2)
        self.assertEqual(payload["channels"][0]["waveform_data"][1], 12.5)


class XlsxTests(TempMixin):
    def test_an_xlsx_is_a_zip_the_reader_can_parse(self):
        path = export.write_xlsx(self.path("capture.xlsx"), channels(), SETTINGS)
        with zipfile.ZipFile(path) as book:
            names = set(book.namelist())
        for part in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml",
                     "xl/worksheets/sheet2.xml"):
            self.assertIn(part, names)

    def test_the_samples_sheet_has_numbers_in_it(self):
        path = export.write_xlsx(self.path("capture.xlsx"), channels(), SETTINGS)
        sheets = export.read_xlsx_sheets(path)
        rows = sheets["Samples"]
        self.assertEqual(rows[0], ["channel", "index", "time_s", "volts"])
        self.assertEqual(rows[1][0], "CH1")
        self.assertAlmostEqual(rows[2][3], 12.5)
        self.assertAlmostEqual(rows[2][2], 2e-05)

    def test_the_settings_sheet_carries_the_provenance(self):
        path = export.write_xlsx(self.path("capture.xlsx"), channels(), SETTINGS)
        rows = export.read_xlsx_sheets(path)["Settings"]
        found = {row[0]: row[1] for row in rows[1:] if len(row) >= 2 and isinstance(row[0], str)}
        self.assertEqual(found["model"], "HDS271")
        self.assertAlmostEqual(float(found["calibration_trim"]), 0.968992)
        # And the per-channel framing, which is what makes the volts meaningful:
        # a table of its own under the settings, keyed by channel name.
        ch1 = [row for row in rows if row and row[0] == "CH1"]
        self.assertTrue(ch1, "no per-channel block")
        self.assertAlmostEqual(float(ch1[0][1]), 5.0)         # volts/div
        self.assertEqual(ch1[0][2], "X1")                     # probe
        self.assertAlmostEqual(float(ch1[0][4]), 4.0)         # points

    def test_a_sheet_name_excel_cannot_take_is_repaired(self):
        self.assertEqual(export.sheet_name("a/b:c*d"), "a b c d")
        self.assertEqual(len(export.sheet_name("x" * 80)), 31)
        self.assertEqual(export.sheet_name(""), "Sheet")

    def test_duplicate_sheet_names_are_made_unique(self):
        used = set()
        self.assertEqual(export.sheet_name("Data", used), "Data")
        self.assertEqual(export.sheet_name("Data", used), "Data (2)")
        self.assertEqual(export.sheet_name("Data", used), "Data (3)")

    def test_text_is_escaped_so_the_xml_survives(self):
        channel = [{"name": "CH1", "point_interval": 1e-3, "waveform_data": [1.0]}]
        path = export.write_xlsx(self.path("escape.xlsx"), channel,
                                 {"note": 'R<1 & "R>2"'})
        rows = export.read_xlsx_sheets(path)["Settings"]
        notes = [row[1] for row in rows if row and row[0] == "note"]
        self.assertEqual(notes, ['R<1 & "R>2"'])

    def test_column_letters_run_past_z(self):
        self.assertEqual(export.column_letter(1), "A")
        self.assertEqual(export.column_letter(26), "Z")
        self.assertEqual(export.column_letter(27), "AA")
        self.assertEqual(export.column_letter(52), "AZ")
        self.assertEqual(export.column_letter(53), "BA")


class FigureTests(TempMixin):
    def test_a_plot_can_be_written_as_png_and_pdf(self):
        from matplotlib.figure import Figure
        figure = Figure(figsize=(4, 3))
        axes = figure.add_subplot(111)
        axes.plot([0, 1, 2], [0, 12.5, 0])
        png = export.save_figure(figure, self.path("plot.png"), dpi=100)
        pdf = export.save_figure(figure, self.path("plot.pdf"), dpi=100)
        self.assertGreater(os.path.getsize(png), 3000)
        self.assertEqual(open(pdf, "rb").read(4), b"%PDF")


class DispatchTests(TempMixin):
    def test_the_suffix_decides_the_format(self):
        from matplotlib.figure import Figure
        figure = Figure(figsize=(3, 2))
        figure.add_subplot(111).plot([0, 1], [0, 1])
        for name in ("a.csv", "a.json", "a.xlsx"):
            described = export.export_capture(self.path(name), channels(), SETTINGS)
            self.assertIn(name, described)
        self.assertIn("plot image", export.export_capture(self.path("a.png"), channels(),
                                                          SETTINGS, figure=figure))

    def test_a_format_we_cannot_write_says_so(self):
        with self.assertRaises(ValueError):
            export.export_capture(self.path("a.doc"), channels(), SETTINGS)

    def test_an_image_needs_a_figure(self):
        with self.assertRaises(ValueError):
            export.export_capture(self.path("a.png"), channels(), SETTINGS, figure=None)

    def test_empty_channels_still_produce_a_valid_file(self):
        described = export.export_capture(self.path("empty.csv"), [], SETTINGS)
        self.assertIn("0 sample rows", described)


class ProvenanceTests(unittest.TestCase):
    def test_every_declared_field_is_present_even_when_unknown(self):
        block = export.provenance(model="HDS271")
        for field in export.PROVENANCE_FIELDS:
            self.assertIn(field, block)
        self.assertEqual(block["model"], "HDS271")
        self.assertIsNone(block["firmware"])

    def test_the_size_a_person_reads(self):
        self.assertEqual(export.human_size(512), "512 B")
        self.assertEqual(export.human_size(2048), "2.0 kB")
        self.assertEqual(export.human_size(3 * 1024 * 1024), "3.0 MB")


if __name__ == "__main__":
    unittest.main()
