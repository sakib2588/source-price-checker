"""Build the Source Check review workbook (.xlsx) from the checker's database. Read-only on the database.

One row per product with the checker's decision (sc_crawl.decide_product, the same rule the portal uses),
both sources, a margin formula, and yellow columns for the supervisor's manual check. Opens in Excel and
imports into Google Sheets with its colors, filters and dropdowns.

Used by the portal's Download review sheet button and by scripts/build_review_workbook.py.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import sc_crawl
from . import sc_store
from . import sc_variants

LONDON = ZoneInfo("Europe/London")
FONT = "Arial"
NAVY, GOLD, GRID = "1F3864", "7F6000", "BFBFBF"
INPUT_FILL = "FFF9DB"

# status key -> (words, needs action by default, what to do, fill, text color); the order is the Summary's order
STATUSES = [
    ("NO_BUYABLE_SOURCE", "No buyable source", "Find a new source listing", "F8D7DA", "842029"),
    ("COULD_NOT_CHECK", "Could not check", "Open the source links by hand", "FFF3CD", "664D03"),
    ("MASKURA_GONE", "Maskura page gone", "Fix or remove the Maskura link", "F1AEB5", "58151C"),
    ("SOURCE_DEARER", "Source costs the same or more", "Find a cheaper source or raise the price", "FFE5D0", "8A3C00"),
    ("MASKURA_UNREADABLE", "Maskura page not read", "Open the Maskura link by hand", "FFF3CD", "664D03"),
    ("OK", "OK", "Nothing to do", "D1E7DD", "0F5132"),
    ("NO_MASKURA_LINK", "No Maskura link in the sheet", "Add the Maskura link to the sheet", "E2E3E5", "41464B"),
    ("NO_SOURCE_LINK", "No source link in the sheet", "Add a source link to the sheet", "E2E3E5", "41464B"),
    ("CHECKING", "Still being checked", "Wait for the check to finish", "E2E3E5", "41464B"),
]
STATUS = {k: (w, todo, fill, fg) for k, w, todo, fill, fg in STATUSES}
HOW = {"link": "Variant in the link", "single": "Only one price", "name": "Matching variant name",
       "cheapest": "Cheapest variant", "product": "Product price"}
CHECK_CHOICES = ["Not checked yet", "Checker is right", "Checker is wrong", "Fixed"]
CHECK_COLORS = {"Checker is right": ("D1E7DD", "0F5132"), "Checker is wrong": ("F8D7DA", "842029"),
                "Fixed": ("CFE2FF", "084298")}
UNREAD = ("NO_CONTENT", "BLOCKED", "ERROR")

PRODUCT_COLUMNS = [  # (header, width, note)
    ("Row in sheet", 8, "The product's row number in the uploaded sheet (All Products)."),
    ("Product", 38, "Product name from the sheet."),
    ("Needs action", 9, "Yes when the checker flags this product, the same as the portal's Needs action list."),
    ("Status", 24, "The checker's result for the product. The About tab explains each status."),
    ("What to do", 30, None),
    ("Details", 40, "Extra notes from the check, such as a moved Maskura link or a variant that was not named."),
    ("Maskura link", 34, "The Maskura link from the sheet."),
    ("Maskura link now", 34, "Filled only when the Maskura link redirects to a new address. Update the sheet to it."),
    ("Maskura price (£)", 11, "The Maskura price used in the comparison: the same variant as the source when it can be matched."),
    ("Compared with", 18, "How the Maskura variant was chosen for the comparison."),
    ("Best source", 10, "The buyable source with the biggest margin."),
    ("Best source price (£)", 11, None),
    ("Margin (£)", 11, "Maskura price minus best source price. Negative means the source costs more."),
    ("Margin %", 9, "Margin as a share of the Maskura price."),
    ("Source 1 link", 34, None), ("Source 1 result", 20, None), ("Source 1 price (£)", 11, None),
    ("Source 2 link", 34, None), ("Source 2 result", 20, None), ("Source 2 price (£)", 11, None),
    ("Checked (UK time)", 19, "When the checker last read this product's pages."),
    ("Supervisor check", 17, "Pick one: Not checked yet, Checker is right, Checker is wrong, Fixed."),
    ("Checked by", 14, "Who did the manual check."),
    ("Check date", 12, "The date of the manual check."),
    ("Supervisor note", 40, "Anything the supervisor found."),
]
SOURCE_COLUMNS = [("Row in sheet", 8), ("Product", 38), ("Source", 9), ("Site", 16), ("Link", 40),
                  ("Result", 22), ("Price (£)", 11), ("Variation in the link", 22), ("Title on the page", 50),
                  ("Checked (UK time)", 19)]


def uk_time(iso):
    if not iso:
        return ""
    t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(LONDON)
    return t.strftime("%d %b %Y, %I:%M %p").replace(", 0", ", ")


def short_link(url):
    if not url:
        return ""
    u = urlsplit(url)
    host = u.netloc.removeprefix("www.")
    path = u.path.rstrip("/")
    if "ebay." in host and "/itm/" in path:
        var = (parse_qs(u.query).get("var") or [None])[0]
        path = "/itm/" + path.split("/itm/", 1)[1].split("/")[-1]
        return host + path + (f"?var={var}" if var else "")
    text = host + path
    return text if len(text) <= 70 else text[:67] + "..."


def result_words(s):
    v, avail = s.get("verdict"), s.get("available")
    if v == "PRICE":
        return "In stock" if avail is not False else "Sold out"
    return {"OUT_OF_STOCK": "Listing ended or sold out", "ENDED_REDIRECT": "Listing ended",
            "NOT_FOUND": "Page not found", "NO_CONTENT": "Page did not load",
            "BLOCKED": "Site blocked the check", "ERROR": "Could not load",
            "PARSER_GAP": "Price not found on page"}.get(v, v or "")


def load(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    srcs = {}
    for r in c.execute("SELECT product_id, slot, url, raw_json, fetched_at FROM source_results ORDER BY product_id, slot"):
        raw = json.loads(r["raw_json"] or "{}") if r["url"] else dict(sc_store.NO_URL_SOURCE)
        srcs.setdefault(r["product_id"], []).append((r["slot"], r["url"], raw, r["fetched_at"]))
    products = [dict(p) for p in c.execute(
        "SELECT id, position, name, maskura_url, maskura_status, maskura_result_json, has_problem, last_crawled_at "
        "FROM products WHERE COALESCE(archived, 0) = 0 ORDER BY position, id")]
    c.close()
    return products, srcs


def decide(p, srcs):
    m = None if p["maskura_status"] == "NO_LINK" else json.loads(p["maskura_result_json"] or "{}")
    sources = [raw for _, _, raw, _ in srcs]
    status, best, notes = sc_crawl.decide_product(m, sources)
    linked = [raw for _, url, raw, _ in srcs if url]
    if status == "NO_BUYABLE_SOURCE" and linked and all(s.get("verdict") in UNREAD for s in linked):
        status = "COULD_NOT_CHECK"
    mp, how = None, None
    if m and best:
        mp, how = sc_variants.compare_price(m, sources[best - 1])
    elif m and m.get("price_gbp"):
        mp, how = float(m["price_gbp"]), "product"
    return m, status, best, notes, mp, how


def style_header(ws, columns, fills):
    for i, (header, width, *rest) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=i, value=header)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor=fills.get(i, NAVY))
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
        if rest and rest[0]:
            cell.comment = Comment(rest[0], "Source check")
    ws.row_dimensions[1].height = 34


def put(ws, row, col, value, *, link=None, fmt=None):
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") and not value.startswith("=IF"):
        cell.data_type = "s"
    if link:
        cell.hyperlink = link
        cell.font = Font(name=FONT, size=10, color="0563C1", underline="single")
    if fmt:
        cell.number_format = fmt
    return cell


def fill_rule(fill, fg=None, bold=False):
    return dict(fill=PatternFill("solid", fgColor=fill, bgColor=fill), font=Font(color=fg, bold=bold) if fg else None)


def reason_text(status, srcs, mp, best):
    """The Reason column: which problem the product has, in plain words, or It's okay."""
    def per_source(only_unread=False):
        parts = [f"Source {slot}: {result_words(raw).lower()}" for slot, url, raw, _ in srcs
                 if url and (not only_unread or raw.get("verdict") in UNREAD)]
        return f" ({'; '.join(parts)})" if parts else ""
    if status == "OK":
        return "It's okay"
    if status == "NO_BUYABLE_SOURCE":
        return "No buyable source" + per_source()
    if status == "COULD_NOT_CHECK":
        return "Could not check, source page did not load" + per_source(only_unread=True)
    if status == "SOURCE_DEARER":
        raw = next((r for slot, url, r, _ in srcs if slot == best), {})
        if mp is not None and raw.get("price_gbp") is not None:
            return (f"Source costs the same or more (Maskura £{mp:,.2f}, Source {best} "
                    f"£{float(raw['price_gbp']):,.2f})")
        return "Source costs the same or more"
    if status == "MASKURA_GONE":
        return "Maskura page gone"
    if status == "MASKURA_UNREADABLE":
        return "Maskura page could not be read"
    if status == "NO_MASKURA_LINK":
        return "Not checked: no Maskura link in the sheet"
    if status == "NO_SOURCE_LINK":
        return "Not checked: no source link in the sheet"
    return "Still being checked"


SIMPLE_REASON_COLORS = [  # (Reason starts with, fill, text)
    ("It's okay", "D1E7DD", "0F5132"), ("No buyable source", "F8D7DA", "842029"),
    ("Could not check", "FFF3CD", "664D03"), ("Maskura page", "F1AEB5", "58151C"),
    ("Source costs", "FFE5D0", "8A3C00"), ("Not checked", "E2E3E5", "41464B"), ("Still", "E2E3E5", "41464B"),
]


def build_simple(ws, rows):
    """Products: the product (linked to Maskura), Source 1, Source 2 and the reason, as the owner asked."""
    cols = [("Maskura product", 46, "The product name from the sheet; it links to the Maskura page."),
            ("Source 1", 40, "The first source link from the sheet."),
            ("Source 2", 40, "The second source link from the sheet, if any."),
            ("Reason", 60, "Why the product needs action, or It's okay. The Details tab has prices and margins.")]
    style_header(ws, cols, {})
    base = Font(name=FONT, size=10)
    for r, (p, s, m, status, best, notes, mp, how) in enumerate(rows, 2):
        by_slot = {slot: url for slot, url, _, _ in s}
        put(ws, r, 1, p["name"] or "(no name in the sheet)", link=p["maskura_url"] or None)
        for slot, col in ((1, 2), (2, 3)):
            url = by_slot.get(slot) or ""
            put(ws, r, col, short_link(url), link=url or None)
        put(ws, r, 4, reason_text(status, s, mp, best))
        for col in range(1, 5):
            cell = ws.cell(row=r, column=col)
            if not cell.hyperlink:
                cell.font = base
            cell.alignment = Alignment(vertical="top", wrap_text=col in (1, 4))
    last = len(rows) + 1
    ws.auto_filter.ref = f"A1:D{last}"
    ws.freeze_panes = "B2"
    for start, fill, fg in SIMPLE_REASON_COLORS:
        ws.conditional_formatting.add(f"D2:D{last}", FormulaRule(formula=[f'LEFT($D2,{len(start)})="{start}"'],
                                                                 **fill_rule(fill, fg, bold=True)))
    ws.conditional_formatting.add(f"A2:C{last}", FormulaRule(formula=["MOD(ROW(),2)=0"],
                                                             fill=PatternFill("solid", fgColor="F6F8FA", bgColor="F6F8FA")))


def build(db, out):
    products, srcs = load(db)
    rows = []
    for p in products:
        s = srcs.get(p["id"], [])
        m, status, best, notes, mp, how = decide(p, s)
        rows.append((p, s, m, status, best, notes, mp, how))
    order = {k: i for i, (k, *_) in enumerate(STATUSES)}
    rows.sort(key=lambda r: (0 if r[0]["has_problem"] else 1, order.get(r[3], 99), r[0]["position"] or 0))

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    simple = wb.create_sheet("Products")
    ws = wb.create_sheet("Details")
    src_ws = wb.create_sheet("Sources")
    about = wb.create_sheet("About")

    build_simple(simple, rows)

    # Details
    supervisor_cols = range(22, 26)
    style_header(ws, PRODUCT_COLUMNS, {i: GOLD for i in supervisor_cols})
    base = Font(name=FONT, size=10)
    gbp = '£#,##0.00;[Red]-£#,##0.00;"-"'
    for r, (p, s, m, status, best, notes, mp, how) in enumerate(rows, 2):
        by_slot = {slot: (url, raw) for slot, url, raw, _ in s}
        words, todo, _, _ = STATUS.get(status, (status, "", "", ""))
        values = [p["position"], p["name"] or "", "Yes" if p["has_problem"] else "No", words, todo,
                  "; ".join(notes)]
        for col, v in enumerate(values, 1):
            put(ws, r, col, v)
        put(ws, r, 7, short_link(p["maskura_url"]), link=p["maskura_url"] or None)
        moved = (m or {}).get("moved_to")
        put(ws, r, 8, short_link(moved), link=moved or None)
        put(ws, r, 9, mp, fmt=gbp)
        put(ws, r, 10, HOW.get(how, "") if how else "")
        put(ws, r, 11, f"Source {best}" if best else "")
        put(ws, r, 12, f'=IF(K{r}="Source 1",Q{r},IF(K{r}="Source 2",T{r},""))', fmt=gbp)
        put(ws, r, 13, f'=IF(AND(ISNUMBER(I{r}),ISNUMBER(L{r})),I{r}-L{r},"")', fmt=gbp)
        put(ws, r, 14, f'=IF(AND(ISNUMBER(M{r}),N(I{r})>0),M{r}/I{r},"")', fmt='0.0%;[Red]-0.0%;"-"')
        for slot, col in ((1, 15), (2, 18)):
            url, raw = by_slot.get(slot, ("", {}))
            put(ws, r, col, short_link(url), link=url or None)
            put(ws, r, col + 1, result_words(raw) if url else "")
            price = raw.get("price_gbp") if url else None
            put(ws, r, col + 2, float(price) if price not in (None, "") else None, fmt=gbp)
        put(ws, r, 21, uk_time(p["last_crawled_at"]))
        put(ws, r, 22, "Not checked yet")
        put(ws, r, 24, None, fmt="dd mmm yyyy")
        for col in range(1, len(PRODUCT_COLUMNS) + 1):
            cell = ws.cell(row=r, column=col)
            if not cell.hyperlink:
                cell.font = base
            cell.alignment = Alignment(vertical="top", wrap_text=col in (2, 5, 6, 25))
            if col in supervisor_cols:
                cell.fill = PatternFill("solid", fgColor=INPUT_FILL)
    last = len(rows) + 1
    end_col = get_column_letter(len(PRODUCT_COLUMNS))
    ws.auto_filter.ref = f"A1:{end_col}{last}"
    ws.freeze_panes = "C2"
    dv = DataValidation(type="list", formula1='"' + ",".join(CHECK_CHOICES) + '"', allow_blank=True,
                        showDropDown=False)
    dv.add(f"V2:V{last}")
    ws.add_data_validation(dv)
    dv_date = DataValidation(type="date", operator="greaterThan", formula1="DATE(2026,1,1)", allow_blank=True)
    dv_date.add(f"X2:X{last}")
    ws.add_data_validation(dv_date)
    cf = ws.conditional_formatting
    for k, w, _, fill, fg in STATUSES:
        cf.add(f"D2:D{last}", FormulaRule(formula=[f'$D2="{w}"'], **fill_rule(fill, fg, bold=True)))
    cf.add(f"C2:C{last}", FormulaRule(formula=['$C2="Yes"'], **fill_rule("F8D7DA", "842029", bold=True)))
    cf.add(f"C2:C{last}", FormulaRule(formula=['$C2="No"'], **fill_rule("D1E7DD", "0F5132")))
    for rng in (f"P2:P{last}", f"S2:S{last}"):
        cf.add(rng, FormulaRule(formula=[f'{rng[0]}2="In stock"'], **fill_rule("D1E7DD", "0F5132")))
        cf.add(rng, FormulaRule(formula=[f'AND({rng[0]}2<>"",{rng[0]}2<>"In stock")'], **fill_rule("F8D7DA", "842029")))
    cf.add(f"M2:N{last}", FormulaRule(formula=['AND(ISNUMBER($M2),$M2>0)'], font=Font(color="0F5132", bold=True)))
    for choice, (fill, fg) in CHECK_COLORS.items():
        cf.add(f"V2:V{last}", FormulaRule(formula=[f'$V2="{choice}"'], **fill_rule(fill, fg, bold=True)))
    cf.add(f"A2:U{last}", FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", fgColor="F6F8FA", bgColor="F6F8FA")))

    # Sources
    style_header(src_ws, SOURCE_COLUMNS, {})
    sr = 2
    by_position = sorted(rows, key=lambda x: x[0]["position"] or 0)
    for p, s, *_ in by_position:
        for slot, url, raw, fetched in s:
            if not url:
                continue
            price = raw.get("price_gbp")
            vals = [p["position"], p["name"] or "", f"Source {slot}", (raw.get("host") or urlsplit(url).netloc).removeprefix("www."),
                    None, result_words(raw), float(price) if price not in (None, "") else None,
                    raw.get("variation_label") or "", raw.get("title") or "", uk_time(raw.get("fetched_at") or fetched)]
            for col, v in enumerate(vals, 1):
                cell = put(src_ws, sr, col, v, fmt=gbp if col == 7 else None)
                cell.font = base
                cell.alignment = Alignment(vertical="top", wrap_text=col in (2, 9))
            put(src_ws, sr, 5, short_link(url), link=url)
            sr += 1
    src_last = sr - 1
    fmt_last = max(src_last, 2)   # a sheet with no source links still gets a valid A1:J2 range
    src_ws.auto_filter.ref = f"A1:J{fmt_last}"
    src_ws.freeze_panes = "C2"
    scf = src_ws.conditional_formatting
    scf.add(f"F2:F{fmt_last}", FormulaRule(formula=['$F2="In stock"'], **fill_rule("D1E7DD", "0F5132")))
    scf.add(f"F2:F{fmt_last}", FormulaRule(formula=['$F2<>"In stock"'], **fill_rule("F8D7DA", "842029")))
    scf.add(f"A2:J{fmt_last}", FormulaRule(formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", fgColor="F6F8FA", bgColor="F6F8FA")))

    # Summary
    S = summary
    S.sheet_view.showGridLines = False
    for col, w in zip("ABCDEF", (34, 13, 12, 12, 42, 4)):
        S.column_dimensions[col].width = w
    S["A1"] = "Maskura UK source check"
    S["A1"].font = Font(name=FONT, bold=True, size=16, color=NAVY)
    first, n = 5, len(STATUSES)
    total_row = first + n
    S["A2"] = (f'=TEXT(C{total_row + 1},"#,##0")&" of "&TEXT(C{total_row},"#,##0")&'
               f'" products need action. A product is OK when a source you can buy costs less than Maskura\'s price '
               f'for the same variant."')
    S["A2"].font = Font(name=FONT, size=11)
    S["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    S.merge_cells("A2:E2")
    S.row_dimensions[2].height = 32
    heads = ["Status", "Needs action", "Products", "Share", "What to do"]
    for i, h in enumerate(heads, 1):
        c = S.cell(row=4, column=i, value=h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center")
    for i, (k, w, todo, fill, fg) in enumerate(STATUSES):
        r = first + i
        S.cell(row=r, column=1, value=w).fill = PatternFill("solid", fgColor=fill)
        S.cell(row=r, column=1).font = Font(name=FONT, bold=True, color=fg)
        S.cell(row=r, column=2, value=f'=IF(COUNTIFS(Details!$D:$D,$A{r},Details!$C:$C,"Yes")>0,"Yes","No")')
        S.cell(row=r, column=3, value=f"=COUNTIF(Details!$D:$D,$A{r})").number_format = "#,##0"
        S.cell(row=r, column=4, value=f"=IF($C${total_row}=0,0,C{r}/$C${total_row})").number_format = "0.0%"
        S.cell(row=r, column=5, value=todo)
    S.cell(row=total_row, column=1, value="All products")
    S.cell(row=total_row, column=3, value=f"=SUM(C{first}:C{total_row - 1})").number_format = "#,##0"
    S.cell(row=total_row, column=4, value=f"=IF(C{total_row}=0,0,C{total_row}/C{total_row})").number_format = "0.0%"
    S.cell(row=total_row + 1, column=1, value="Need action")
    S.cell(row=total_row + 1, column=3, value='=COUNTIF(Details!$C:$C,"Yes")').number_format = "#,##0"
    S.cell(row=total_row + 1, column=4, value=f"=IF(C{total_row}=0,0,C{total_row + 1}/C{total_row})").number_format = "0.0%"
    for r in (total_row, total_row + 1):
        for col in range(1, 6):
            S.cell(row=r, column=col).font = Font(name=FONT, bold=True)
            S.cell(row=r, column=col).border = Border(top=Side(style="thin", color=NAVY))
    for r in range(first, total_row + 2):
        for col in range(2, 5):
            S.cell(row=r, column=col).alignment = Alignment(horizontal="center")
        for col in (2, 3, 4, 5):
            if S.cell(row=r, column=col).font.name != FONT:
                S.cell(row=r, column=col).font = Font(name=FONT)

    sup = total_row + 3
    S.cell(row=sup, column=1, value="Supervisor checks").font = Font(name=FONT, bold=True, size=12, color=GOLD)
    for i, h in enumerate(["Result", "", "Needs action", "All products"], 1):
        if True:
            c = S.cell(row=sup + 1, column=i, value=h or None)
            c.font = Font(name=FONT, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=GOLD)
            c.alignment = Alignment(horizontal="center")
    for i, choice in enumerate(CHECK_CHOICES):
        r = sup + 2 + i
        S.cell(row=r, column=1, value=choice).font = Font(name=FONT)
        S.cell(row=r, column=3, value=f'=COUNTIFS(Details!$V:$V,$A{r},Details!$C:$C,"Yes")')
        S.cell(row=r, column=4, value=f"=COUNTIF(Details!$V:$V,$A{r})")
    right, wrong = sup + 3, sup + 4
    acc = sup + 2 + len(CHECK_CHOICES)
    S.cell(row=acc, column=1, value="Checker agrees with the supervisor").font = Font(name=FONT, bold=True)
    S.cell(row=acc, column=4, value=f'=IF(D{right}+D{wrong}=0,"No checks yet",D{right}/(D{right}+D{wrong}))').number_format = "0.0%"
    S.cell(row=acc, column=4).font = Font(name=FONT, bold=True)
    for r in range(sup + 2, acc + 1):
        for col in (3, 4):
            S.cell(row=r, column=col).alignment = Alignment(horizontal="center")
            if S.cell(row=r, column=col).font.name != FONT:
                S.cell(row=r, column=col).font = Font(name=FONT)

    leg = acc + 2
    S.cell(row=leg, column=1, value="How to use the Details tab").font = Font(name=FONT, bold=True, size=12, color=NAVY)
    tips = [
        ("Yellow columns", "Filled in by the supervisor: Supervisor check (a dropdown), Checked by, Check date and Supervisor note.", INPUT_FILL),
        ("Products tab", "Four columns: the product (linked to its Maskura page), Source 1, Source 2 and the reason. Sort or filter by Reason to group the problems.", None),
        ("Filters", "Use the filter arrows in the header row, for example Needs action = Yes, or one Status.", None),
        ("Colors", "Red: a problem. Amber: could not be read, check by hand. Green: OK. Gray: a link is missing in the sheet.", None),
        ("Links", "Every link opens the page. eBay links are shortened to the item number; the full link is kept.", None),
    ]
    for i, (k, v, fill) in enumerate(tips):
        r = leg + 1 + i
        a = S.cell(row=r, column=1, value=k)
        a.font = Font(name=FONT, bold=True)
        if fill:
            a.fill = PatternFill("solid", fgColor=fill)
        b = S.cell(row=r, column=2, value=v)
        b.font = Font(name=FONT)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        S.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        S.row_dimensions[r].height = 30

    chart = BarChart()
    chart.type = "bar"
    chart.title = "Products by status"
    chart.legend = None
    chart.y_axis.title = "Products"
    chart.add_data(Reference(S, min_col=3, min_row=4, max_row=total_row - 1), titles_from_data=True)
    chart.set_categories(Reference(S, min_col=1, min_row=first, max_row=total_row - 1))
    chart.height, chart.width = 9, 18
    chart.gapWidth = 40
    chart.x_axis.scaling.orientation = "maxMin"   # top-to-bottom in the Summary's order
    S.add_chart(chart, "G4")

    # About
    A = about
    A.column_dimensions["A"].width = 30
    A.column_dimensions["B"].width = 100
    A["A1"] = "About this file"
    A["A1"].font = Font(name=FONT, bold=True, size=14, color=NAVY)
    crawled = [p["last_crawled_at"] for p in products if p["last_crawled_at"]]
    lines = [
        ("Source", f"Maskura source checker database, full check of every link in the All Products sheet "
                   f"({len(products):,} products). Pages last read {uk_time(min(crawled)) if crawled else ''} to "
                   f"{uk_time(max(crawled)) if crawled else ''} (UK time)."),
        ("Built", f"{uk_time(datetime.datetime.now(datetime.timezone.utc).isoformat())} (UK time)."),
        ("The rule", "A product is OK when at least one source can be bought and costs less than Maskura's price for "
                     "the same variant. The variant named in the Maskura link is used; otherwise the variant whose "
                     "name matches the source; otherwise the cheapest Maskura variant. An equal price is not OK."),
    ]
    for k, w, todo, *_ in STATUSES:
        meaning = {
            "NO_BUYABLE_SOURCE": "Every source link is sold out, ended or gone.",
            "COULD_NOT_CHECK": "Every source page failed to load (often an Amazon bot page), so nothing is known.",
            "MASKURA_GONE": "The Maskura link returns page not found, even after following any redirect.",
            "SOURCE_DEARER": "A source can be bought, but it costs the same as Maskura or more.",
            "MASKURA_UNREADABLE": "The Maskura page could not be read this time.",
            "OK": "A buyable source costs less than Maskura's price.",
            "NO_MASKURA_LINK": "The sheet row has no Maskura link, so the product cannot be checked.",
            "NO_SOURCE_LINK": "The sheet row has no source link, so there is nothing to compare.",
            "CHECKING": "The check had not finished for this product.",
        }[k]
        lines.append((f"Status: {w}", meaning))
    lines += [
        ("Listing ended or sold out", "eBay answered the link with a page-gone code but still showed the listing as "
                                      "ended or out of stock. A browser sample of 10 such links on 24 Oct 2026 found 9 "
                                      "marked ENDED; eBay blocked the tenth."),
        ("Margin", "Maskura price minus the best source price, worked out by formula in the Details tab."),
    ]
    for i, (k, v) in enumerate(lines, 3):
        a = A.cell(row=i, column=1, value=k)
        a.font = Font(name=FONT, bold=True)
        a.alignment = Alignment(vertical="top")
        b = A.cell(row=i, column=2, value=v)
        b.font = Font(name=FONT)
        b.alignment = Alignment(wrap_text=True, vertical="top")

    simple.sheet_properties.tabColor = "C00000"
    for sheet in (S, ws, src_ws, A):
        sheet.sheet_properties.tabColor = {"Summary": NAVY, "Details": "2D8B8B", "Sources": "6B7A8F", "About": "A8A8A8"}[sheet.title]
    wb.calculation.fullCalcOnLoad = True   # no cached values from openpyxl; Excel works them out on open
    wb.save(out)
    return {"products": len(rows), "sources": src_last - 1,
            "needs_action": sum(1 for r in rows if r[0]["has_problem"]),
            "status": {k: sum(1 for r in rows if r[3] == k) for k, *_ in STATUSES}}

