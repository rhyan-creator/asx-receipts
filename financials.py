"""Pulls headline financials out of ASX half-year and annual reports.

From the income statement: revenue, profit before tax, finance costs / income, D&A, net profit.
From the balance sheet: cash, borrowings, lease liabilities, total equity.
From the notes: basic EPS, weighted-average shares, ordinary shares on issue.
EBIT and EBITDA aren't statutory lines, so they're built:
    EBIT   = profit before tax + finance costs − finance income
    EBITDA = EBIT + depreciation & amortisation
Every value comes with the prior-period comparative so the next report can cross-check it.
"""
from __future__ import annotations

import re
from datetime import datetime

import pymupdf

from .parse import (page_text, NUM, _merge_tokens, _norm, _period_end_from_text, _rows, _snap_to_month_end, _to_number,
                    _unit_multiplier, _currency, resolve_period_end)

# ---------------------------------------------------------------- line patterns
PL_PAGE = re.compile(r"statement\s+of\s+(profit\s+or\s+loss|comprehensive\s+income|financial\s+performance)|"
                     r"income\s+statement|profit\s+and\s+loss\s+statement", re.I)
BS_PAGE = re.compile(r"statement\s+of\s+financial\s+position|balance\s+sheet", re.I)
NOT_STATEMENT = re.compile(r"five[- ]year|5[- ]year|summary|highlights|key\s+metrics|chair|ceo|directors'?\s+report", re.I)

L = {
    "revenue_total": re.compile(r"^total\s+(revenue|revenues|sales)(\s+from\s+(contracts\s+with\s+customers|operations|ordinary\s+activities))?\b(?!.*other\s+income)", re.I),
    "revenue": re.compile(r"^(insurance\s+revenue|revenue|revenues|sales|sales\s+revenue|operating\s+revenue|revenue\s+from\s+(contracts\s+with\s+customers|"
                          r"continuing\s+operations|operations|ordinary\s+activities|sale\s+of\s+goods|services))\b", re.I),
    "pbt": re.compile(r"(profit|loss|profit\s*/\s*\(loss\)|\(loss\)\s*/\s*profit|loss\s*/\s*profit|profit\s*/\s*loss|earnings)\s+"
                      r"(before\s+(income\s+)?tax)", re.I),
    "npat": re.compile(r"^(net\s+)?(profit|loss|profit\s*/\s*\(loss\)|\(loss\)\s*/\s*profit|loss\s*/\s*profit|profit\s*/\s*loss)"
                       r"(\s+after\s+(income\s+)?tax)?(\s+(expense|benefit))?(\s+for\s+the\s+(year|period|half[- ]year|financial\s+year))?"
                       r"(\s+from\s+continuing\s+operations)?(\s+attributable\s+to\s+(the\s+)?(owners|members|shareholders|equity\s+holders))?", re.I),
    "fin_cost": re.compile(r"^(finance\s+(costs?|expenses?)|interest\s+(expense|costs?)|borrowing\s+costs)\b", re.I),
    "fin_income": re.compile(r"^(finance\s+income|interest\s+(income|revenue|received))\b", re.I),
    "net_fin": re.compile(r"^net\s+(finance|financing|interest)\s+(costs?|expenses?|income)", re.I),
    "da_total": re.compile(r"^(total\s+)?(depreciation\s*(,|and|&)\s*amortisation|amortisation\s*(and|&)\s*depreciation|"
                           r"depreciation,\s*amortisation\s*(and|&)\s*impairment|depreciation\s+and\s+amortisation\s+expenses?)", re.I),
    "da_part": re.compile(r"^(depreciation|amortisation)\b(?!.*accumulated)", re.I),
    "cash": re.compile(r"^cash\s+(and\s+cash\s+equivalents|and\s+short[- ]term\s+deposits|at\s+bank(\s+and\s+(on\s+hand|in\s+hand))?|assets)\b", re.I),
    "borrow": re.compile(r"^((non[- ])?current\s+)?(interest[- ]bearing\s+)?(loans\s+and\s+)?borrowings|^interest[- ]bearing\s+(loans|liabilities)|"
                         r"^bank\s+(loans?|overdrafts?|facilit)|^loans\s+(payable|from)|^convertible\s+notes?|^notes\s+payable|"
                         r"^debt\b|^financial\s+liabilities\s*-?\s*borrowings", re.I),
    "lease": re.compile(r"^((non[- ])?current\s+)?lease\s+liabilit|^right[- ]of[- ]use\s+(lease\s+)?liabilit|^leases?\b(?!.*receivable)", re.I),
    "equity": re.compile(r"^total\s+(equity|shareholders'?\s+equity)\b", re.I),
    "eps": re.compile(r"basic\s+(and\s+diluted\s+)?(earnings|\(loss\)|loss|profit)?\s*(/\s*\(loss\)\s*)?(per\s+share|eps)|"
                      r"basic\s+eps|earnings\s+per\s+share\s*-?\s*basic", re.I),
    "wa_shares": re.compile(r"weighted\s+average\s+number\s+of\s+(ordinary\s+)?shares", re.I),
    "issued": re.compile(r"^(fully\s+paid\s+)?ordinary\s+shares(\s*-\s*fully\s+paid|\s+fully\s+paid|\s+on\s+issue|\s+issued)?\b|"
                         r"^balance\s+at\s+(the\s+)?end\s+of\s+(the\s+)?(year|period|half[- ]year)|^closing\s+balance", re.I),
}
EXCL = re.compile(r"other\s+(revenue|income)|cost\s+of|per\s+share|deferred|unearned|receivable|tax\s+(expense|benefit)\s*$|"
                  r"accumulated|comprehensive\s+income\s+for|non[- ]controlling", re.I)


# ---------------------------------------------------------------- row helpers
def _label_text(row) -> str:
    return _norm(" ".join(w[4] for w in row if not NUM.match(_norm(w[4])) or re.search(r"[a-z]", w[4], re.I)))


_COL_CACHE: dict[int, list[float] | None] = {}
COL_HEAD = re.compile(r"^(\$|a\$|us\$)?['\u2019]?000s?$|^\$$|^\$m$|^20\d\d$|^cents$", re.I)


def _columns(rows, i: int = 0) -> list[float] | None:
    """Right edges of the value columns, from the nearest header row ABOVE row i ("$'000  $'000" or
    "2026  2025"), so each table on a page (or each half of a two-page spread) uses its own columns.
    Numbers in these statements are right-aligned under their header."""
    key = (id(rows), i)
    if key in _COL_CACHE:
        return _COL_CACHE[key]
    cols = None
    for j in range(min(i, len(rows) - 1), -1, -1):
        heads = [w for w in rows[j] if COL_HEAD.match(_norm(w[4]).strip())]
        if len(heads) >= 2:
            cols = sorted(w[2] for w in heads)[-2:]
            break
    if cols is None:  # no header above this row: fall back to the first header on the page
        for row in rows[:40]:
            heads = [w for w in row if COL_HEAD.match(_norm(w[4]).strip())]
            if len(heads) >= 2:
                cols = sorted(w[2] for w in heads)[-2:]
                break
    _COL_CACHE[key] = cols
    return cols


def _by_columns(row, cols) -> list[float] | None:
    """Pick the number sitting under each value column; anything else (note refs like '4, 26') is ignored."""
    nums = [w for w in row if NUM.match(_norm(w[4]).strip(",")) and not re.search(r"[a-z]", w[4], re.I)]
    out = []
    for cx in cols[:2]:
        best = min(nums, key=lambda w: abs(w[2] - cx), default=None)
        if best is None or abs(best[2] - cx) > 28:
            break
        v = _to_number(_norm(best[4]))
        if v is None:
            break
        out.append(v)
    return out or None


def _values(rows, i, use_cols: bool = True) -> list[float]:
    """Numbers to the right of the label on row i (or a continuation row), note column removed.
    Share-count notes use 'Number' columns rather than $'000, so they skip the column matching."""
    cols = _columns(rows, i) if use_cols else None
    if cols:
        own_nums = [w for w in rows[i] if NUM.match(_norm(w[4]).strip(",")) and not re.search(r"[a-z]", w[4], re.I)]
        for j in range(i, min(i + 3, len(rows))):
            if j > i and (own_nums or len([w for w in rows[j] if re.search(r"[a-z]", w[4], re.I)]) > 6):
                break  # a row with its own numbers never borrows the next row's
            got = _by_columns(rows[j], cols)
            if got:
                return got
        if not own_nums:
            return []
        # numbers don't line up with the column headers: read them right of the label instead
    row = rows[i]
    alpha = [w for w in row if re.search(r"[a-z]", w[4], re.I)]
    x0 = alpha[-1][2] + 1 if alpha else 0.0
    toks = [t for t in _merge_tokens([_norm(w[4]) for w in row if w[0] >= x0]) if NUM.match(t)]
    j = i + 1
    while not toks and j < min(i + 3, len(rows)):
        nxt = rows[j]
        a2 = [w for w in nxt if re.search(r"[a-z]", w[4], re.I)]
        if len(a2) > 6:
            break
        x1 = a2[-1][2] + 1 if a2 else 0.0
        toks = [t for t in _merge_tokens([_norm(w[4]) for w in nxt if w[0] >= x1]) if NUM.match(t)]
        j += 1
    if len(toks) >= 3:
        first = toks[0].strip("()")
        if first.replace(".", "").isdigit() and len(first) <= 4 and "," not in toks[0] and float(first) < 60:
            toks = toks[1:]  # note reference column
    elif len(toks) == 2:
        a = toks[0].strip("()")
        if a.isdigit() and len(a) <= 2 and ("," in toks[1] or len(toks[1].strip("()")) >= 4):
            toks = toks[1:]
    out = []
    for t in toks[:2]:
        v = _to_number(t)
        if v is not None:
            out.append(v)
    return out


def _find(rows, pat, excl=EXCL, start_only=True, all_matches=False, use_cols=True):
    hits = []
    for i, row in enumerate(rows):
        lab = _label_text(row).strip()
        lab_clean = re.sub(r"^[\d.()a-z]{1,4}\s+(?=[A-Za-z])", "", lab) if re.match(r"^\(?[\divx]{1,4}[.)]?\s", lab) else lab
        m = pat.search(lab_clean) if not start_only else pat.match(lab_clean)
        if not m or (excl and excl.search(lab_clean)):
            continue
        vals = _values(rows, i, use_cols)
        if not vals:
            continue
        hits.append((lab_clean, vals))
        if not all_matches:
            break
    return hits


REV_SKIP = re.compile(r"^other\s+(income|gains?|revenue)|gain\s+on|gains?\b|fair\s+value|interest|finance\s+income|"
                      r"share\s+of|associate|joint\s+venture|dividend|grant|government|foreign\s+exchange|"
                      r"insurance\s+recover|reversal|net\s+investment", re.I)
REV_STOP = re.compile(r"^(expenses?|less\b|cost|employee|depreciation|amortisation|finance\s+costs|profit|loss|gross\s+profit|"
                      r"raw\s+materials|changes\s+in|purchases|administration|marketing|occupancy|other\s+expenses|total\s+expenses)", re.I)


def _revenue_block(rows):
    """Revenue lines at the top of the income statement, summed (e.g. LGI: contracts + environmental
    certificates). Other income, gains, interest and associates are left out. Returns (values, n_lines,
    total_row_values_or_None)."""
    start = None
    for i, row in enumerate(rows):
        lab = _label_text(row).strip()
        has_num = any(NUM.match(_norm(w[4])) and not re.search(r"[a-z]", w[4], re.I) for w in row)
        if re.match(r"^(revenue|income|revenue\s+and\s+other\s+income|income\s+from\s+operations|revenues?\s+from\s+operations)$", lab, re.I) and not has_num:
            start = i + 1
            break
        if (L["revenue"].match(lab) or L["revenue_total"].match(lab)) and has_num and not EXCL.search(lab):
            start = i
            break
    if start is None:
        return None
    tot, n = [0.0, 0.0], 0
    for i in range(start, min(start + 14, len(rows))):
        lab = _label_text(rows[i]).strip()
        if not lab:
            continue
        if L["revenue_total"].match(lab):
            v = _values(rows, i)
            if v:
                return [abs(x) for x in v], n, v
        if REV_STOP.match(lab):
            break
        has_num = any(NUM.match(_norm(w[4])) and not re.search(r"[a-z]", w[4], re.I) for w in rows[i])
        if not has_num:
            continue
        vals = _values(rows, i)
        if not vals:
            continue
        if vals[0] < 0:
            break
        if REV_SKIP.search(lab) or re.match(r"^total", lab, re.I):
            continue
        for k in range(min(2, len(vals))):
            tot[k] += vals[k]
        n += 1
    return (tot, n, None) if n else None


END_ROW = re.compile(r"(at|as\s+at|on\s+issue\s+at)\s+(the\s+)?(reporting\s+date|end\s+of|30\s+june|31\s+december|period[- ]end|year[- ]end|balance\s+date)|"
                     r"closing\s+balance|balance\s+at\s+(the\s+)?end|^total\s+(issued|ordinary|shares)", re.I)


def _issued_shares(rows) -> list[float] | None:
    """Ordinary shares on issue at period end, read from the 'No. of shares' column of the capital note."""
    cols, thousands = [], False
    for i, row in enumerate(rows):
        for j, w in enumerate(row):
            wl = _norm(w[4]).lower().strip(".:")
            nxt = _norm(row[j + 1][4]).lower() if j + 1 < len(row) else ""
            if wl in ("no", "number") and nxt.startswith("of"):
                # centre of the whole "No. of shares" phrase
                end_w = next((x for x in row[j + 1:j + 4] if _norm(x[4]).lower().startswith("share")), row[j + 1])
                cx = (w[0] + end_w[2]) / 2
                cols.append(cx)
                # a "'000" under or beside this header means thousands
                near = [x for r2 in rows[i:i + 3] for x in r2 if abs((x[0] + x[2]) / 2 - cx) < 45 and "000" in x[4] and not re.search(r"\d,\d", x[4])]
                thousands = thousands or bool(near)
        if cols:
            break
    for i, row in enumerate(rows):
        lab = _label_text(row).strip()
        if not END_ROW.search(lab) or re.search(r"option|right|performance|\$", lab, re.I):
            continue
        nums = [w for w in row if NUM.match(_norm(w[4])) and not re.search(r"[a-z]", w[4], re.I)]
        vals = [(w, _to_number(_norm(w[4]))) for w in nums]
        vals = [(w, v) for w, v in vals if v is not None]
        if not vals:
            continue
        if cols:
            picks = []
            for cx in cols[:2]:
                w, v = min(vals, key=lambda wv: abs((wv[0][0] + wv[0][2]) / 2 - cx))
                if abs((w[0] + w[2]) / 2 - cx) < 60:
                    picks.append(abs(v) * (1000 if thousands else 1))
            if picks and picks[0] >= 1e5:
                return picks + [None] * (2 - len(picks))
        elif len(vals) in (1, 2) and re.search(r"shares", lab, re.I) and abs(vals[0][1]) >= 1e5:
            return [abs(v) for _, v in vals] + [None] * (2 - len(vals))
    # tables headed "Number of shares" (possibly over several lines) vs "$'000": read the closing balance
    # only from a number-of-shares section
    mode, thousands = None, False
    for i, row in enumerate(rows):
        txt = _norm(" ".join(w[4] for w in row)).lower()
        toks = [_norm(w[4]).lower().strip() for w in row]
        if "number" in toks or "no." in toks or "of shares" in txt or re.search(r"\bno\.\s*of\b", txt):
            mode, thousands = "num", thousands or bool(re.search(r"'000|\(000\)|thousands", txt))
            continue
        if any(COL_HEAD.match(t) for t in toks) and not re.search(r"number|shares", txt):
            mode, thousands = "$", False
            continue
        lab = _label_text(row).strip()
        if mode == "num" and END_ROW.search(lab) and not re.search(r"option|right|performance", lab, re.I):
            vals = _values(rows, i, use_cols=False)
            if vals and abs(vals[0]) >= 1e4:
                k = 1000 if thousands and abs(vals[0]) < 1e7 else 1
                return [abs(v) * k for v in vals] + [None] * (2 - len(vals))
    # older style: a single "Ordinary shares - fully paid  125,500,000  124,000,000" line
    for lab, vals in _find(rows, L["issued"], excl=re.compile(r"\$|value|amount|beginning|opening|issue\s+of|issued\s+during", re.I), all_matches=True, use_cols=False):
        if vals and abs(vals[0]) >= 1e5 and float(vals[0]).is_integer() and len(vals) <= 2:
            return [abs(x) for x in vals] + [None] * (2 - len(vals))
    return None


def _canon(lab: str) -> str:
    """Profit-line label with brackets and profit/loss pairs normalised:
    '(Loss)/profit before income tax' → 'profit before income tax', '(Loss) for the year' → 'loss for the year'."""
    t = _norm(lab).lower()
    t = re.sub(r"\(?\s*(profit|loss)\s*\)?\s*/\s*\(?\s*(profit|loss)\s*\)?", "profit", t)
    t = t.replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", t).strip()


PBT_C = re.compile(r"\b(profit|loss|earnings|result)\b(?:\s+[a-z&,'-]+){0,5}?\s+before\s+(income\s+)?tax")
NPAT_C = re.compile(r"^(net\s+)?(profit|loss)\b(?!.*\bbefore\b)(?!.*per\s+share)(?!.*comprehensive)")
SUMMARY_PAGE = re.compile(r"appendix\s+4[de]|results\s+for\s+announcement|up\s*/\s*\(?down\)?|change\s*%|%\s*change|"
                          r"statutory\s+ebitda|underlying\s+ebitda", re.I)


def _page_score(text: str, kind: str) -> int:
    t = text.lower()
    s = 0
    if kind == "pl":
        s += 4 if PL_PAGE.search(text) else 0
        s += 3 if re.search(r"before\s+(income\s+)?tax", t) else 0
        s += 1 if "revenue" in t else 0
        s += 1 if re.search(r"per\s+share", t) else 0
    else:
        s += 4 if BS_PAGE.search(text) else 0
        s += 2 if "total assets" in t else 0
        s += 2 if "total liabilities" in t else 0
        s += 1 if "cash" in t else 0
    if NOT_STATEMENT.search(t[:600]):
        s -= 4
    if SUMMARY_PAGE.search(t):
        s -= 5  # an Appendix 4E/4D results summary, not the statement itself
    return s


_ROWS_CACHE: dict = {}


def _page_rows(doc, p):
    key = (id(doc), p)
    if key not in _ROWS_CACHE:
        _ROWS_CACHE[key] = _rows(doc[p])
    return _ROWS_CACHE[key]


TAX_LINE = re.compile(r"^income\s+tax\s+(expense|benefit)|^tax\s+(expense|benefit)|^income\s+tax\s*$", re.I)
EPS_LINE = re.compile(r"(earnings|loss|profit)\s+per\s+share|\beps\b", re.I)
FIN_LINE = re.compile(r"^(finance\s+(costs?|expenses?)|interest\s+(expense|paid|costs?)|borrowing\s+costs|net\s+finance)", re.I)


def _line_score(rows, kind: str) -> int:
    """How much this page looks like the statement itself: count the statement's own lines that have
    numbers beside them. Running headers, navigation bars and notes that merely mention the statement
    names score nothing, so they can't win."""
    have = set()
    expense_lines = 0
    for i, row in enumerate(rows):
        lab = _label_text(row).strip()
        if not lab or len(lab) > 90:
            continue
        has_num = any(NUM.match(_norm(w[4]).strip(",")) and not re.search(r"[a-z]", w[4], re.I) for w in row)
        if not has_num:
            continue
        c = _canon(lab)
        if kind == "pl":
            if PBT_C.search(c):
                have.add("pbt")
            elif TAX_LINE.match(c):
                have.add("tax")
            elif NPAT_C.match(c) or re.match(r"^profit\s+after\s+income\s+tax", c):
                have.add("npat")
            elif L["revenue"].match(lab) or L["revenue_total"].match(lab):
                have.add("rev")
            elif EPS_LINE.search(c):
                have.add("eps")
            elif FIN_LINE.match(c):
                have.add("fin")
            elif re.search(r"expenses?$|^cost\s+of|^depreciation|^employee", c):
                expense_lines += 1
        else:
            if L["cash"].match(lab):
                have.add("cash")
            elif re.match(r"^total\s+assets", c):
                have.add("ta")
            elif re.match(r"^total\s+liabilities", c):
                have.add("tl")
            elif re.match(r"^(total\s+equity|net\s+assets|total\s+shareholders)", c):
                have.add("te")
            elif re.match(r"^(trade\s+and\s+other\s+(payables|receivables)|borrowings|property,?\s+plant)", c):
                have.add("other")
    w = {"pbt": 3, "tax": 2, "npat": 2, "rev": 1, "eps": 1, "fin": 1, "cash": 2, "ta": 2, "tl": 2, "te": 2, "other": 1}
    score = sum(w[k] for k in have) + (1 if expense_lines >= 3 else 0)
    return score


def _best_pages(doc, kind: str, n: int = 2) -> list[int]:
    """The statement page(s): ranked by the statement lines actually on the page (see _line_score), with the
    old heading-based score only as a tie-breaker. Adds the page after the best one, since statements often
    run over a page break."""
    need = 6 if kind == "pl" else 6
    scored = []
    for p in range(doc.page_count):
        txt = page_text(doc[p])
        if kind == "pl" and not re.search(r"before\s+(income\s+)?tax", txt, re.I):
            continue
        if kind == "bs" and not re.search(r"total\s+assets|net\s+assets", txt, re.I):
            continue
        ls = _line_score(_page_rows(doc, p), kind)
        both = ls
        if 3 <= ls < need and p + 1 < doc.page_count:  # statement split over a page break
            both = _line_score(_page_rows(doc, p) + _page_rows(doc, p + 1), kind)
        if both >= need:
            scored.append((both, ls, _page_score(txt, kind), p))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]))
    pages = [x[3] for x in scored[:n]]
    if pages and pages[0] + 1 < doc.page_count and pages[0] + 1 not in pages:
        pages.append(pages[0] + 1)
    return pages


def _structural(text: str, kind: str) -> bool:
    t = text.lower()
    if kind == "pl":
        return bool(re.search(r"before\s+(income\s+)?tax", t)) and bool(re.search(r"income\s+tax", t)) and \
            bool(re.search(r"revenue|sales|income", t))
    return "total assets" in t and "total liabilities" in t


def _mult(doc, p: int) -> float:
    m = _unit_multiplier(page_text(doc[p]))
    if m == 1.0 and p > 0:
        m2 = _unit_multiplier(page_text(doc[p - 1])[-1500:])
        m = m2 if m2 != 1.0 else m
    return m


# ---------------------------------------------------------------- main entry
def parse_report(data: bytes, kind: str, released: str, title: str | None = None) -> dict | None:
    """Return {'period_end', 'months', 'currency', 'metrics': {name: (cur, prior)}, 'pages': {...}} or None."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return _parse(doc, kind, released, title)
    finally:
        _ROWS_CACHE.clear()
        _COL_CACHE.clear()
        doc.close()


def _parse(doc, kind: str, released: str, title: str | None = None) -> dict | None:
    _ROWS_CACHE.clear()  # page rows are cached per report only
    _COL_CACHE.clear()
    months = 6 if kind == "h" else 12
    metrics: dict[str, list[float]] = {}
    pages: dict[str, int] = {}

    def put(name, vals, mult, page, absolute=False):
        if name in metrics or not vals:
            return
        v = [x * mult for x in vals]
        if absolute:
            v = [abs(x) for x in v]
        metrics[name] = v + [None] * (2 - len(v))
        pages[name] = page + 1

    # -------- income statement
    # the income statement page, plus the next page only if the statement continues onto it
    # (profit before tax found but not the profit after tax that ends it). Other pages that merely look
    # similar (segment notes, tax notes) are never read.
    pl_pages = _best_pages(doc, "pl")[:1]
    if pl_pages:
        r0 = _page_rows(doc, pl_pages[0])
        labs = [_canon(_label_text(r)) for r in r0]
        has_pbt = any(PBT_C.search(c) for c in labs)
        has_npat = any(NPAT_C.match(c) for c in labs)
        if not (has_pbt and has_npat) and pl_pages[0] + 1 < doc.page_count:
            pl_pages.append(pl_pages[0] + 1)
    ctx_text = ""
    pbt_seen = False  # kept across pages: profit before tax can sit at the bottom of one page, net profit on the next
    for p in pl_pages:
        rows = _page_rows(doc, p)
        mult = _mult(doc, p)
        ctx_text = ctx_text or page_text(doc[p])
        # revenue: a 'Total revenue' line if there is one, otherwise every revenue line at the top of
        # the statement added together (other income, gains and interest left out)
        tot = _find(rows, L["revenue_total"])
        blk = _revenue_block(rows)
        if tot:
            put("revenue", tot[0][1], mult, p, absolute=True)
        elif blk:
            put("revenue", blk[0], mult, p, absolute=True)
        else:
            tot_oi = _find(rows, re.compile(r"^total\s+(revenue|income)(\s+and\s+other\s+income)?\b", re.I),
                           excl=re.compile(r"comprehensive|tax", re.I))
            if tot_oi:
                put("revenue", tot_oi[0][1], mult, p, absolute=True)
        h = _find(rows, re.compile(r"^gross\s+(profit|margin)\b", re.I), excl=re.compile(r"%|percent", re.I))
        if h:
            put("gross", h[0][1], mult, p)
        for i, row in enumerate(rows):
            c = _canon(_label_text(row))
            if PBT_C.search(c) and not re.search(r"per\s+share|comprehensive|discontinued", c):
                v = _values(rows, i)
                if v:
                    put("pbt", v, mult, p)
                    break
        h = _find(rows, L["net_fin"])
        if h:
            # stored as a cost (positive = expense). "Net finance costs (1,234)" → 1,234.
            # "Net finance income/(costs)": positive = income, bracketed = cost.
            v = h[0][1]
            income_label = re.search(r"income", h[0][0], re.I)
            put("net_fin_cost", [-x if income_label else abs(x) for x in v], mult, p)
        h = _find(rows, L["fin_cost"])
        if h:
            put("fin_cost", h[0][1], mult, p, absolute=True)
        h = _find(rows, L["fin_income"])
        if h:
            put("fin_income", h[0][1], mult, p, absolute=True)
        h = _find(rows, L["da_total"])
        if h:
            put("da", h[0][1], mult, p, absolute=True)
        else:
            parts = _find(rows, L["da_part"], all_matches=True)
            if parts:
                tot = [sum(abs(v[k]) for _, v in parts if len(v) > k) for k in range(2)]
                put("da", tot, mult, p, absolute=True)
        # NPAT: first profit-after-tax style line *below* PBT
        for i, row in enumerate(rows):
            lab = _label_text(row).strip()
            c = _canon(lab)
            if PBT_C.search(c):
                pbt_seen = True
                continue
            if pbt_seen and "npat" not in metrics and NPAT_C.match(c) and \
                    not re.search(r"discontinued\s+operations\s*$", c):
                v = _values(rows, i)
                if v:
                    put("npat", v, mult, p)
                    break
        h = _find(rows, L["eps"], start_only=False, excl=re.compile(r"^(?!.*basic).*diluted", re.I))
        if h:
            metrics.setdefault("eps_raw", h[0][1] + [None] * (2 - len(h[0][1])))
            pages.setdefault("eps_raw", p + 1)

    # D&A elsewhere: cash flow reconciliation or expenses note
    if "da" not in metrics:
        for p in range(doc.page_count):
            t = page_text(doc[p])
            if not re.search(r"depreciation", t, re.I):
                continue
            recon = re.search(r"reconciliation\s+of", t, re.I) and re.search(r"operating\s+activities", t, re.I)
            if not recon and not re.search(r"\bexpenses\b", t, re.I):
                continue
            rows = _page_rows(doc, p)
            mult = _mult(doc, p)
            h = _find(rows, L["da_total"])
            if h:
                put("da", h[0][1], mult, p, absolute=True)
                break
            if recon:  # separate depreciation / amortisation lines only on the cash-flow reconciliation
                parts = _find(rows, L["da_part"], all_matches=True)
                if parts and len(parts) <= 4:
                    tot = [sum(abs(v[k]) for _, v in parts if len(v) > k) for k in range(2)]
                    put("da", tot, mult, p, absolute=True)
                    break

    # -------- balance sheet
    bs_pages = _best_pages(doc, "bs")[:1]
    if bs_pages and not any(re.match(r"^(total\s+equity|net\s+assets)", _canon(_label_text(r)))
                            for r in _page_rows(doc, bs_pages[0])) and bs_pages[0] + 1 < doc.page_count:
        bs_pages.append(bs_pages[0] + 1)  # balance sheet continues onto the next page
    for p in bs_pages:
        rows = _page_rows(doc, p)
        mult = _mult(doc, p)
        h = _find(rows, L["cash"])
        if h:
            put("cash", h[0][1], mult, p, absolute=True)
        b = _find(rows, L["borrow"], excl=re.compile(r"lease|cost|receivable|deferred", re.I), all_matches=True)
        if b:
            tot = [sum(abs(v[k]) for _, v in b if len(v) > k) for k in range(2)]
            put("debt", tot, mult, p, absolute=True)
        elif "cash" in metrics:
            metrics.setdefault("debt", [0.0, 0.0])
        ls = _find(rows, L["lease"], excl=re.compile(r"receivable|asset", re.I), all_matches=True)
        if ls:
            tot = [sum(abs(v[k]) for _, v in ls if len(v) > k) for k in range(2)]
            put("leases", tot, mult, p, absolute=True)
        elif "cash" in metrics:
            metrics.setdefault("leases", [0.0, 0.0])
        h = _find(rows, L["equity"])
        if h:
            put("equity", h[0][1], mult, p)
        if "cash" in metrics and "equity" in metrics:
            break

    # -------- shares (weighted average from EPS note; issued from capital note)
    wa = None
    for p in range(doc.page_count):
        t = page_text(doc[p])
        if not re.search(r"weighted\s+average\s+number", t, re.I):
            continue
        rows = _rows(doc[p])
        for i, row in enumerate(rows):
            lab = _label_text(row)
            if not L["wa_shares"].search(lab) or re.search(r"diluted(?!.*basic)", lab, re.I):
                continue
            vals = _values(rows, i, use_cols=False)
            if not vals:
                continue
            # units only from this line and the few header rows right above it (a page-wide "$'000"
            # usually belongs to the earnings figures, not the share count)
            near = _norm(" ".join(" ".join(w[4] for w in r) for r in rows[max(0, i - 3):i + 1])).lower()
            if re.search(r"(\bno\.|\bnumber\b|\bshares\b)[^\n]{0,25}('000|\(000\)|000s|thousands)|('000|thousands)[^\n]{0,15}\bshares\b|"
                         r"weighted[^\n]*('000|\(000\)|thousands)", near):
                vals = [x * 1000 for x in vals]
            elif re.search(r"(\bno\.|\bnumber\b|\bshares\b)[^\n]{0,25}(millions|\(m\))", near):
                vals = [x * 1e6 for x in vals]
            wa = vals
            pages["wa_shares"] = p + 1
            break
        if wa:
            break
    issued = None
    for p in range(doc.page_count):
        t = page_text(doc[p])
        if not re.search(r"issued\s+capital|contributed\s+equity|share\s+capital", t, re.I):
            continue
        if not re.search(r"number|no\.|shares", t, re.I):
            continue
        issued = _issued_shares(_rows(doc[p]))
        if issued:
            pages["issued"] = p + 1
            break

    # sanity: reconcile shares with NPAT / EPS
    npat = (metrics.get("npat") or [None])[0]
    eps = (metrics.get("eps_raw") or [None])[0]
    implied = None
    if npat and eps and abs(eps) >= 0.05:
        implied = abs(npat) / (abs(eps) / 100)  # EPS normally in cents
    if wa and implied:
        best = min([1, 1e-3, 1e3], key=lambda k: abs(wa[0] * k / implied - 1))
        wa = [x * best for x in wa]
        if abs(wa[0] / implied - 1) > 0.35:  # maybe EPS is in dollars
            if abs(wa[0] / (implied / 100) - 1) < 0.35:
                implied = implied / 100
    if wa and wa[0] and wa[0] > 1e4:
        metrics["shares_wa"] = wa + [None] * (2 - len(wa))
    elif implied and implied > 1e4:
        metrics["shares_wa"] = [implied, None]
        pages["shares_wa"] = pages.get("eps_raw")
    if issued and metrics.get("shares_wa") and metrics["shares_wa"][0]:
        r = issued[0] / metrics["shares_wa"][0]
        if 0.6 <= r <= 2.5:
            metrics["shares"] = issued + [None] * (2 - len(issued))
    elif issued and issued[0] >= 1e5:
        metrics["shares"] = issued + [None] * (2 - len(issued))  # nothing to check against; outlier filter still applies
    if "shares" not in metrics and metrics.get("shares_wa"):
        metrics["shares"] = list(metrics["shares_wa"])
        pages["shares"] = pages.get("wa_shares") or pages.get("eps_raw")
    metrics.pop("eps_raw", None)

    # -------- sanity checks: a figure that breaks basic accounting relationships is a misread, so drop it
    rejected = []

    def col0(name, k):
        v = metrics.get(name)
        return v[k] if v and len(v) > k and v[k] is not None else None

    for k in range(2):
        rev, pbt, npat, da = col0("revenue", k), col0("pbt", k), col0("npat", k), col0("da", k)
        small = 0.02 * abs(rev or 0) + 1e5
        if pbt is not None and npat is not None:
            lo, hi = (pbt - 0.6 * abs(pbt) - small, pbt + 0.5 * abs(pbt) + small)
            if not (lo <= npat <= hi):  # tax can't explain the gap between profit before and after tax
                metrics["npat"][k] = None
                rejected.append("npat")
        if da is not None and rev and rev > 1e6 and da > 1.5 * rev:
            metrics["da"][k] = None
            rejected.append("da")
        g = col0("gross", k)
        if g is not None and rev and g > rev * 1.001:
            metrics["gross"][k] = None
            rejected.append("gross")
    for name in list(metrics):
        if all(x is None for x in metrics[name][:2]):
            metrics.pop(name)

    # -------- derived EBIT / EBITDA (current and prior)
    def col(name, k):
        v = metrics.get(name)
        return v[k] if v and len(v) > k else None

    for k in range(2):
        pbt = col("pbt", k)
        if pbt is None:
            continue
        if col("net_fin_cost", k) is not None:
            ebit = pbt + col("net_fin_cost", k)
        else:
            ebit = pbt + (col("fin_cost", k) or 0) - (col("fin_income", k) or 0)
        metrics.setdefault("ebit", [None, None])[k] = ebit
        if col("da", k) is not None:
            metrics.setdefault("ebitda", [None, None])[k] = ebit + col("da", k)
    for tmp in ("fin_cost", "fin_income", "net_fin_cost", "pbt"):
        metrics.pop(tmp, None)

    if not metrics:
        return None

    # -------- period end
    kw = (r"half[\s-]*year\s+ended|six\s+months\s+ended|period\s+ended|half[\s-]*year\s+ending"
          if kind == "h" else r"year\s+ended|financial\s+year\s+ended|period\s+ended|year\s+ending")
    text0 = (page_text(doc[pl_pages[0]]) if pl_pages else "") + "\n" + \
        "\n".join(page_text(doc[q]) for q in range(min(4, doc.page_count)))
    end = resolve_period_end(_period_end_from_text(text0, kw), kind, released, title)
    missing = [m for m in ("revenue", "npat", "ebit", "cash", "shares") if m not in metrics]
    out = {"missing": missing, "rejected": sorted(set(rejected)), "period_end": _snap_to_month_end(end).isoformat(), "months": months,
           "currency": _currency(ctx_text[:3000]),
           "metrics": {k: [None if x is None else float(x) for x in v[:2]] for k, v in metrics.items()},
           "pages": pages}
    return out


# ---------------------------------------------------------------- 4C / 5B: end-of-quarter cash
CASH_4C = re.compile(r"cash\s+and\s+cash\s+equivalents\s+at\s+end\s+of\s+(the\s+)?(period|quarter)", re.I)


def quarterly_cash(data: bytes) -> float | None:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for p in range(doc.page_count - 1, -1, -1):
            t = page_text(doc[p])
            if not CASH_4C.search(_norm(t).replace("\n", " ")):
                continue
            rows = _rows(doc[p])
            for i, row in enumerate(rows):
                lab = _label_text(row)
                if CASH_4C.search(lab):
                    toks = [x for x in _merge_tokens([_norm(w[4]) for w in row]) if NUM.match(x)]
                    toks = [x for x in toks if not re.match(r"^\d\.\d$", x)]  # drop line ref "4.6"
                    if toks:
                        v = _to_number(toks[0])
                        if v is not None:
                            mult = _unit_multiplier(t) if _unit_multiplier(t) != 1.0 else 1000.0
                            return abs(v) * mult
        return None
    finally:
        doc.close()
