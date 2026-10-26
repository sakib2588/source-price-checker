"""Read the sourcing sheet (.csv or .xlsx) into rows for sc_store.Store.import_sheet.

Moved out of the portal's app.py unchanged, so the command line reads a sheet exactly as the upload page does.
Columns are found by name: Product Name, Maskura Link, Source 1 to Source 10 (case, spaces and punctuation
ignored). The first worksheet with data is read; a cell showing text such as "Link" keeps its hyperlink URL.
"""
import codecs
import csv
import datetime
import io
import os
import re
from urllib.parse import urlsplit

from . import sc_urls

try:
    import openpyxl as _openpyxl
    _SC_XLSX_OK = True
except ImportError:   # .csv still works without openpyxl
    _openpyxl = None
    _SC_XLSX_OK = False

_SC_MAX_ROWS     = 100_000
_SC_NAME_COLS    = ("product name", "product_name", "name", "title")
_SC_MASKURA_COLS = ("maskura link", "maskura_link", "maskura url", "maskura")


def _sc_normalise(name):
    s = str(name or "").replace("﻿", "").lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _sc_find_col(fieldnames, *candidates):
    norm = {}
    for f in fieldnames:
        n = _sc_normalise(f)
        if n:
            norm.setdefault(n, f)
    for c in candidates:
        if _sc_normalise(c) in norm:
            return norm[_sc_normalise(c)]
    return None


_SC_SRC_HEADER_RE = re.compile(r"^(?:source|src)_?(\d{1,2})(?:_?(?:url|link))?$")


def _sc_find_src_cols(fieldnames):
    """Source 1..10 columns in numeric order. Accepts 'Source1', 'source #2', 'Source 3 URL', 'SRC4'."""
    by_num = {}
    for f in fieldnames:
        m = _SC_SRC_HEADER_RE.match(_sc_normalise(f))
        if m and 1 <= int(m.group(1)) <= 10:
            by_num.setdefault(int(m.group(1)), f)
    return [by_num[n] for n in sorted(by_num)]


def _sc_decode_bytes(raw):
    """Text of an uploaded sheet (UTF-8, UTF-16/32 with BOM, or Windows-1252), or None if it is not text."""
    if not raw:
        return ""
    if raw[:4] in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE):
        encodings = ("utf-32",)
    elif raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp1252")
    text = None
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text:
        return None
    sample = text[:65536]
    controls = sum(1 for c in sample if ord(c) < 32 and c not in "\t\r\n")
    if controls > max(3, len(sample) // 50):
        return None
    return text


def _sc_sniff_delimiter(text):
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return max((",", ";", "\t", "|"), key=lambda d: (first.count(d), d == ","))


def _sc_parse_sheet(text):
    """-> (rows as dicts keyed by header, header list). Ragged rows are padded or trimmed."""
    try:
        table = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=_sc_sniff_delimiter(text))
                 if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise ValueError(f"Could not read the file as a spreadsheet: {exc}") from exc
    if not table:
        return [], []
    header = [h.replace("﻿", "").strip() for h in table[0]]
    rows = []
    for cells in table[1:]:
        cells = (list(cells) + [""] * len(header))[:len(header)]
        rows.append({h: c.strip() for h, c in zip(header, cells)})
    return rows, header

def _sc_cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


def _sc_xlsx_to_text(data, ext):
    """CSV text of the first worksheet that has data. Raises ValueError with a message to show the user."""
    if ext == "xls" and not data.startswith(b"PK"):
        raise ValueError("Old .xls files are not supported. Open it in Excel, save it as .xlsx or .csv, "
                         "and upload that file.")
    try:
        wb = _openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ValueError("Could not read this Excel file. Re-save it as .xlsx, or export it to .csv, "
                         "and upload that file.") from exc
    try:
        for ws in wb.worksheets:
            table, blanks = [], 0
            for i, row in enumerate(ws.iter_rows()):
                cells = [(_sc_cell_text(c.value), getattr(getattr(c, "hyperlink", None), "target", None))
                         for c in row]
                if not any(t.strip() for t, _ in cells):
                    blanks += 1
                    if blanks > 1000 or i > 200_000:
                        break
                    continue
                blanks = 0
                table.append(cells)
            if not table:
                continue
            header = [t.strip() for t, _ in table[0]]
            link_cols = {header.index(c) for c in
                         _sc_find_src_cols(header) + [_sc_find_col(header, *_SC_MASKURA_COLS)] if c}
            out = io.StringIO()
            writer = csv.writer(out, lineterminator="\n")
            for r_i, cells in enumerate(table):
                line = []
                for c_i, (text, link) in enumerate(cells):
                    # A cell showing friendly text such as "Link" keeps its URL in the hyperlink target.
                    if r_i and c_i in link_cols and link and not _sc_normalize_url(text) and _sc_normalize_url(link):
                        text = link
                    line.append(text)
                writer.writerow(line)
            return out.getvalue()
    finally:
        wb.close()
    raise ValueError("The workbook has no data")


class SheetError(ValueError):
    """A sheet that cannot be read, with the HTTP status to answer and a sentence to show."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _sc_sheet_text(filename, data):
    """CSV text of an uploaded .xlsx or .csv (shared by the old and the new page). Raises SheetError."""
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext not in ("xlsx", "xls", "csv"):
        raise SheetError("Only .xlsx and .csv files are supported", 400)
    if ext in ("xlsx", "xls"):
        if not _SC_XLSX_OK:
            raise SheetError("openpyxl not installed — upload a .csv instead", 501)
        return _sc_xlsx_to_text(data, ext)
    text = _sc_decode_bytes(data)
    if text is None:
        raise SheetError("That file is not a text spreadsheet. Export the sheet as CSV (UTF-8) or upload the .xlsx.")
    return text


def _sc_read_sheet(filename, data):
    """Store rows for sc_store.import_sheet from an uploaded file (INTERFACES.md "Sheet upload").

    Positions are 1-based data rows, blank rows skipped, as the old crawler counted them. Source cells go through
    sc_urls.normalize_source_url, so "N/A" or "TBC" is no link. Raises SheetError with a sentence to show.
    """
    text = _sc_sheet_text(filename, data)
    rows, fns = _sc_parse_sheet(text)
    if not fns:
        raise SheetError("The file is empty: no header row found")
    if not rows:
        raise SheetError("The file has a header row but no data rows")
    if len(rows) > _SC_MAX_ROWS:
        raise SheetError(f"The sheet has {len(rows):,} rows; the limit is {_SC_MAX_ROWS:,}.")
    name_col = _sc_find_col(fns, *_SC_NAME_COLS)
    maskura_col = _sc_find_col(fns, *_SC_MASKURA_COLS)
    src_cols = _sc_find_src_cols(fns)
    warnings = []
    if not name_col:
        warnings.append("No product name column found, so rows show their row number instead.")
    if not maskura_col:
        warnings.append("No Maskura link column found, so no margin can be worked out.")
    if not src_cols:
        warnings.append("No Source 1 to Source 10 columns found, so there are no supplier links to check.")
    out = []
    for position, row in enumerate(rows, 1):
        out.append({
            "position": position,
            "name": (row.get(name_col) or "").strip() if name_col else "",
            "maskura_raw": (row.get(maskura_col) or "").strip() if maskura_col else "",
            "source_urls": [sc_urls.normalize_source_url(row.get(c) or "") or "" for c in src_cols],
        })
    return {"rows": out, "columns": {"name": name_col, "maskura": maskura_col, "sources": src_cols},
            "warnings": warnings}

_SC_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _sc_normalize_url(value):
    """An http(s) URL for a sheet cell, or None if the cell is blank or not a link.

    Repairs a missing scheme ('www.ebay.co.uk/itm/1'); rejects placeholders ('N/A', '-', 'TBC'),
    text with spaces, and non-web schemes.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v or any(c.isspace() for c in v):
        return None
    try:
        if "://" in v:
            if not re.match(r"^https?://", v, re.I):
                return None
            host = urlsplit(v).hostname or ""
            return v if _SC_HOST_RE.match(host.lower()) and not host.rsplit(".", 1)[-1].isdigit() else None
        if re.match(r"^(javascript|mailto|data|file|tel|about|blob):", v, re.I):
            return None
        host = (urlsplit("https://" + v).hostname or "").lower()
    except ValueError:
        return None
    if _SC_HOST_RE.match(host) and host.rsplit(".", 1)[-1].isalpha():
        return "https://" + v
    return None


def read_sheet_file(path):
    """{"rows", "columns", "warnings"} for the sheet at `path`. Raises SheetError with a sentence to show."""
    with open(path, "rb") as fh:
        data = fh.read()
    return _sc_read_sheet(os.path.basename(path), data)
