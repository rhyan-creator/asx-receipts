"""Yahoo Finance backstop: direct-method 'Receipts from Customers' for recent years (~4)."""
from __future__ import annotations

import pandas as pd


def _row(df: pd.DataFrame) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for idx in df.index:
        if str(idx).replace(" ", "").lower() == "receiptsfromcustomers":
            return df.loc[idx]
    return None


def fetch(code: str) -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        return []
    t = yf.Ticker(f"{code.upper()}.AX")
    out = []
    for attr, months in (("cashflow", 12), ("quarterly_cashflow", 3)):
        try:
            s = _row(getattr(t, attr))
        except Exception:
            s = None
        if s is None:
            continue
        s = s.dropna().sort_index()
        if months == 3 and len(s) < 2:
            continue  # can't tell whether these are quarters or halves
        if months == 3:
            # ASX companies mostly report halves; Yahoo files those under "quarterly"
            gaps = pd.Series(pd.to_datetime(s.index)).diff().dt.days.dropna()
            if not gaps.empty and gaps.median() > 120:
                months = 6
        for when, v in s.items():
            if v == 0:
                continue
            out.append({
                "period_end": pd.Timestamp(when).strftime("%Y-%m-%d"),
                "months": months,
                "value": abs(float(v)),
                "currency": "AUD",
                "source": "yahoo",
                "title": "Yahoo Finance cash flow statement",
            })
    return out
