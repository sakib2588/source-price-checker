"""Every user-facing word in the Source Checker: problems, Maskura and review states, archive reasons,
preset "No change needed" reasons, run-state sentences and the legend.

The API, CSV, Excel, the on-page legend, STATUS_GLOSSARY.md and /source-check/help all read from here.
Pure data and pure functions; no I/O.

Severity is one of "bad", "warn", "ok", "neutral" (the legend also uses "faint" for archived rows).
"""
from __future__ import annotations

import re

from . import title_quality as tq

SEVERITY_ORDER = ("bad", "warn", "neutral", "ok")
_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}

BEING_CHECKED = {"word": "Being checked", "severity": "neutral"}
NO_PROBLEM = {"word": "No problem found", "severity": "ok"}

# Problem suffixes: the part after "S{i}_", plus the whole code for Maskura ("M_ERR").
_PROBLEMS = {
    "INVERTED":      {"word": "Source costs more than Maskura price", "severity": "bad",
                      "meaning": "The supplier price is higher than the price on Maskura."},
    "OOS":           {"word": "Source out of stock", "severity": "warn",
                      "meaning": "The supplier page says the item is out of stock."},
    "GONE":          {"word": "Listing removed", "severity": "warn",
                      "meaning": "The supplier listing has been removed or has ended, or the link now "
                                 "leads to a different item."},
    "VARIANT":       {"word": "Variant may differ", "severity": "warn",
                      "meaning": "The supplier title is missing a size, pack count or color that the "
                                 "Maskura title names."},
    "VARIATION":     {"word": "Variation not checked", "severity": "warn",
                      "meaning": "The link names one variation, and the price shown may belong to another."},
    "VARIANT_UNCLEAR": {"word": "Variation not named", "severity": "warn",
                        "meaning": "The supplier sells variations at different prices and the link does not "
                                   "say which one, so the margin depends on which variation is bought."},
    "PRICE_SUSPECT": {"word": "Price looks wrong", "severity": "warn",
                      "meaning": "The two prices are so far apart that one of them was probably misread."},
    "CURRENCY":      {"word": "Not in pounds", "severity": "warn",
                      "meaning": "The supplier price is not in pounds, so it is not compared."},
    "ERR":           {"word": "Could not read", "severity": "warn",
                      "meaning": "The supplier page could not be read after every retry."},
    "M_ERR":         {"word": "Could not read Maskura page", "severity": "warn",
                      "meaning": "The Maskura page could not be read after every retry."},
    "PENDING":       dict(BEING_CHECKED, meaning="This link has not been checked yet."),
    "BLOCKED_RETRY": dict(BEING_CHECKED, meaning="The site refused the last request. It will be tried again."),
}
TRANSIENT_SUFFIXES = frozenset({"PENDING", "BLOCKED_RETRY"})
_UNKNOWN = {"word": "Check this row", "severity": "warn"}

_CODE_RE = re.compile(r"^S(\d{1,3})_([A-Z_]+)$")


def all_problem_suffixes() -> tuple:
    """Every suffix that has a label. Codes are "S{i}_<suffix>", or "M_ERR" for the Maskura page."""
    return tuple(_PROBLEMS)


def _split_code(code: str):
    code = str(code or "").strip()
    if code.startswith("M_"):
        return None, code
    m = _CODE_RE.match(code)
    if not m:
        return None, code
    return int(m.group(1)), m.group(2)


def source_label(slot, platform=None) -> str:
    """"eBay, link 2", or "Link 2" when the platform is unknown."""
    if platform:
        return f"{tq.platform_label(platform)}, link {slot}"
    return f"Link {slot}"


def problem(code, platform_label=None) -> dict:
    """{code, word, source_label, severity} for one flag. `platform_label` may be a platform key
    ("ebay") or a label ("eBay"); both give "eBay, link 2"."""
    slot, suffix = _split_code(code)
    entry = _PROBLEMS.get(suffix, _UNKNOWN)
    if suffix.startswith("M_"):
        label = "Maskura"
    elif slot is not None:
        label = source_label(slot, platform_label)
    else:
        label = ""
    return {"code": str(code), "word": entry["word"], "source_label": label, "severity": entry["severity"]}


def _flag_list(flags) -> list:
    if not flags:
        return []
    if isinstance(flags, str):
        return [f.strip() for f in flags.split(";") if f.strip()]
    return [str(f).strip() for f in flags if f and str(f).strip()]


def is_transient(code) -> bool:
    return _split_code(code)[1] in TRANSIENT_SUFFIXES


def problems(flags, platforms) -> list:
    """The final problems of a row, most severe first (stable within a severity). Transient states
    (PENDING, BLOCKED_RETRY) are not problems and are left out. `platforms[i]` is slot i+1's platform."""
    platforms = list(platforms or [])
    out = []
    for code in _flag_list(flags):
        if is_transient(code):
            continue
        slot, _ = _split_code(code)
        plat = platforms[slot - 1] if slot and 0 < slot <= len(platforms) else None
        out.append(problem(code, plat))
    return sorted(out, key=lambda p: _RANK.get(p["severity"], len(_RANK)))


def row_problem_cell(flags, platforms, being_checked=False) -> dict:
    """{word, severity, more} for the table cell: the most severe word plus how many more.

    A row with any transient flag, or one the store marks as being checked (imported but never crawled has
    no flags at all), shows "Being checked": it is not reviewable yet. `more` then counts the final
    problems found so far."""
    found = problems(flags, platforms)
    if being_checked or any(is_transient(c) for c in _flag_list(flags)):
        return {"word": BEING_CHECKED["word"], "severity": BEING_CHECKED["severity"], "more": len(found)}
    if not found:
        return {"word": NO_PROBLEM["word"], "severity": NO_PROBLEM["severity"], "more": 0}
    return {"word": found[0]["word"], "severity": found[0]["severity"], "more": len(found) - 1}


# ── Maskura, review, archive ─────────────────────────────────────────────────

MASKURA_STATUS = {
    "OK":                      {"word": "OK", "severity": "ok"},
    "LINK_CHANGED_CONFIRM":    {"word": "Link changed: confirm", "severity": "warn"},
    "LINK_CHANGED_SUGGESTION": {"word": "Link changed: suggestion", "severity": "warn"},
    "READ_ERROR":              {"word": "Could not read Maskura page", "severity": "warn"},
    "NOT_ON_MASKURA":          {"word": "Not on Maskura", "severity": "bad"},
    "NO_LINK":                 {"word": "No link", "severity": "neutral"},
    "LINK_BROKEN":             {"word": "Maskura link broken", "severity": "warn"},
}
_MASKURA_MEANING = {
    "OK": "The Maskura page was read.",
    "LINK_CHANGED_CONFIRM": "The link was corrected automatically. Confirm that the new link is right.",
    "LINK_CHANGED_SUGGESTION": "A possible new link was found. Open it and confirm or edit it.",
    "READ_ERROR": "The Maskura page could not be read. It is tried again; this never changes the link.",
    "NOT_ON_MASKURA": "The product is not in the Maskura catalog.",
    "NO_LINK": "The sheet has no Maskura link for this row.",
    "LINK_BROKEN": "The Maskura page at this link is gone. A link check will look for its new address.",
}

REVIEW_STATUS = {
    "to_review":            {"word": "To review", "severity": "neutral"},
    "no_change_needed":     {"word": "No change needed", "severity": "ok"},
    "fixed_rechecking":     {"word": "Fixed: re-checking", "severity": "warn"},
    "fixed_confirmed":      {"word": "Fixed, confirmed", "severity": "ok"},
    "still_differs":        {"word": "Still differs", "severity": "bad"},
    "changed_since_review": {"word": "Changed since review", "severity": "warn"},
}
_REVIEW_MEANING = {
    "to_review": "Nobody has reviewed this problem yet.",
    "no_change_needed": "Someone checked the problem and decided nothing needs to change.",
    "fixed_rechecking": "Marked fixed. The Maskura page is being checked again.",
    "fixed_confirmed": "Marked fixed, and the re-check agrees.",
    "still_differs": "Marked fixed, but the re-check still finds the problem.",
    "changed_since_review": "The results changed after the last review. Look again.",
}

ARCHIVE_REASONS = {
    "not_sold": "Not sold any more",
    "duplicate": "Duplicate",
    "not_a_product": "Not a product row",
    "other": "Other",
}

CLAIM = {"word": "Reviewing: {name}", "severity": "neutral"}
TAGS = {
    "not_in_sheet": {"word": "No longer in sheet", "severity": "neutral"},
    "possible_duplicate": {"word": "Possible duplicate", "severity": "warn"},
}
ARCHIVED = {"word": "Archived ({reason})", "severity": "faint"}

# Preset reasons for "No change needed", picked by number key. Keyed by problem suffix.
NO_CHANGE_REASONS = {
    "INVERTED": ("Checked: the price is right", "Supplier offer is temporary",
                 "We buy from a different supplier now", "Trade or bulk price is lower", "Price change planned"),
    "OOS": ("Back in stock soon", "We buy from a different supplier now", "We hold our own stock",
            "Supplier restocks often"),
    "GONE": ("We buy from a different supplier now", "Supplier link replaced in the sheet",
             "We hold our own stock", "Listing came back"),
    "VARIANT": ("Checked: same variant", "Title wording differs, item is the same",
                "Supplier sells the variant under another listing"),
    "VARIATION": ("Checked: the variation price is right", "Only one variation is sold",
                  "Price is the same for every variation"),
    "VARIANT_UNCLEAR": ("Checked: the variation we buy is cheaper", "Variation added to the source link",
                        "We buy a different variation now"),
    "PRICE_SUSPECT": ("Checked: both prices are right", "Bundle or pack priced differently",
                      "Supplier price is per unit"),
    "CURRENCY": ("Checked the price by hand", "Supplier invoices in pounds", "Not a supplier we use"),
    "ERR": ("Checked the page by hand", "Supplier page needs a login", "Not a supplier we use"),
    "M_ERR": ("Checked the Maskura page by hand", "Page is hidden on purpose", "Product is being replaced"),
}

# Sentence templates for the run state. The browser fills {time} (local time) and {name}.
# The products table (DESIGN.md Section 10.3). One definition drives the header, the cell and the phone label;
# static/sc/columns.js has one renderer per key (test_sc_page checks they match). `sort` names a
# sc_store.SORTS key; a column without it is not sortable.
COLUMNS = (
    {"key": "row", "label": "Row", "sort": "row"},
    {"key": "product", "label": "Product", "sort": "name"},
    {"key": "problem", "label": "Problem"},
    {"key": "maskura", "label": "Maskura"},
    {"key": "best_source", "label": "Cheapest source"},
    {"key": "review", "label": "Review"},
    {"key": "checked", "label": "Last checked", "sort": "checked"},
)

# Summary tiles: each reads "N of TOTAL <noun>" with a share bar.
TILES = (
    {"key": "needs_action", "label": "Needs action", "noun": "products",
     "hint": "Rows with a problem that a person can act on now."},
    {"key": "reviewed", "label": "Reviewed", "noun": "with a problem",
     "hint": "Resolved rows out of every row that has or had a problem."},
    {"key": "being_checked", "label": "Being checked", "noun": "products",
     "hint": "Rows waiting for a link to be read. They cannot be reviewed yet."},
    {"key": "link_broken", "label": "Maskura link broken", "noun": "products",
     "hint": "The Maskura page was not found. Usually the store renamed the product URL."},
)


# Why a write was refused (a 409). "{name}" and "{time}" are filled in: the name by the server, the time by the
# browser from the conflict's "at", in the viewer's zone.
CONFLICTS = {
    "version": "{name} changed this at {time}.",
    "being_checked": "This row is being checked, so it cannot be reviewed yet.",
    "archived": "This row is archived. Restore it first.",
    "already_archived": "This row is already archived.",
    "not_archived": "This row is not archived.",
    "claimed": "{name} is reviewing this row.",
    "nothing_to_confirm": "There is no changed link to confirm on this row.",
    "changed_since": "It was changed again since, so it was not undone.",
    "already_undone": "That change was already undone.",
    "not_undoable": "That change cannot be undone.",
    "sheet_applied": "{name} applied another sheet at {time}, after this preview. Upload the file again to see a "
                     "fresh report.",
}


def conflict_text(reason, name=None) -> str:
    return CONFLICTS.get(reason, "Someone else changed this first.").replace("{name}", name or "Someone")


# The sheet upload report (DESIGN.md Section 10.14), in the order the dialog shows it. Keys are the store's
# import report categories.
UPLOAD_REPORT = (
    ("matched", "Matched", "Rows that match a product already here. Its history, review and notes are kept.", "ok"),
    ("new", "New", "Rows not seen before. They are added and wait as Being checked until the next check.",
     "neutral"),
    ("gone", "No longer in the sheet", "Products here that this sheet leaves out. They are kept and marked, never "
     "deleted.", "warn"),
    ("link_differs", "Sheet link differs", "The sheet has another Maskura link than the one corrected here. The "
     "corrected link is kept.", "warn"),
    ("possible_duplicates", "Possible duplicates", "Rows that look like the same product as another row.", "warn"),
    ("archived_in_sheet", "Archived but in the sheet", "Rows archived here that are still in the sheet. They stay "
     "archived.", "neutral"),
    ("no_link", "No link", "Rows with no usable Maskura link, so no margin can be worked out for them.", "bad"),
)


def upload_words():
    return [{"key": k, "label": label, "meaning": meaning, "severity": sev} for k, label, meaning, sev in UPLOAD_REPORT]


# History and Recent changes: one phrase per event kind (sc_store writes the kinds).
EVENT_WORDS = {
    "imported": "Added from the sheet",
    "back_in_sheet": "Back in the sheet",
    "gone_from_sheet": "No longer in the sheet",
    "sheet_link_changed": "Maskura link changed in the sheet",
    "source_url_changed": "Supplier link changed in the sheet",
    "review": "Review changed",
    "review_outcome": "Re-check result",
    "no_change_needed": "Marked No change needed",
    "fixed_rechecking": "Marked fixed",
    "note": "Note added",
    "claim": "Started reviewing",
    "release": "Stopped reviewing",
    "archive": "Archived",
    "restore": "Restored",
    "set_link": "Maskura link edited",
    "confirm_link": "Maskura link confirmed",
    "undo": "Undone",
}


def event_word(kind, changes=None) -> str:
    """The history phrase. A review change names the new status ("Review: No change needed")."""
    if kind == "review" and isinstance(changes, dict):
        pair = changes.get("review_status")
        if isinstance(pair, list) and len(pair) == 2 and pair[1] in REVIEW_STATUS:
            return "Review: " + REVIEW_STATUS[pair[1]]["word"]
    return EVENT_WORDS.get(kind, "Changed")


# "No change needed" on a row that has no problem-specific preset reasons.
DEFAULT_NO_CHANGE_REASONS = ("Checked: nothing needs to change", "Already handled outside the portal")

CLAIM_FILTERS = (("mine", "Mine"), ("unclaimed", "Unclaimed"), ("stale", "Stale"), ("claimed", "Claimed by anyone"))


def filter_options() -> dict:
    """The words for the filter menus. Values are the API's filter values."""
    problems = [("any", "Any problem"), ("none", NO_PROBLEM["word"])]
    problems += [(k, v["word"]) for k, v in _PROBLEMS.items() if k not in TRANSIENT_SUFFIXES]
    return {
        "problem": problems,
        "maskura": [(k, v["word"]) for k, v in MASKURA_STATUS.items()],
        "review": [(k, v["word"]) for k, v in REVIEW_STATUS.items()],
        "claim": list(CLAIM_FILTERS),
    }


# The progress panel: what a job is doing, and a host's state.
JOB_KINDS = {
    "full": "Checking all products",
    "recheck": "Re-checking products",
    "retry": "Retrying products",
    "catalog_refresh": "Reading the Maskura catalog",
    "link_fix": "Fixing Maskura links",
    "changed": "Checking new and changed links",
}
HOST_STATE = {
    "challenge": "{name} asked for a bot check; paused until {time}",
    "cooling_down": "{name} is cooling down until {time}",
}

# The progress panel's own sentences. {n}, {done} and {total} are filled in by the browser.
PROGRESS = {
    "start": "Check all {n} products",
    "start_changed": "Check new and changed ({n} products)",
    "nothing_changed": "Every product was checked in the last 7 days",
    "skipped": "{n} archived are skipped",
    "checked": "{done} of {total} products checked",
    "eta_unknown": "Working out the time left",
    "host_ok": "Working",
    "host_waiting": "Waiting",
    "paused_by_hand": "Paused by hand",
    "no_host": "Links with no site",
    "more_hosts": "{n} more sites",
    "worker_down": "The worker is not running, so nothing is being checked. It starts with the portal service.",
    "stop_title": "Stop this check?",
    "stop_lede": "Rows that were not checked yet stay Being checked. A new check can be started any time.",
}

RUN_STATE = {
    "idle": "No check is running",
    "queued": "Check queued at {time}",
    "running": "Checking since {time}",
    "paused": "Paused by {name} at {time}",
    "paused_auto": "Paused automatically at {time}: {name} is refusing too many requests",
    "stopping": "Stopping after the current pages",
    "stopped": "Stopped by {name} at {time}",
    "done": "Finished at {time}",
    "error": "The check stopped with an error at {time}",
    "cooling_down": "{name} is cooling down until {time}",
    "offline": "The internet connection looks down since {time}. The check waits and carries on",
    "catalog_refresh": "Reading the Maskura catalog since {time}",
    "link_fix": "Fixing Maskura links since {time}",
}


def legend() -> list:
    """Rows of {column, word, severity, meaning} for the on-page legend and STATUS_GLOSSARY.md."""
    rows = [{"column": "Check", "word": BEING_CHECKED["word"], "severity": BEING_CHECKED["severity"],
             "meaning": "A link is not checked yet, or is waiting for a retry. It is not a problem and "
                        "cannot be reviewed yet."}]
    for suffix, entry in _PROBLEMS.items():
        if suffix in TRANSIENT_SUFFIXES:
            continue
        rows.append({"column": "Problem", "word": entry["word"], "severity": entry["severity"],
                     "meaning": entry["meaning"]})
    rows.append({"column": "Problem", "word": NO_PROBLEM["word"], "severity": NO_PROBLEM["severity"],
                 "meaning": "Every link was read and nothing needs attention."})
    for key, entry in MASKURA_STATUS.items():
        rows.append({"column": "Maskura", "word": entry["word"], "severity": entry["severity"],
                     "meaning": _MASKURA_MEANING[key]})
    for key, entry in REVIEW_STATUS.items():
        rows.append({"column": "Review", "word": entry["word"], "severity": entry["severity"],
                     "meaning": _REVIEW_MEANING[key]})
    rows.append({"column": "Claim", "word": CLAIM["word"], "severity": CLAIM["severity"],
                 "meaning": "Someone is reviewing this row. The claim ends after 2 hours without activity."})
    rows.append({"column": "Tag", "word": TAGS["not_in_sheet"]["word"],
                 "severity": TAGS["not_in_sheet"]["severity"],
                 "meaning": "The row was not in the last uploaded sheet."})
    rows.append({"column": "Tag", "word": TAGS["possible_duplicate"]["word"],
                 "severity": TAGS["possible_duplicate"]["severity"],
                 "meaning": "Another row has the same Maskura link. Keep both, or archive this one."})
    for key, word in ARCHIVE_REASONS.items():
        rows.append({"column": "Archived", "word": word, "severity": "faint",
                     "meaning": f"Archive reason: {word}. Shown with \"Show archived\"."})
    return rows
