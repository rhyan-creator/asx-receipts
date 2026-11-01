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

from .parse import (NUM, _merge_tokens, _norm, _period_end_from_text, _rows, _snap_to_month_end, _to_number,
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
    "borrow": re.compile(r"^(interest[- ]bearing\s+)?(loans\s+and\s+)?borrowings|^interest[- ]bearing\s+(loans|liabilities)|"
                         r"^bank\s+(loans?|overdrafts?|facilit)|^loans\s+(payable|from)|^convertible\s+notes?|^notes\s+payable|"
                         r"^debt\b|^financial\s+liabilities\s*-?\s*borrowings", re.I),
    "lease": re.compile(r"^lease\s+liabilit|^right[- ]of[- ]use\s+(lease\s+)?liabilit|^leases?\b(?!.*receivable)", re.I),
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


def _values(rows, i) -> list[float]:
    """Numbers to the right of the label on row i (or a continuation row), note column removed."""
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


def _find(rows, pat, excl=EXCL, start_only=True, all_matches=False):
    hits = []
    for i, row in enumerate(rows):
        lab = _label_text(row).strip()
        lab_clean = re.sub(r"^[\d.()a-z]{1,4}\s+(?=[A-Za-z])", "", lab) if re.match(r"^\(?[\divx]{1,4}[.)]?\s", lab) else lab
        m = pat.search(lab_clean) if not start_only else pat.match(lab_clean)
        if not m or (excl and excl.search(lab_clean)):
            continue
        vals = _values(rows, i)
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
    # older style: a single "Ordinary shares - fully paid  125,500,000  124,000,000" line
    for lab, vals in _find(rows, L["issued"], excl=re.compile(r"\$|value|amount|beginning|opening|issue\s+of|issued\s+during", re.I), all_matches=True):
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


PBT_C = re.compile(r"\b(net\s+)?(profit|loss|earnings)\s+before\s+(income\s+)?tax")
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


def _best_pages(doc, kind: str, n: int = 2) -> list[int]:
    """The statement page(s), plus the page after the best one (statements often run over a page break).
    Falls back to a looser score when the heading isn't extractable text (e.g. drawn as an image)."""
    scored = []
    for p in range(doc.page_count):
        txt = doc[p].get_text()
        scored.append((_page_score(txt, kind), p))
    good = [x for x in scored if x[0] >= 6] or [x for x in scored if x[0] >= 4 and _structural(doc[x[1]].get_text(), kind)]
    good.sort(key=lambda x: (-x[0], x[1]))
    pages = [p for _, p in good[:n]]
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
    m = _unit_multiplier(doc[p].get_text())
    if m == 1.0 and p > 0:
        m2 = _unit_multiplier(doc[p - 1].get_text()[-1500:])
        m = m2 if m2 != 1.0 else m
    return m


# ---------------------------------------------------------------- main entry
def parse_report(data: bytes, kind: str, released: str, title: str | None = None) -> dict | None:
    """Return {'period_end', 'months', 'currency', 'metrics': {name: (cur, prior)}, 'pages': {...}} or None."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return _parse(doc, kind, released, title)
    finally:
        doc.close()


def _parse(doc, kind: str, released: str, title: str | None = None) -> dict | None:
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
    pl_pages = _best_pages(doc, "pl")
    ctx_text = ""
    pbt_seen = False  # kept across pages: profit before tax can sit at the bottom of one page, net profit on the next
    for p in pl_pages:
        rows = _rows(doc[p])
        mult = _mult(doc, p)
        ctx_text = ctx_text or doc[p].get_text()
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
        h = _find(rows, L["eps"], start_only=False, excl=re.compile(r"diluted\s+(earnings|eps)(?!.*basic)", re.I))
        if h:
            metrics.setdefault("eps_raw", h[0][1] + [None] * (2 - len(h[0][1])))
            pages.setdefault("eps_raw", p + 1)

    # D&A elsewhere: cash flow reconciliation or expenses note
    if "da" not in metrics:
        for p in range(doc.page_count):
            t = doc[p].get_text()
            if not re.search(r"depreciation", t, re.I):
                continue
            if not re.search(r"reconciliation|operating\s+activities|expenses|profit\s+before", t, re.I):
                continue
            rows = _rows(doc[p])
            mult = _mult(doc, p)
            h = _find(rows, L["da_total"])
            if h:
                put("da", h[0][1], mult, p, absolute=True)
                break
            parts = _find(rows, L["da_part"], all_matches=True)
            if parts and len(parts) <= 4:
                tot = [sum(abs(v[k]) for _, v in parts if len(v) > k) for k in range(2)]
                put("da", tot, mult, p, absolute=True)
                break

    # -------- balance sheet
    for p in _best_pages(doc, "bs", n=3):
        rows = _rows(doc[p])
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
        if "cash" in metrics:
            break

    # -------- shares (weighted average from EPS note; issued from capital note)
    wa = None
    for p in range(doc.page_count):
        t = doc[p].get_text()
        if not re.search(r"weighted\s+average\s+number", t, re.I):
            continue
        rows = _rows(doc[p])
        for i, row in enumerate(rows):
            lab = _label_text(row)
            if not L["wa_shares"].search(lab) or re.search(r"diluted(?!.*basic)", lab, re.I):
                continue
            vals = _values(rows, i)
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
        t = doc[p].get_text()
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
    if "shares" not in metrics and metrics.get("shares_wa"):
        metrics["shares"] = list(metrics["shares_wa"])
        pages["shares"] = pages.get("wa_shares") or pages.get("eps_raw")
    metrics.pop("eps_raw", None)

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
    text0 = (doc[pl_pages[0]].get_text() if pl_pages else "") + "\n" + \
        "\n".join(doc[q].get_text() for q in range(min(4, doc.page_count)))
    end = resolve_period_end(_period_end_from_text(text0, kw), kind, released, title)
    missing = [m for m in ("revenue", "npat", "ebit", "cash", "shares") if m not in metrics]
    out = {"missing": missing, "period_end": _snap_to_month_end(end).isoformat(), "months": months,
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
            t = doc[p].get_text()
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
