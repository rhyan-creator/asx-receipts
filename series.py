"""Turns stored data points into quarterly, half-yearly, annual and trailing-12-month series.

Every output row carries:
  basis     reported / derived (and how)
  released  the date the figure became public (latest filing it relies on), for no-hindsight multiples
  check     'verified' / 'differs' / None  (cross-check against the next report's comparative)
"""
from __future__ import annotations

import pandas as pd

PRIORITY = {"import": 0, "asx": 1, "yahoo": 2}
COLS = ["period_end", "months", "value", "currency", "source", "ids_id", "title", "released", "check"]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=COLS + ["m"])
    d = df.copy()
    for c in COLS:
        if c not in d:
            d[c] = None
    d["rank"] = d["source"].map(lambda s: PRIORITY.get(str(s).split(":")[0], 3))
    # among same-priority duplicates (e.g. 4E and Annual Report) keep the earliest release
    d["_rel"] = d["released"].fillna("9999")
    d = d.sort_values(["rank", "_rel"]).drop_duplicates(["period_end", "months"], keep="first")
    d["m"] = pd.PeriodIndex(pd.to_datetime(d["period_end"]), freq="M")
    return d.drop(columns=["_rel"]).sort_values("m").reset_index(drop=True)


def _rel(*vals):
    v = [x for x in vals if isinstance(x, str)]
    return max(v) if v else None


def fiscal_year_end_month(d: pd.DataFrame) -> int:
    a = d[d["months"] == 12]
    if not a.empty:
        return int(a["m"].dt.month.mode().iloc[0])
    return 6  # most ASX companies are June year-end


def _row(r, **kw):
    base = dict(value=r.value, currency=r.currency, basis="reported", source=r.source,
                ids_id=r.ids_id, title=r.title, released=r.released, check=r.check)
    base.update(kw)
    return base


def quarterly(d: pd.DataFrame) -> pd.DataFrame:
    rows = [{"m": r.m, **_row(r)} for r in d[d["months"] == 3].itertuples()]
    return pd.DataFrame(rows)


def half_yearly(d: pd.DataFrame) -> pd.DataFrame:
    rows = {r.m: _row(r) for r in d[d["months"] == 6].itertuples()}
    # H2 = full year − H1
    for r in d[d["months"] == 12].itertuples():
        h1 = r.m - 6
        if r.m not in rows and h1 in rows:
            rows[r.m] = _row(r, value=r.value - rows[h1]["value"], basis="derived: FY − H1",
                             title=f"{r.title} less H1" if isinstance(r.title, str) else None,
                             released=_rel(r.released, rows[h1]["released"]), check=None)
    # two quarters
    fye = fiscal_year_end_month(d)
    q = {r.m: r for r in d[d["months"] == 3].itertuples()}
    for m, r in q.items():
        prev = q.get(m - 3)
        if m not in rows and prev is not None and m.month in (fye, (fye + 6 - 1) % 12 + 1):
            rows[m] = _row(r, value=r.value + prev.value, basis="sum of 2 quarters",
                           released=_rel(r.released, prev.released), check=None)
    out = pd.DataFrame([{"m": k, **v} for k, v in rows.items()])
    return out.sort_values("m").reset_index(drop=True) if not out.empty else out


def annual(d: pd.DataFrame) -> pd.DataFrame:
    fye = fiscal_year_end_month(d)
    rows = {r.m: _row(r) for r in d[d["months"] == 12].itertuples()}
    q = {r.m: r for r in d[d["months"] == 3].itertuples()}
    for m, r in q.items():
        if m.month != fye or m in rows:
            continue
        span = [q.get(m - k) for k in (9, 6, 3, 0)]
        if all(x is not None for x in span):
            rows[m] = _row(r, value=float(sum(x.value for x in span)), basis="sum of 4 quarters",
                           released=_rel(*[x.released for x in span]), check=None)
    h = half_yearly(d)
    if not h.empty:
        hs = {r.m: r for r in h.itertuples()}
        for m, r in hs.items():
            if m.month == fye and m not in rows and (m - 6) in hs:
                rows[m] = _row(r, value=float(r.value + hs[m - 6].value), basis="H1 + H2",
                               released=_rel(r.released, hs[m - 6].released), check=None)
    out = pd.DataFrame([{"m": k, **v} for k, v in rows.items()])
    if out.empty:
        return out
    out = out.sort_values("m").reset_index(drop=True)
    out["label"] = out["m"].map(lambda p: f"FY{str(p.year)[2:]}" if p.month == fye else p.strftime("%b-%y"))
    return out


def ttm(d: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    q = {r.m: r for r in d[d["months"] == 3].itertuples()}
    for m in sorted(q):
        span = [q.get(m - k) for k in (9, 6, 3, 0)]
        if all(x is not None for x in span):
            rows[m] = {"value": float(sum(x.value for x in span)), "basis": "4 quarters",
                       "released": _rel(*[x.released for x in span]), "ids_id": q[m].ids_id, "title": q[m].title}
    for r in d[d["months"] == 12].itertuples():
        if r.m not in rows:
            rows[r.m] = {"value": float(r.value), "basis": "full year", "released": r.released,
                         "ids_id": r.ids_id, "title": r.title}
    h = half_yearly(d)
    if not h.empty:
        hs = {r.m: r for r in h.itertuples()}
        for m, r in hs.items():
            if m not in rows and (m - 6) in hs:
                rows[m] = {"value": float(r.value + hs[m - 6].value), "basis": "2 halves",
                           "released": _rel(r.released, hs[m - 6].released), "ids_id": r.ids_id, "title": r.title}
    out = pd.DataFrame([{"m": k, **v} for k, v in rows.items()])
    return out.sort_values("m").reset_index(drop=True) if not out.empty else out


def build(points: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = _clean(points)
    if d.empty:
        e = pd.DataFrame()
        return {"Quarterly": e, "Half-yearly": e, "Annual": e, "TTM": e, "clean": d}
    return {"Quarterly": quarterly(d), "Half-yearly": half_yearly(d), "Annual": annual(d),
            "TTM": ttm(d), "clean": d}


def add_growth(df: pd.DataFrame, periods_per_year: int = 1) -> pd.DataFrame:
    """Year-on-year growth, matched on the calendar month 12 months earlier."""
    if df.empty:
        return df
    s = df.set_index("m")["value"]
    df = df.copy()
    df["yoy"] = [((s[m] / s[m - 12]) - 1) if (m - 12) in s.index and s[m - 12] and s[m - 12] > 0 else None
                 for m in df["m"]]
    return df


def cagr(ann: pd.DataFrame, years: int = 5) -> tuple[float, str] | None:
    """Compound annual growth over up to `years`; returns (rate, span label)."""
    if ann.empty or len(ann) < 2:
        return None
    s = ann.set_index("m")["value"]
    end = s.index.max()
    start = end - 12 * years
    if start not in s.index or s[start] <= 0 or s[end] <= 0:
        cands = [m for m in s.index if m < end and s[m] > 0 and (end - m).n <= 12 * years]
        if not cands or s[end] <= 0:
            return None
        start = min(cands)
    n = (end - start).n / 12
    if n <= 0:
        return None
    lab = ann.set_index("m")["label"] if "label" in ann else None
    span = f"{lab[start]}–{lab[end]}" if lab is not None else f"{start}–{end}"
    return (s[end] / s[start]) ** (1 / n) - 1, span
