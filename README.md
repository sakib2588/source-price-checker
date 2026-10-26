# Source Price Checker

Checks a dropshipping sourcing sheet: for every product, it reads our shop's price and each supplier
listing's price and stock (eBay, Amazon and others), then writes an Excel review sheet that lists the
products needing action.

It was built for Maskura (maskura.co.uk, a Shopify store whose products are sourced from eBay and Amazon).
The shop side reads Shopify's public product JSON, so it works with that store layout.

## What it decides

For each product it compares the shop price with every buyable source:

| Status | Meaning |
|---|---|
| OK | A source can be bought and costs less than our price for the same variant |
| Source costs the same or more | A source can be bought, but our price is not higher |
| No buyable source | Every source is sold out, ended or gone |
| Maskura page gone / not read | The shop link is broken or could not be read |
| Could not check | Every source page failed to load (often an Amazon bot page) |

**Variants.** The variant named in the shop link (`?variant=`) is used first; otherwise the variant whose
name matches the source; otherwise the cheapest shop variant. When an eBay link names its variation
(`?var=`), that variation's price and stock are read.

Prices are only compared, never changed. Fixing a price or a source is done by a person in the shop admin.

## Setup

Tested with Python 3.13.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## The sheet

A `.csv` or `.xlsx` file whose **first row** holds the column names:

| Product Name | Maskura Link | Source 1 | Source 2 |
|---|---|---|---|
| Product title | Shop product link | Supplier link | Backup supplier link (optional) |

- Up to `Source 10` is read. Case, spaces and punctuation in the names do not matter (`Source1`, `SRC 2`).
- Other columns are ignored. A cell with `N/A` or `TBC` counts as no link.
- In `.xlsx`, a cell showing text such as "Link" keeps the URL behind it. Only the first tab with data is read.

## Run it

```bash
.venv/bin/python cli.py check "All Products.csv" --limit 5   # try the first 5 products
.venv/bin/python cli.py check "All Products.csv"             # every product
.venv/bin/python cli.py review --out review.xlsx             # rebuild the workbook from stored results
```

Results are kept in `checks.db` (SQLite; change with `--db`). The workbook (`review.xlsx` by default) has
Summary, Products, Details, Sources and About tabs. `Ctrl+C` stops a run and still writes the workbook.

**Pace.** Requests to one site are spaced out (a few seconds apart, with back-off when a site refuses), so a
sheet of about 1,600 products takes several hours. eBay and Amazon may refuse automated requests: in our
runs Amazon showed a bot-check page for every link, and eBay refused 3 to 15 percent of requests. Their terms
of use do not allow automated access without permission; eBay's official Browse API is the supported way to
read eBay prices.

## Tests

```bash
.venv/bin/python -m unittest discover -p 'test_*.py'
```

The tests make no network requests. `fixtures/` holds trimmed copies of eBay item pages used by the eBay
reader's tests.

## Layout

| File | Role |
|---|---|
| `cli.py` | Command line: sheet in, review workbook out |
| `sheet.py` | Reads the sourcing sheet |
| `sc_crawl.py` | Fetches pages, paces requests per site, decides each product's status |
| `sources/` | Page readers for eBay, Amazon, the shop, and other sites |
| `sc_variants.py` | Which shop variant price a source is compared with |
| `sc_store.py` | SQLite store: products, results, check jobs |
| `sc_worker.py` | Runs a check job across sites in parallel threads |
| `sc_review_book.py` | Builds the Excel review workbook |
| `sc_labels.py`, `outcomes.py`, `title_quality.py`, `sc_urls.py` | Wording, outcome rules, title checks, URL handling |
