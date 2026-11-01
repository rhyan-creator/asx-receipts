"""Daily share prices from Yahoo Finance: month-end closes for 10+ years, the latest close, and splits."""
from __future__ import annotations

import pandas as pd


def fetch(code: str, years: int = 11) -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    t = yf.Ticker(f"{code.upper()}.AX")
    hist = t.history(period=f"{years}y", interval="1d", auto_adjust=False, actions=True)
    if hist is None or hist.empty or "Close" not in hist:
        return None
    hist = hist[hist["Close"].notna() & (hist["Close"] > 0)]
    if hist.empty:
        return None
    idx = pd.to_datetime(hist.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    close = pd.Series(hist["Close"].values, index=idx)
    monthly = close.groupby(close.index.to_period("M")).last()
    out = {
        # Yahoo closes are already adjusted for splits and consolidations (not for dividends)
        "monthly": [[str(p), round(float(v), 6)] for p, v in monthly.items()],
        "last": {"date": close.index[-1].strftime("%Y-%m-%d"), "close": round(float(close.iloc[-1]), 6)},
        "splits": [],
    }
    if "Stock Splits" in hist:
        sp = hist["Stock Splits"]
        sp = sp[sp.fillna(0) > 0]
        out["splits"] = [[pd.Timestamp(d).strftime("%Y-%m-%d"), float(r)] for d, r in zip(idx[hist["Stock Splits"].fillna(0) > 0], sp.values)]
    try:
        n = t.fast_info.get("shares")
        if n:
            out["shares_now"] = float(n)
    except Exception:  # noqa: BLE001
        pass
    return out
