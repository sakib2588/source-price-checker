"""Named outcomes for one crawled source page.

Every source ends in exactly one of these, so a person can filter "confirmed" from "uncertain"
instead of reading free-text error strings.

  PRICE           a price was read (the stock label may still say unavailable)
  OUT_OF_STOCK    the page says unavailable and shows no price
  NOT_FOUND       the listing is gone (removed page, HTTP 404 or 410)
  ENDED_REDIRECT  the listing ended, or the link now leads to a different item
  NO_CONTENT      the page never showed product content (a script-only shell, or empty)
  PARSER_GAP      product content is present but no price or status could be read
  BLOCKED         the site refused the request (403, 401, 429, a challenge or robot page)
  ERROR           a network failure, timeout, server error or parse failure
  NON_GBP         a price was read in a currency that is not pounds and is not compared

Pure function; no I/O.
"""
from __future__ import annotations

import re

VERDICTS = ("PRICE", "OUT_OF_STOCK", "NOT_FOUND", "ENDED_REDIRECT", "NO_CONTENT",
            "PARSER_GAP", "BLOCKED", "ERROR", "NON_GBP")

# States that carry a named error string (the staged data uses the same convention). Definitive
# states such as NOT_FOUND or OUT_OF_STOCK are not failures and carry no error.
FAILURE_VERDICTS = {"NO_CONTENT", "PARSER_GAP", "BLOCKED", "ERROR"}

# Phrases from real challenge pages. "captcha" must stand alone: an identifier such as
# captcha_token inside a parse error is not a bot wall.
BLOCK_RE = re.compile(
    r"robot check|\bcaptcha\b|bot-wall|challenge-platform|access denied|unusual traffic|"
    r"verify you are|attention required|pardon our interruption|just a moment", re.I)


def classify(sd):
    """(verdict, reason) from the fields an extractor and the fetch layer produced.

    Reads: title, price_gbp, available, error, http_status, state, redirected, non_gbp.
    """
    error = str(sd.get("error") or "")
    status = sd.get("http_status")

    if error:
        if error.startswith("Fetch failed"):
            if status in (404, 410):
                return "NOT_FOUND", f"HTTP {status}, the page no longer exists"
            if status in (401, 403):
                return "BLOCKED", f"HTTP {status}, the site refused the request"
            if status == 429:
                return "BLOCKED", "HTTP 429, the site is rate limiting requests"
            return "ERROR", error[:120]
        if BLOCK_RE.search(re.sub(r"https?://\S+", "", error)):
            return "BLOCKED", error[:120]
        return "ERROR", error[:120]

    state = sd.get("state") or ""
    if state == "removed":
        return "NOT_FOUND", "the page says the listing no longer exists"
    if sd.get("redirected"):
        return "ENDED_REDIRECT", "the link now leads to a different item"
    if state == "ended":
        return "ENDED_REDIRECT", "the listing has ended"
    if sd.get("price_gbp") is not None and sd.get("non_gbp"):
        return "NON_GBP", "price is not in pounds, so it is not compared"
    if sd.get("price_gbp") is not None:
        return "PRICE", ""
    if sd.get("available") is False:
        return "OUT_OF_STOCK", "page says unavailable"
    if state == "shell":
        return "NO_CONTENT", "eBay shell page, no product content"
    if sd.get("title"):
        return "PARSER_GAP", "product page with no readable price or status"
    return "NO_CONTENT", "page has no product content"
