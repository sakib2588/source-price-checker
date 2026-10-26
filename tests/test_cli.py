"""The command line and the sheet reader it uses (no network: the sheets here have no links to fetch)."""
import contextlib
import io
import os
import tempfile
import unittest

import openpyxl

import cli
from checker import sheet


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


class ReadSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_owner_columns_are_found_by_name(self):
        p = write(self.path("s.csv"), "Product Name,Maskura Link,Source 1,Source 2,Assigned Person\n"
                                      "Yoga Mat,https://www.maskura.co.uk/products/yoga-mat,"
                                      "https://www.ebay.co.uk/itm/1,N/A,Asha\n")
        got = sheet.read_sheet_file(p)
        self.assertEqual(got["columns"], {"name": "Product Name", "maskura": "Maskura Link",
                                          "sources": ["Source 1", "Source 2"]})
        row = got["rows"][0]
        self.assertEqual((row["position"], row["name"]), (1, "Yoga Mat"))
        self.assertEqual(row["source_urls"], ["https://www.ebay.co.uk/itm/1", ""], "N/A is no link")

    def test_header_spelling_does_not_matter(self):
        p = write(self.path("s.csv"), "product_name;MASKURA URL;Source1;SRC 2\nMat;;;\n")
        self.assertEqual(sheet.read_sheet_file(p)["columns"]["sources"], ["Source1", "SRC 2"])

    def test_missing_columns_are_warnings(self):
        p = write(self.path("s.csv"), "Thing\nx\n")
        self.assertEqual(len(sheet.read_sheet_file(p)["warnings"]), 3)

    def test_xlsx_link_text_keeps_its_hyperlink(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Product Name", "Maskura Link", "Source 1"])
        ws.append(["Mat", "", "Link"])
        ws["C2"].hyperlink = "https://www.ebay.co.uk/itm/42"
        p = self.path("s.xlsx")
        wb.save(p)
        self.assertEqual(sheet.read_sheet_file(p)["rows"][0]["source_urls"], ["https://www.ebay.co.uk/itm/42"])

    def test_other_file_types_are_refused(self):
        p = write(self.path("s.txt"), "Product Name\nx\n")
        with self.assertRaises(sheet.SheetError):
            sheet.read_sheet_file(p)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "checks.db")
        self.out = os.path.join(self.tmp.name, "review.xlsx")

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(list(argv))
        return buf.getvalue()

    def test_check_writes_the_review_workbook(self):
        p = write(os.path.join(self.tmp.name, "s.csv"), "Product Name,Maskura Link,Source 1\nMat,,\nBand,,\nRope,,\n")
        text = self.run_cli("check", p, "--limit", "2", "--db", self.db, "--out", self.out, "--every", "0")
        self.assertIn("Checking 0 products", text, "rows without links make no requests")
        self.assertIn("2 products", text)
        wb = openpyxl.load_workbook(self.out)
        self.assertEqual(wb["Products"].max_row - 1, 2)

    def test_review_needs_a_results_file(self):
        with self.assertRaises(SystemExit):
            self.run_cli("review", "--db", self.db, "--out", self.out)


if __name__ == "__main__":
    unittest.main()
