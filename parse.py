"""Pulls the 'receipts from customers' figure out of an ASX filing PDF.

Works on three layouts:
  * Appendix 4C / 5B quarterly cash flow reports (line 1.1, current-quarter column)
  * Half-year (4D / interim) cash flow statements
  * Full-year (4E / annual report) cash flow statements
Rows are rebuilt from word coordinates, so table columns that the PDF stores out of
order still line up with their labels.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pymupdf

LABEL = re.compile(
    r"(cash\s+)?receipts?\s+(received\s+)?from\s+(customers?|clients?|operations|patients|tenants|members|"
    r"sales|trade\s+debtors|debtors|contracts?\s+with\s+customers|operating\s+activities)"
    r"|cash\s+receipts\s+in\s+the\s+course\s+of\s+operations|receipts\s+from\s+the\s+sale\s+of\s+goods"
    # insurers: premiums collected play the role of customer receipts
    r"|(gross\s+)?(insurance\s+)?premiums?\s+(received|receipts|collected)|receipts\s+from\s+policyholders",
    re.I,
)
# exclude rows that mention receipts but aren't customer receipts
LABEL_EXCLUDE = re.compile(r"government|grant|interest|dividend|insurance\s+recover|r\s*&\s*d|research|"
                           r"tax\s+(refund|incentive)|sale\s+of\s+(property|plant|investment|subsid|share)", re.I)
NUM = re.compile(r"^\(?-?\$?[\d,]+(\.\d+)?\)?$|^[-–—]$|^nil$", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\s*,?\s+(\d{4})", re.I)


# ---------------------------------------------------------------- helpers
def _norm(s: str) -> str:
    return (s.replace("’", "'").replace("‘", "'").replace("′", "'")
             .replace(" ", " ").replace("`", "'"))


def _to_number(tok: str) -> float | None:
    t = tok.strip().replace("$", "")
    if t in {"-", "–", "—"} or t.lower() == "nil":
        return 0.0
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = t.strip("()-")
    try:
        v = float(t.replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def _rotated_lines(page) -> list[tuple]:
    """Rotated text such as the ASX's 'For personal use only' margin stamp: (bbox, set of its words)."""
    out = []
    try:
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                dx, dy = ln.get("dir", (1, 0))
                if abs(dy) > 0.2:
                    text = " ".join(sp.get("text", "") for sp in ln.get("spans", []))
                    out.append((tuple(ln["bbox"]), {t.lower() for t in text.split()}))
    except Exception:  # noqa: BLE001
        pass
    return out


def _is_stamp_word(w, rot) -> bool:
    """A word belongs to rotated text only if it is one of that text's words AND it spans the full
    thickness of the rotated line (rotated words are as wide as the line is tall). Horizontal words
    that merely sit inside the stamp's box, e.g. Calix's 'Loss' at the left margin, are kept."""
    x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4].lower()
    for (bx0, by0, bx1, by1), words in rot:
        if txt not in words:
            continue
        if x0 >= bx0 - 2 and x1 <= bx1 + 2 and y0 >= by0 - 2 and y1 <= by1 + 2 and (x1 - x0) >= 0.7 * (bx1 - bx0):
            return True
    return False


# ---------------------------------------------------------------- broken font encodings
# Some PDFs (e.g. LGI's FY24 report) embed fonts whose text codes are shifted: every character is stored
# 29 places lower than it should be ("Receipts" is stored as "5HFHLSWV", a space as \x03). The page looks
# fine because the shapes are drawn correctly, but the text reads as gibberish. We detect and undo it.
SHIFT = 29
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
VOCAB = set("""revenue receipts profit loss net income tax expense expenses total assets liabilities cash equity borrowings
lease leases depreciation amortisation amortization finance costs cost before after for the year earnings per share shares basic
diluted notes note current non-current and of from customers payments suppliers employees operating activities investing financing
interest paid received other comprehensive statement financial position flows changes balance issued capital ordinary number
reserves retained gross sales goods sold benefits property plant equipment intangible trade receivables payables provisions
deferred contracts with certificates environmental attributable members owners period half-year consolidated""".split())


# ligatures and punctuation that the same broken fonts store outside the shifted range
LIGS = {"\u00bf": "fi", "\u00c0": "fl", "\u1088": "ffi", "\u1087": "ff", "\u00b6": "'", "\u00b1": "-",
        "\u00b3": '"', "\u00b4": '"', "\u00b5": "'"}


def _unshift(t: str) -> str:
    out = []
    for ch in t:
        o = ord(ch)
        if 3 <= o <= 97:
            out.append(chr(o + SHIFT))
        else:
            out.append(LIGS.get(ch, ch))
    return "".join(out)


def _span_is_shifted(t: str) -> bool:
    if not t.strip():
        return False
    if _CTRL.search(t):
        return True
    if any(not (3 <= ord(c) <= 97 or c in LIGS) for c in t if not c.isspace()):
        return False
    dec = re.findall(r"[a-z\-]+", _unshift(t).lower())
    raw = re.findall(r"[a-z\-]+", t.lower())
    return bool(dec) and sum(w in VOCAB for w in dec) > sum(w in VOCAB for w in raw)


def _has_shifted(page) -> bool:
    return bool(_CTRL.search(page.get_text()))


_TEXT_CACHE: dict = {}


def page_text(page) -> str:
    """page.get_text(), with shifted-font text decoded."""
    if not _has_shifted(page):
        return page.get_text()
    parts = []
    for b in page.get_text("dict").get("blocks", []):
        for ln in b.get("lines", []):
            parts.append("".join(_unshift(sp["text"]) if _span_is_shifted(sp["text"]) else sp["text"] for sp in ln["spans"]))
    return "\n".join(parts)


def _decoded_words(page) -> list[tuple]:
    """Words rebuilt character by character, decoding shifted spans."""
    words = []
    for bi, b in enumerate(page.get_text("rawdict").get("blocks", [])):
        for li, ln in enumerate(b.get("lines", [])):
            for sp in ln.get("spans", []):
                chars = sp.get("chars", [])
                raw = "".join(c["c"] for c in chars)
                shifted = _span_is_shifted(raw)
                cur, box = [], None
                for c in chars:
                    ch = _unshift(c["c"]) if shifted else c["c"]
                    if ch.isspace():
                        if cur:
                            words.append((*box, "".join(cur), bi, li, len(words)))
                        cur, box = [], None
                        continue
                    x0, y0, x1, y1 = c["bbox"]
                    box = [x0, y0, x1, y1] if box is None else [min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]
                    cur.append(ch)
                if cur:
                    words.append((*box, "".join(cur), bi, li, len(words)))
    return words


# ---------------------------------------------------------------- page layout
def _panels(words, page) -> list[list[tuple]]:
    """Split a two-page spread (one wide PDF page holding two report pages side by side, like LGI's FY23
    report) at its gutter, so rows from the left and right pages are never merged."""
    W, H = page.rect.width, page.rect.height
    if W < 1.15 * H or len(words) < 40:
        return [words]
    lo, hi = 0.35 * W, 0.65 * W
    covered = sorted((w[0], w[2]) for w in words if w[2] > lo and w[0] < hi)
    best, x = None, lo
    for a, b in covered + [(hi, hi)]:
        if a > x and (best is None or a - x > best[1] - best[0]):
            best = (x, a)
        x = max(x, b)
    cut = (best[0] + best[1]) / 2 if best and best[1] - best[0] >= 6 else W / 2
    left = [w for w in words if (w[0] + w[2]) / 2 < cut]
    right = [w for w in words if (w[0] + w[2]) / 2 >= cut]
    return [left, right] if len(left) >= 15 and len(right) >= 15 else [words]


def _group(words) -> list[list[tuple]]:
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows, cur, cur_y = [], [], None
    for w in words:
        y = (w[1] + w[3]) / 2
        if cur_y is None or abs(y - cur_y) <= 3.0:
            cur.append(w)
            cur_y = y if cur_y is None else (cur_y * (len(cur) - 1) + y) / len(cur)
        else:
            rows.append(sorted(cur, key=lambda w: w[0]))
            cur, cur_y = [w], y
    if cur:
        rows.append(sorted(cur, key=lambda w: w[0]))
    return rows


def _rows(page) -> list[list[tuple]]:
    """Words grouped into visual rows: margin stamps removed, shifted fonts decoded, and the two halves of
    a side-by-side spread read one after the other rather than merged."""
    words = _decoded_words(page) if _has_shifted(page) else page.get_text("words")
    rot = _rotated_lines(page)
    if rot:
        words = [w for w in words if not _is_stamp_word(w, rot)]
    rows = []
    for panel in _panels(words, page):
        rows.extend(_group(panel))
    return rows


def _merge_tokens(tokens: list[str]) -> list[str]:
    """Rejoin numbers split across words, e.g. '(1,234' ')' or '12' ',345'."""
    out = []
    for t in tokens:
        if out and (t == ")" or t.startswith(",")) and NUM.match(out[-1] + t):
            out[-1] += t
        else:
            out.append(t)
    return out


def _numbers_right_of(row, x_min: float) -> list[str]:
    toks = _merge_tokens([_norm(w[4]) for w in row if w[0] >= x_min - 0.5])
    return [t for t in toks if NUM.match(t)]


def _drop_note_ref(nums: list[str]) -> list[str]:
    """Annual reports often have a 'Note' column before the values."""
    if len(nums) >= 3:
        first = nums[0].strip("()")
        if first.isdigit() and len(first) <= 2 and "," not in nums[0]:
            return nums[1:]
    if len(nums) == 2:
        a, b = nums[0].strip("()"), nums[1].strip("()")
        # "Receipts from customers 5 12,345" (one value column + note)
        if a.isdigit() and len(a) <= 2 and ("," in b or len(b) >= 4):
            return nums[1:]
    return nums


def _find_receipts(page) -> tuple[float, str] | None:
    """Return (current-period value, raw label) for the receipts row on a page."""
    rows = _rows(page)
    for i, row in enumerate(rows):
        text = _norm(" ".join(w[4] for w in row))
        m = LABEL.search(text)
        if not m or LABEL_EXCLUDE.search(text[: m.end() + 40]):
            continue
        # x-position where the label ends: numbers must sit to the right of it
        last = m.group(0).split()[-1].lower()
        x_end, started = None, False
        for w in row:
            wl = _norm(w[4]).lower().strip("(),:")
            if not started and wl.startswith(("receipt", "cash")):
                started = True
            if started and wl == last:
                x_end = w[2]
                break
        if x_end is None:  # fall back: right edge of the last alphabetic word
            alpha = [w for w in row if re.search(r"[a-z]", w[4], re.I)]
            x_end = alpha[-1][2] if alpha else 0.0
        nums = _numbers_right_of(row, x_end + 1)
        # label wrapped over two lines: values may sit on the continuation row
        j = i + 1
        while not nums and j < min(i + 3, len(rows)):
            nxt = rows[j]
            words_txt = [w[4] for w in nxt if re.search(r"[a-z]", w[4], re.I)]
            if len(words_txt) > 6 or re.search(r"payment|paid|purchase|net\s+cash|interest\s+received|dividend",
                                               " ".join(words_txt), re.I):
                break
            alpha = [w for w in nxt if re.search(r"[a-z]", w[4], re.I)]
            x0 = alpha[-1][2] + 1 if alpha else 0.0
            nums = [t for t in _merge_tokens([_norm(w[4]) for w in nxt if w[0] >= x0]) if NUM.match(t)]
            j += 1
        nums = _drop_note_ref(nums)
        if not nums:
            continue
        v = _to_number(nums[0])
        if v is None:
            continue
        prior = _to_number(nums[1]) if len(nums) > 1 else None
        return abs(v), text, (abs(prior) if prior is not None else None)
    return None


def _unit_multiplier(text: str) -> float:
    t = _norm(text).lower()
    if re.search(r"\$\s?'?000|'000|\$000|000s|thousands|a\$000|\$a'000|\$us'000|us\$'000", t):
        return 1_000.0
    if re.search(r"\$\s?m\b|\$\s?million|in millions|\$m's|a\$m\b|us\$m\b", t):
        return 1_000_000.0
    return 1.0


def _currency(text: str) -> str:
    t = _norm(text)
    if re.search(r"US\$|\$US|USD|US\s+dollars", t):
        return "USD"
    if re.search(r"NZ\$|\$NZ|NZD", t):
        return "NZD"
    if re.search(r"C\$|CAD", t):
        return "CAD"
    if re.search(r"£|GBP", t):
        return "GBP"
    return "AUD"


def _parse_date(m) -> date:
    return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))


def _period_end_from_text(text: str, keywords: str) -> date | None:
    t = _norm(text)
    for m in re.finditer(rf"({keywords})\s*(\(\s*.?current quarter.?\s*\))?\s*(:)?", t, re.I):
        d = DATE_RE.search(t, m.end(), m.end() + 400)
        if d:
            try:
                return _parse_date(d)
            except ValueError:
                continue
    return None


MAX_LAG_DAYS = 2400  # ~6.5 years: old reports are re-lodged when a company lists


def title_period_end(title: str | None, kind: str) -> date | None:
    """A date in the headline, e.g. 'Annual report - 30 June 2018' or 'FY18 Annual Report'."""
    if not title:
        return None
    m = DATE_RE.search(_norm(title))
    if m:
        try:
            return _parse_date(m)
        except ValueError:
            pass
    m = re.search(r"\b(?:FY|HY|H1\s*FY|1H\s*FY)\s?'?(\d{2})\b", title, re.I)
    if m:
        y = 2000 + int(m.group(1))
        if kind == "h":
            return date(y - 1, 12, 31)
        return date(y, 6, 30)
    return None


def resolve_period_end(found: date | None, kind: str, released: str, title: str | None = None) -> date:
    """Trust the date printed in the document (even for old reports lodged at listing); then the
    headline's date; only then guess from the release date."""
    rel = datetime.strptime(released, "%Y-%m-%d").date()
    if found and 0 <= (rel - found).days <= MAX_LAG_DAYS:
        return found
    t = title_period_end(title, kind)
    if t and 0 <= (rel - t).days <= MAX_LAG_DAYS:
        return t
    return _quarter_end_before(released, lag_days=20 if kind != "q" else 10)


def _quarter_end_before(released: str, lag_days: int = 10) -> date:
    d = datetime.strptime(released, "%Y-%m-%d").date() - timedelta(days=lag_days)
    for back in range(0, 120):
        x = d - timedelta(days=back)
        if (x.month, x.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            return x
    return d


def _snap_to_month_end(d: date) -> date:
    """Most reporting periods end on a month end; tolerate odd dates like 52/53-week years."""
    nxt = (d.replace(day=28) + timedelta(days=4))
    return nxt - timedelta(days=nxt.day)


# ---------------------------------------------------------------- public API
def parse_pdf(data: bytes, kind: str, released: str, title: str | None = None) -> dict | None:
    """kind: 'q' quarterly, 'h' half-year, 'a' annual. Returns a data point or None."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if kind == "q":
            return _parse_quarterly(doc, released)
        return _parse_report(doc, kind, released, title)
    finally:
        doc.close()


def _parse_quarterly(doc, released: str) -> dict | None:
    # Appendix 4C/5B usually sits at the back of an activities report
    pages = list(range(doc.page_count))
    marker = re.compile(r"appendix\s+(4c|5b)|quarterly cash\s*flow report|consolidated statement of cash flows", re.I)
    ordered = [p for p in pages if marker.search(page_text(doc[p]))] + pages
    seen = set()
    for p in ordered:
        if p in seen:
            continue
        seen.add(p)
        page = doc[p]
        text = page_text(page)
        if not re.search(r"1\.1|receipts", text, re.I):
            continue
        hit = _find_receipts(page)
        if not hit:
            continue
        value, label, _ = hit  # 4C second column is year-to-date, not a comparative
        # header (quarter end, units) can be on this page or the first appendix page
        context = text + "\n" + "\n".join(page_text(doc[q]) for q in range(max(0, p - 2), p))
        end = _period_end_from_text(context, r"quarter\s+ended|quarter\s+ending|for\s+the\s+quarter\s+ended")
        if not end or abs((datetime.strptime(released, "%Y-%m-%d").date() - end).days) > 200:
            end = _quarter_end_before(released)
        mult = _unit_multiplier(context) if _unit_multiplier(context) != 1.0 else 1_000.0  # 4C default $A'000
        return {
            "period_end": _snap_to_month_end(end).isoformat(),
            "months": 3,
            "value": value * mult,
            "currency": _currency(context[:4000]),
            "label": label,
            "page": p + 1,
        }
    return None


def _parse_report(doc, kind: str, released: str, title: str | None = None) -> dict | None:
    scored = []
    for p in range(doc.page_count):
        text = page_text(doc[p])
        if not LABEL.search(_norm(text).replace("\n", " ")):
            continue
        score = 0
        low = text.lower()
        if re.search(r"statement\s+of\s+cash\s*flows?|cash\s*flow\s+statement", low):
            score += 3
        if "operating activities" in low:
            score += 2
        if re.search(r"five[- ]year|5[- ]year|summary|highlights", low):
            score -= 2
        if "consolidated" in low:
            score += 1
        scored.append((score, p))
    scored.sort(key=lambda s: (-s[0], s[1]))
    rel = datetime.strptime(released, "%Y-%m-%d").date()
    for _, p in scored:
        page = doc[p]
        hit = _find_receipts(page)
        if not hit:
            continue
        value, label, prior = hit
        text = page_text(page)
        context = text + "\n" + (page_text(doc[p - 1]) if p > 0 else "")
        mult = _unit_multiplier(text)
        if mult == 1.0:
            mult = _unit_multiplier(context)
        months = 6 if kind == "h" else 12
        kw = (r"half[\s-]*year\s+ended|six\s+months\s+ended|period\s+ended|half[\s-]*year\s+ending"
              if kind == "h" else r"year\s+ended|financial\s+year\s+ended|period\s+ended|year\s+ending")
        end = _period_end_from_text(text, kw) or _period_end_from_text(
            "\n".join(page_text(doc[q]) for q in range(min(4, doc.page_count))), kw)
        end = resolve_period_end(end, kind, released, title)
        return {
            "period_end": _snap_to_month_end(end).isoformat(),
            "months": months,
            "value": value * mult,
            "prior": prior * mult if prior is not None else None,
            "currency": _currency(text[:3000] + context[:3000]),
            "label": label,
            "page": p + 1,
        }
    return None
