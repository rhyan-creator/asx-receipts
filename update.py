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

from . import asx, parse, series, yahoo

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
SITE = ROOT / "docs" / "data"
YEARS = 10
COMPANY_CSV = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"


# ---------------------------------------------------------------- state files
def load_state(t: str) -> dict:
    p = STATE / f"{t}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"ticker": t, "listings": {}, "docs": {}, "points": [], "backfilled": False}


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
            out.setdefault(t, []).append({
                "period_end": pd.to_datetime(r["period_end"].strip(), dayfirst=True).strftime("%Y-%m-%d"),
                "months": int(r["months"]), "value": float(r["value"]), "currency": "AUD",
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
def _work(doc: dict) -> tuple[str, dict, dict | None, str]:
    try:
        pdf = asx.download_pdf(doc["ids_id"])
        return doc["ids_id"], doc, parse.parse_pdf(pdf, doc["kind"], doc["released"]), ""
    except Exception as e:  # noqa: BLE001
        return doc["ids_id"], doc, None, f"{type(e).__name__}: {str(e)[:150]}"


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
            if d["s"] == "pending" or (d["s"] == "error" and d.get("tries", 0) < 3)]
    todo = [d for d in todo if d["released"] >= f"{first}-01-01"]
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(_work, d) for d in todo]):
                ids, doc, point, err = f.result()
                rec = s["docs"][ids]
                if err:
                    rec["s"], rec["n"], rec["tries"] = "error", err, rec.get("tries", 0) + 1
                elif point is None:
                    rec["s"], rec["n"] = "no_data", "no receipts line found"
                else:
                    rec["s"], rec["n"] = "parsed", f"p{point['page']}: {point['label'][:100]}"
                    s["points"] = [p for p in s["points"] if p.get("ids_id") != ids]
                    s["points"].append({"period_end": point["period_end"], "months": point["months"],
                                        "value": point["value"], "currency": point["currency"],
                                        "source": "asx", "ids_id": ids, "title": doc["title"],
                                        "released": doc["released"]})
    if use_yahoo:  # backstop for recent years
        try:
            ypts = yahoo.fetch(t)
            if ypts:
                s["points"] = [p for p in s["points"] if p["source"] != "yahoo"] + ypts
        except Exception:  # noqa: BLE001
            pass
    s["backfilled"] = True
    s["updated"] = today.isoformat()
    save_state(s)
    return s


# ---------------------------------------------------------------- website JSON
def _clean_num(x):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), 4)


def build_site_json(s: dict, meta: dict, overrides: list[dict]) -> dict:
    pts = pd.DataFrame(s["points"] + overrides)
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
            df = series.add_growth(df, 1)
            rows = []
            for r in df.to_dict("records"):
                m = r["m"]
                rows.append({
                    "end": m.strftime("%Y-%m"),
                    "label": r.get("label") or m.strftime("%b-%y"),
                    "v": _clean_num(r["value"]),
                    "yoy": _clean_num(r.get("yoy")),
                    "basis": r.get("basis") or "reported",
                    "doc": r.get("ids_id") if isinstance(r.get("ids_id"), str) else None,
                    "src": r.get("title") if isinstance(r.get("title"), str) else (r.get("source") or ""),
                })
            out["series"][freq] = rows
        g = series.cagr(views["Annual"], 5)
        out["cagr"] = {"rate": _clean_num(g[0]), "span": g[1]} if g else None
    out["docs"] = sorted(
        [{"id": i, "date": d["r"], "title": d["t"], "status": d["s"], "note": d.get("n", "")}
         for i, d in s["docs"].items()],
        key=lambda d: d["date"], reverse=True)
    return out


def write_site(s: dict, meta: dict, overrides: list[dict]) -> str:
    data = build_site_json(s, meta, overrides)
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / f"{s['ticker']}.json").write_text(json.dumps(data, separators=(",", ":")))
    has = any(data["series"].get(f) for f in data["series"])
    return "ready" if has else "none"


def write_index(companies: list[dict], status: dict[str, str]) -> None:
    rows = [[c["t"], c["n"], c["s"], status.get(c["t"], "queued")] for c in companies]
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

    weekly = date.today().weekday() == 0  # Yahoo re-check for loaded tickers once a week

    def do(t: str, why: str) -> None:
        try:
            s = refresh_ticker(t, use_yahoo=(why != "update" or weekly))
            status[t] = write_site(s, meta.get(t, {}), overrides.get(t, []))
            print(f"[{(time.time() - t0) / 60:5.1f}m] {why} {t}: {status[t]}, "
                  f"{sum(1 for d in s['docs'].values() if d['s'] == 'parsed')} filings parsed", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  {t} failed: {e}", flush=True)

    # 1. explicitly requested tickers
    requested = [t.upper() for t in a.tickers]
    for t in requested:
        do(t, "requested")
    checkpoint("Load requested tickers", force=bool(requested))

    # 2. check tickers already loaded for new filings (only current-year lists are re-read).
    #    Runs at most every ~10 hours so the ASX site isn't hit on every scheduled run.
    loaded = sorted(p.stem for p in STATE.glob("*.json") if not p.stem.startswith("_"))
    listed = set(meta)
    meta_path = STATE / "_meta.json"
    run_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    last = run_meta.get("last_full_check", "2000-01-01T00:00:00")
    hours_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc)).total_seconds() / 3600
    if budget > 0 and hours_since >= 10:
        start = run_meta.get("resume_from", "")
        finished = True
        for t in loaded:
            if t < start or t in requested or t not in listed:
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
    wl_path = ROOT / "watchlist.txt"
    watch = [ln.strip().upper() for ln in wl_path.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")] if wl_path.exists() else []
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
