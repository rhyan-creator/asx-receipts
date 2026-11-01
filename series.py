"""Turns stored data points into quarterly, half-yearly, annual and trailing-12-month series."""
from __future__ import annotations

import pandas as pd

PRIORITY = {"import": 0, "asx": 1, "yahoo": 2}


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.assign(m=pd.Series(dtype="period[M]"))
    d = df.copy()
    d["rank"] = d["source"].map(lambda s: PRIORITY.get(str(s).split(":")[0], 3))
    d = d.sort_values("rank").drop_duplicates(["period_end", "months"], keep="first")
    d["m"] = pd.PeriodIndex(pd.to_datetime(d["period_end"]), freq="M")
    return d.sort_values("m").reset_index(drop=True)


def _consecutive(ms: list, step: int) -> bool:
    return all((b - a).n == step for a, b in zip(ms, ms[1:]))


def fiscal_year_end_month(d: pd.DataFrame) -> int:
    a = d[d["months"] == 12]
    if not a.empty:
        return int(a["m"].dt.month.mode().iloc[0])
    h = d[d["months"] == 6]
    if not h.empty:  # halves end at FYE and FYE-6
        months = set(h["m"].dt.month)
        if 6 in months and 12 in months:
            return 6  # most ASX companies are June year-end
    return 6


def quarterly(d: pd.DataFrame) -> pd.DataFrame:
    q = d[d["months"] == 3][["m", "value", "currency", "source", "ids_id", "title"]].copy()
    q["basis"] = "reported"
    return q


def half_yearly(d: pd.DataFrame) -> pd.DataFrame:
    rows = {r.m: dict(value=r.value, currency=r.currency, basis="reported", source=r.source,
                      ids_id=r.ids_id, title=r.title)
            for r in d[d["months"] == 6].itertuples()}
    # H2 = full year − H1
    for r in d[d["months"] == 12].itertuples():
        h1 = r.m - 6
        if r.m not in rows and h1 in rows:
            rows[r.m] = dict(value=r.value - rows[h1]["value"], currency=r.currency,
                             basis="derived: FY − H1", source=r.source, ids_id=r.ids_id,
                             title=f"{r.title} less H1" if isinstance(r.title, str) else None)
    # from two quarters
    q = d[d["months"] == 3].set_index("m")["value"]
    for m in q.index:
        prev = m - 3
        if m not in rows and prev in q.index and m.month in (6, 12, 3, 9):
            fye = fiscal_year_end_month(d)
            if m.month in (fye, (fye + 6 - 1) % 12 + 1):
                rows[m] = dict(value=q[m] + q[prev], currency=d.loc[d["m"] == m, "currency"].iloc[0],
                               basis="sum of 2 quarters", source="asx")
    out = pd.DataFrame([{"m": k, **v} for k, v in rows.items()])
    return out.sort_values("m").reset_index(drop=True) if not out.empty else out


def annual(d: pd.DataFrame) -> pd.DataFrame:
    fye = fiscal_year_end_month(d)
    rows = {r.m: dict(value=r.value, currency=r.currency, basis="reported", source=r.source,
                      ids_id=r.ids_id, title=r.title)
            for r in d[d["months"] == 12].itertuples()}
    q = d[d["months"] == 3].set_index("m")["value"]
    for m in q.index:
        if m.month != fye or m in rows:
            continue
        span = [m - 9, m - 6, m - 3, m]
        if all(x in q.index for x in span):
            rows[m] = dict(value=float(sum(q[x] for x in span)), currency="AUD",
                           basis="sum of 4 quarters", source="asx")
    h = half_yearly(d)
    if not h.empty:
        hs = h.set_index("m")["value"]
        for m in hs.index:
            if m.month == fye and m not in rows and (m - 6) in hs.index:
                rows[m] = dict(value=float(hs[m] + hs[m - 6]), currency="AUD",
                               basis="H1 + H2", source="asx")
    out = pd.DataFrame([{"m": k, **v} for k, v in rows.items()])
    if out.empty:
        return out
    out = out.sort_values("m").reset_index(drop=True)
    out["label"] = out["m"].map(lambda p: f"FY{str(p.year)[2:]}" if p.month == fye else p.strftime("%b-%y"))
    return out


def ttm(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    q = d[d["months"] == 3].set_index("m")["value"].sort_index()
    for m in q.index:
        span = [m - 9, m - 6, m - 3, m]
        if all(x in q.index for x in span):
            rows.append({"m": m, "value": float(sum(q[x] for x in span)), "basis": "4 quarters"})
    have = {r["m"] for r in rows}
    h = half_yearly(d)
    if not h.empty:
        hs = h.set_index("m")["value"]
        for m in hs.index:
            if m not in have and (m - 6) in hs.index:
                rows.append({"m": m, "value": float(hs[m] + hs[m - 6]), "basis": "2 halves"})
    out = pd.DataFrame(rows)
    return out.sort_values("m").reset_index(drop=True) if not out.empty else out


def build(points: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = _clean(points)
    if d.empty:
        empty = pd.DataFrame()
        return {"Quarterly": empty, "Half-yearly": empty, "Annual": empty, "TTM": empty, "clean": d}
    return {"Quarterly": quarterly(d), "Half-yearly": half_yearly(d), "Annual": annual(d),
            "TTM": ttm(d), "clean": d}


def add_growth(df: pd.DataFrame, periods_per_year: int) -> pd.DataFrame:
    """Year-on-year growth, matched on calendar month 12 months earlier."""
    if df.empty:
        return df
    s = df.set_index("m")["value"]
    df = df.copy()
    df["yoy"] = [((s[m] / s[m - 12]) - 1) if (m - 12) in s.index and s[m - 12] else None for m in df["m"]]
    return df


def cagr(ann: pd.DataFrame, years: int = 5) -> tuple[float, str] | None:
    """Compound annual growth over up to `years`; returns (rate, span label)."""
    if ann.empty or len(ann) < 2:
        return None
    s = ann.set_index("m")["value"]
    end = s.index.max()
    start = end - 12 * years
    if start not in s.index or s[start] <= 0 or s[end] <= 0:
        # fall back to the longest available span up to `years`
        cands = [m for m in s.index if m < end and s[m] > 0 and (end - m).n <= 12 * years]
        if not cands:
            return None
        start = min(cands)
    n = (end - start).n / 12
    if n <= 0:
        return None
    lab = ann.set_index("m")["label"] if "label" in ann else None
    span = f"{lab[start]}–{lab[end]}" if lab is not None else f"{start}–{end}"
    return (s[end] / s[start]) ** (1 / n) - 1, span
