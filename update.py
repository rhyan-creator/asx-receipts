"""Cloud updater. Runs on GitHub Actions; nobody needs to run this by hand.

Each run:
  1. downloads the current ASX company list
  2. checks every already-loaded ticker for new 4C/5B, half-year and annual filings
  3. spends the remaining time budget loading 10 years of history for tickers not yet loaded
     (watchlist.txt first, then the rest of the ASX alphabetically)
  4. writes chart-ready JSON into docs/data/ for the website, and progress into state/

    python -m scraper.update                     # normal scheduled run
    python -m scraper.update --tickers SPZ LGI   # load / refresh specific tickers now
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from . import asx, financials, parse, prices, series, yahoo

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
SITE = ROOT / "docs" / "data"
YEARS = 10
FIN_V = 6  # bump to make the robot re-read reports after a parser change
FLOW = ["revenue", "gross", "ebitda", "ebit", "npat", "da"]
STOCK = ["cash", "debt", "leases", "equity", "shares", "shares_wa"]
COMPANY_CSV = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"


# ---------------------------------------------------------------- state files
def load_state(t: str) -> dict:
    p = STATE / f"{t}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"ticker": t, "listings": {}, "docs": {}, "points": [], "fin": [], "backfilled": False}


def save_state(s: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{s['ticker']}.json").write_text(json.dumps(s, separators=(",", ":"), sort_keys=True))


def load_overrides() -> dict[str, list[dict]]:
    p = ROOT / "overrides.csv"
    out: dict[str, list[dict]] = {}
    if not p.exists():
        return out
    for r in csv.DictReader(io.StringIO(p.read_text())):
        try:
            t = r["ticker"].strip().upper()
            metric = (r.get("metric") or "receipts").strip().lower() or "receipts"
            out.setdefault(t, []).append({
                "metric": metric,
                "period_end": pd.to_datetime(r["period_end"].strip(), dayfirst=True).strftime("%Y-%m-%d"),
                "months": int(r["months"] or 0), "value": float(r["value"]), "currency": "AUD",
                "source": "import", "title": "Manual override (overrides.csv)",
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------- company list
def company_list() -> list[dict]:
    r = asx._get(COMPANY_CSV)
    rows = list(csv.reader(io.StringIO(r.text)))
    out = []
    for row in rows:
        if len(row) >= 2 and row[1] and row[1] != "ASX code" and len(row[1]) <= 6:
            out.append({"t": row[1].strip().upper(), "n": row[0].strip().title(),
                        "s": row[2].strip() if len(row) > 2 else ""})
    if len(out) < 500:
        raise RuntimeError(f"company list looks wrong ({len(out)} rows)")
    return out


# ---------------------------------------------------------------- one ticker
def _work(doc: dict) -> tuple[str, dict, dict | None, dict | None, str]:
    """Download one filing; read the receipts line and the headline financials from it."""
    try:
        pdf = asx.download_pdf(doc["ids_id"])
    except Exception as e:  # noqa: BLE001
        return doc["ids_id"], doc, None, None, f"{type(e).__name__}: {str(e)[:150]}"
    point = fin = None
    fin_err = ""
    try:
        point = parse.parse_pdf(pdf, doc["kind"], doc["released"], doc["title"])
    except Exception as e:  # noqa: BLE001
        fin_err = f"receipts reader error: {type(e).__name__}: {str(e)[:80]}"
    try:
        if doc["kind"] == "q":
            c = financials.quarterly_cash(pdf)
            if c is not None:
                end = point["period_end"] if point else parse._quarter_end_before(doc["released"]).isoformat()
                fin = {"period_end": end, "months": 3, "currency": point["currency"] if point else "AUD",
                       "metrics": {"cash": [c, None]}, "pages": {}}
        else:
            fin = financials.parse_report(pdf, doc["kind"], doc["released"], doc["title"])
    except Exception as e:  # noqa: BLE001
        # never fail silently: record why, and leave the filing marked for another try next run
        fin_err = f"financials reader error: {type(e).__name__}: {str(e)[:80]}"
    return doc["ids_id"], doc, point, fin, fin_err and ("READER:" + fin_err)


def _store_fin(s: dict, ids: str, doc: dict, fin: dict) -> int:
    s.setdefault("fin", [])
    s["fin"] = [p for p in s["fin"] if p.get("ids_id") != ids]
    end = pd.Period(fin["period_end"][:7], "M")
    n = 0
    for metric, vals in fin["metrics"].items():
        cur, prior = (vals + [None, None])[:2]
        if cur is None:
            continue
        stock = metric in STOCK
        if stock:
            prior_end = end - (6 if fin["months"] == 6 else 12 if fin["months"] == 12 else 3)
        else:
            prior_end = end - 12
        s["fin"].append({
            "metric": metric, "period_end": fin["period_end"], "months": 0 if stock else fin["months"],
            "value": cur, "prior": prior, "prior_end": prior_end.end_time.strftime("%Y-%m-%d"),
            "currency": fin.get("currency", "AUD"), "source": "asx" if doc["kind"] != "q" else "asx4c",
            "ids_id": ids, "title": doc["title"], "released": doc["released"],
            "page": fin.get("pages", {}).get(metric),
        })
        n += 1
    return n


def refresh_ticker(t: str, workers: int = 4, use_yahoo: bool = True, log=print) -> dict:
    s = load_state(t)
    today = date.today()
    first = today.year - YEARS
    for y in range(first, today.year + 1):
        fetched = s["listings"].get(str(y))
        # a past year's list is final once fetched after the year ended
        if fetched and fetched >= f"{y + 1}-01-01":
            continue
        try:
            for d in asx.relevant(asx.list_announcements(t, y)):
                s["docs"].setdefault(d["ids_id"], {"r": d["released"], "t": d["title"], "k": d["kind"], "s": "pending"})
            s["listings"][str(y)] = today.isoformat()
        except Exception as e:  # noqa: BLE001
            log(f"  {t} list {y}: {e}")
    todo = [{"ids_id": i, "released": d["r"], "title": d["t"], "kind": d["k"]}
            for i, d in s["docs"].items()
            if d["s"] == "pending" or (d["s"] == "error" and d.get("tries", 0) < 5)
            or (d["s"] in ("parsed", "no_data") and d.get("fv", 0) < FIN_V)]
    todo = [d for d in todo if d["released"] >= f"{first}-01-01"]
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(_work, d) for d in todo]):
                ids, doc, point, fin, err = f.result()
                rec = s["docs"][ids]
                reader_err = ""
                if err.startswith("READER:"):
                    reader_err, err = err[7:], ""
                if err:
                    rec["s"], rec["n"], rec["tries"] = "error", err, rec.get("tries", 0) + 1
                    continue
                rec["ftries"] = rec.get("ftries", 0) + (1 if reader_err else 0)
                if not reader_err or rec["ftries"] >= 3:
                    rec["fv"] = FIN_V  # done (or given up after 3 reader errors, which stay listed)
                rec["err"] = reader_err
                nfin = _store_fin(s, ids, doc, fin) if fin else 0
                rec["f"] = nfin
                rec["miss"] = (fin or {}).get("missing", []) if doc["kind"] != "q" else []
                if point is None:
                    rec["s"] = "parsed" if nfin else "no_data"
                    rec["n"] = f"no receipts line; {nfin} financial figures" if nfin else "nothing found"
                else:
                    rec["s"], rec["n"] = "parsed", f"p{point['page']}: {point['label'][:90]}" + (f" · {nfin} financial figures" if nfin else "")
                    s["points"] = [p for p in s["points"] if p.get("ids_id") != ids]
                    pe = pd.Period(point["period_end"][:7], "M") - 12
                    s["points"].append({"period_end": point["period_end"], "months": point["months"],
                                        "value": point["value"], "currency": point["currency"],
                                        "source": "asx", "ids_id": ids, "title": doc["title"],
                                        "released": doc["released"], "prior": point.get("prior"),
                                        "prior_end": pe.end_time.strftime("%Y-%m-%d") if point["months"] in (6, 12) else None})
    if use_yahoo:  # backstop for recent years
        try:
            ypts = yahoo.fetch(t)
            if ypts:
                s["points"] = [p for p in s["points"] if p["source"] != "yahoo"] + ypts
        except Exception:  # noqa: BLE001
            pass
    try:
        px = prices.fetch(t)
        if px:
            s["prices"] = px
    except Exception as e:  # noqa: BLE001
        log(f"  {t} prices: {e}")
    s["backfilled"] = True
    s["updated"] = today.isoformat()
    save_state(s)
    return s


# ---------------------------------------------------------------- website JSON
def _clean_num(x):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), 4)


def _rows_out(df: pd.DataFrame) -> list[dict]:
    rows = []
    for r in df.to_dict("records"):
        m = r["m"]
        rows.append({
            "end": m.strftime("%Y-%m"),
            "label": r.get("label") if isinstance(r.get("label"), str) else m.strftime("%b-%y"),
            "v": _clean_num(r["value"]),
            "yoy": _clean_num(r.get("yoy")),
            "basis": r.get("basis") or "reported",
            "doc": r.get("ids_id") if isinstance(r.get("ids_id"), str) else None,
            "src": r.get("title") if isinstance(r.get("title"), str) else (r.get("source") or ""),
            "rel": r.get("released") if isinstance(r.get("released"), str) else None,
            "chk": r.get("check") if isinstance(r.get("check"), str) else None,
        })
    return rows


def with_comparatives(pts: list[dict], stock: bool = False) -> list[dict]:
    """Fill a period that has no figure of its own (its report couldn't be read, or it predates the
    first report we have) from the prior-period column of the next report."""
    have = {(p.get("metric", "receipts"), p["period_end"][:7], p["months"]) for p in pts}
    extra = []
    for p in pts:
        if p.get("prior") is None or not p.get("prior_end") or p.get("source") == "import":
            continue
        if not stock and p["months"] not in (6, 12):
            continue
        key = (p.get("metric", "receipts"), p["prior_end"][:7], p["months"])
        if key in have:
            continue
        have.add(key)
        extra.append({**p, "period_end": p["prior_end"], "value": p["prior"], "prior": None, "prior_end": None,
                      "basis": "from next report's comparative", "check": None,
                      "title": f"{p.get('title') or 'later report'} (prior-period column)"})
    return pts + extra


def _checks(fin: list[dict]) -> None:
    """Mark each figure 'verified' if a later report's comparative agrees with it (within 1%)."""
    comps: dict[tuple, list[float]] = {}
    for p in fin:
        if p.get("prior") is not None and p.get("prior_end"):
            comps.setdefault((p.get("metric", "receipts"), p["prior_end"][:7], p["months"]), []).append(p["prior"])
    for p in fin:
        c = comps.get((p.get("metric", "receipts"), p["period_end"][:7], p["months"]))
        if not c:
            p["check"] = None
            continue
        v = p["value"]
        ok = any(abs(x - v) <= max(abs(v), abs(x)) * 0.01 + 1 for x in c)
        p["check"] = "verified" if ok else "differs"


def _split_factor(splits: list, period_end: str) -> float:
    f = 1.0
    for d, r in splits or []:
        if d > period_end and r:
            f *= r
    return f


def build_fin(s: dict, overrides: list[dict]) -> tuple[dict, dict]:
    fin = [dict(p) for p in s.get("fin", [])]
    _checks(fin)
    splits = (s.get("prices") or {}).get("splits", [])
    ov = [o for o in overrides if o.get("metric") not in (None, "receipts")]
    flows, stocks = {}, {}
    for metric in FLOW:
        pts = with_comparatives([p for p in fin if p["metric"] == metric]) + [o for o in ov if o["metric"] == metric]
        if not pts:
            continue
        v = series.build(pd.DataFrame(pts))
        flows[metric] = {}
        for freq in ("Half-yearly", "Annual", "TTM"):
            df = v[freq]
            flows[metric][freq] = _rows_out(series.add_growth(df)) if not df.empty else []
    rank = {"import": 0, "asx": 1, "asx4c": 2}
    for metric in STOCK:
        pts = with_comparatives([p for p in fin if p["metric"] == metric], stock=True) + [o for o in ov if o["metric"] == metric]
        if not pts:
            continue
        pts.sort(key=lambda p: (p["period_end"][:7], rank.get(p.get("source"), 3) + (0.5 if p.get("basis") else 0),
                                p.get("released") or "9999"))
        seen, rows = set(), []
        for p in pts:
            key = p["period_end"][:7]
            if key in seen:
                continue
            seen.add(key)
            val = p["value"]
            if metric in ("shares", "shares_wa"):
                val = val * _split_factor(splits, p["period_end"])
            m = pd.Period(key, "M")
            rows.append({"end": key, "label": m.strftime("%b-%y"), "v": _clean_num(val),
                         "doc": p.get("ids_id"), "src": p.get("title") or "", "rel": p.get("released"),
                         "chk": p.get("check"), "basis": "override" if p.get("source") == "import" else (p.get("basis") or "reported"),
                         "q": p.get("source") == "asx4c"})
        if metric in ("shares", "shares_wa"):
            rows = _drop_share_outliers(rows, (s.get("prices") or {}).get("shares_now"))
        stocks[metric] = rows
    return flows, stocks


def _drop_share_outliers(rows: list[dict], shares_now: float | None) -> list[dict]:
    """A share count far out of line is a unit misread (e.g. ×1,000). Splits and consolidations are
    already adjusted for, so real counts never jump like that. Today's count from the market data is the
    anchor when we have it (several misreads in a row can't outvote it); otherwise the median."""
    vals = sorted(r["v"] for r in rows if r.get("v"))
    if shares_now:
        lo, hi = shares_now / 50, shares_now * 3
    elif len(vals) >= 3:
        med = vals[len(vals) // 2]
        lo, hi = med * 0.2, med * 5
    else:
        return rows
    return [r for r in rows if lo <= (r.get("v") or 0) <= hi]


def build_site_json(s: dict, meta: dict, overrides: list[dict]) -> dict:
    receipt_ov = [o for o in overrides if o.get("metric", "receipts") == "receipts"]
    rpts = [dict(p) for p in s["points"] if p.get("metric", "receipts") == "receipts"]
    _checks(rpts)
    pts = pd.DataFrame(with_comparatives(rpts) + receipt_ov)
    for col in ("ids_id", "title", "released", "currency", "source"):
        if col not in pts:
            pts[col] = None
    views = series.build(pts) if not pts.empty else {}
    out = {"ticker": s["ticker"], "name": meta.get("n", ""), "sector": meta.get("s", ""),
           "updated": s.get("updated"), "series": {}, "currencies": [], "docs": []}
    if views and not views["clean"].empty:
        out["currencies"] = sorted(set(views["clean"]["currency"].dropna()))
        for freq in ("Quarterly", "Half-yearly", "Annual", "TTM"):
            df = views[freq]
            if df.empty:
                out["series"][freq] = []
                continue
            out["series"][freq] = _rows_out(series.add_growth(df, 1))
        g = series.cagr(views["Annual"], 5)
        out["cagr"] = {"rate": _clean_num(g[0]), "span": g[1]} if g else None
    for f in ("Quarterly", "Half-yearly", "Annual", "TTM"):
        out["series"].setdefault(f, [])
    out["fin"], out["bal"] = build_fin(s, overrides)
    px = s.get("prices")
    if px:
        out["prices"] = {"monthly": px.get("monthly", []), "last": px.get("last"),
                         "shares_now": px.get("shares_now"), "splits": px.get("splits", [])}
    if not out["currencies"]:
        out["currencies"] = sorted({p.get("currency") or "AUD" for p in s.get("fin", [])}) or ["AUD"]
    out["docs"] = sorted(
        [{"id": i, "date": d["r"], "title": d["t"], "status": d["s"], "note": d.get("n", ""), "fin": d.get("f", 0),
          "miss": d.get("miss", []), "err": d.get("err", "")}
         for i, d in s["docs"].items()],
        key=lambda d: d["date"], reverse=True)
    return out


def write_site(s: dict, meta: dict, overrides: list[dict]) -> str:
    data = build_site_json(s, meta, overrides)
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / f"{s['ticker']}.json").write_text(json.dumps(data, separators=(",", ":")))
    has = any(data["series"].get(f) for f in data["series"]) or bool(data.get("fin")) or bool(data.get("bal"))
    return "ready" if has else "none"


def write_index(companies: list[dict], status: dict[str, str]) -> None:
    rows = [[c["t"], c["n"], c["s"], status.get(c["t"], "queued")] for c in companies]
    SITE.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "companies": rows}
    (SITE / "index.json").write_text(json.dumps(payload, separators=(",", ":")))


# ---------------------------------------------------------------- git
def commit(msg: str) -> None:
    try:
        subprocess.run(["git", "add", "-A", "state", "docs/data"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True)
        subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=ROOT, check=False)
        subprocess.run(["git", "push", "-q"], cwd=ROOT, check=True)
    except Exception as e:  # noqa: BLE001
        print(f"git commit skipped: {e}")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=[])
    ap.add_argument("--budget-min", type=float, default=300, help="stop starting new work after this")
    ap.add_argument("--commit-every", type=float, default=20, help="minutes between progress commits")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--offline-companies", help="use this CSV/JSON list instead of downloading (testing)")
    ap.add_argument("--weekly-after-backfill", action="store_true",
                    help="scheduled runs: work daily until every ASX company is loaded, then only on Saturdays")
    a = ap.parse_args()
    t0, last_commit = time.time(), time.time()
    budget = a.budget_min * 60

    def checkpoint(msg: str, force: bool = False) -> None:
        nonlocal last_commit
        if not a.no_git and (force or time.time() - last_commit > a.commit_every * 60):
            write_index(companies, status)
            commit(msg)
            last_commit = time.time()

    if a.offline_companies:
        companies = json.loads(Path(a.offline_companies).read_text())
    else:
        try:
            companies = company_list()
            (STATE / "_companies.json").write_text(json.dumps(companies))
        except Exception as e:  # noqa: BLE001
            print(f"company list failed ({e}); using last saved list")
            companies = json.loads((STATE / "_companies.json").read_text())
    meta = {c["t"]: c for c in companies}
    overrides = load_overrides()

    # previous status from existing index
    status: dict[str, str] = {}
    idx = SITE / "index.json"
    if idx.exists():
        status = {r[0]: r[3] for r in json.loads(idx.read_text()).get("companies", [])}

    # daily until the whole ASX has been loaded once, then weekly (Saturday, Melbourne time)
    if a.weekly_after_backfill and not a.tickers:
        queued = [c["t"] for c in companies if status.get(c["t"], "queued") == "queued"]
        try:
            from zoneinfo import ZoneInfo
            mel_day = datetime.now(ZoneInfo("Australia/Melbourne")).weekday()
        except Exception:  # noqa: BLE001
            mel_day = datetime.now(timezone.utc).weekday()
        if not queued and mel_day != 5:
            print("History is fully loaded, so scheduled runs are weekly now (Saturdays). Nothing to do today.")
            return
        print(f"{len(queued)} companies still to load; running daily until done." if queued
              else "Weekly update (Saturday).")

    weekly = date.today().weekday() == 0  # Yahoo re-check for loaded tickers once a week

    def do(t: str, why: str) -> None:
        try:
            s = refresh_ticker(t, use_yahoo=(why != "update" or weekly))
            status[t] = write_site(s, meta.get(t, {}), overrides.get(t, []))
            print(f"[{(time.time() - t0) / 60:5.1f}m] {why} {t}: {status[t]}, "
                  f"{sum(1 for d in s['docs'].values() if d['s'] == 'parsed')} filings parsed", flush=True)
        except Exception as e:  # noqa: BLE001
            status[t] = "error"  # so one broken ticker can't hold up the switch to weekly
            print(f"  {t} failed: {e}", flush=True)

    # 1. explicitly requested tickers
    requested = [t.upper() for t in a.tickers]
    for t in requested:
        do(t, "requested")
    checkpoint("Load requested tickers", force=bool(requested))

    # 2. check tickers already loaded for new filings (only current-year lists are re-read).
    #    Runs at most every ~10 hours so the ASX site isn't hit on every scheduled run.
    wl_path = ROOT / "watchlist.txt"
    watch = [ln.strip().upper() for ln in wl_path.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")] if wl_path.exists() else []
    loaded = sorted(p.stem for p in STATE.glob("*.json") if not p.stem.startswith("_"))
    loaded = [t for t in watch if t in loaded] + [t for t in loaded if t not in watch]
    listed = set(meta)
    meta_path = STATE / "_meta.json"
    run_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    last = run_meta.get("last_full_check", "2000-01-01T00:00:00")
    hours_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc)).total_seconds() / 3600
    if budget > 0 and hours_since >= 10:
        start = run_meta.get("resume_from", "")
        finished = True
        skipping = bool(start)
        for t in loaded:
            if skipping:
                if t != start:
                    continue
                skipping = False
            if t in requested or t not in listed:
                continue
            if time.time() - t0 > budget:
                run_meta["resume_from"], finished = t, False
                break
            do(t, "update")
            checkpoint("Check for new filings (in progress)")
        if finished:
            run_meta["last_full_check"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
            run_meta["resume_from"] = ""
        STATE.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(run_meta))

    # 3. backfill: watchlist first, then everything else
    order = [t for t in watch if t in listed] + [c["t"] for c in companies]
    done = set(loaded) | set(requested)
    for t in dict.fromkeys(order):
        if t in done:
            continue
        if time.time() - t0 > budget:
            break
        do(t, "backfill")
        done.add(t)
        checkpoint("Backfill history (in progress)")

    # overrides may have changed for tickers we didn't touch this run
    for t, ov in overrides.items():
        if (STATE / f"{t}.json").exists():
            status[t] = write_site(load_state(t), meta.get(t, {}), ov)

    write_index(companies, status)
    if not a.no_git:
        commit(f"Data update {date.today().isoformat()}")
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
