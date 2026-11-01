"""End-to-end test of the cloud updater with the ASX mocked out."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scraper import asx, update, yahoo  # noqa: E402
from test_parse import _pdf  # noqa: E402

MONTH = {3: "March", 6: "June", 9: "September", 12: "December"}


def _4c(end_y, end_m, value):
    return _pdf([[
        (50, 800, "Appendix 4C"), (300, 770, "Quarter ended (“current quarter”)"),
        (300, 758, f"{30 if end_m in (6, 9) else 31} {MONTH[end_m]} {end_y}"),
        (330, 730, "Current quarter"), (330, 720, "$A'000"),
        (50, 688, "1.1"), (70, 688, "Receipts from customers"), (340, 688, f"{value:,}"), (440, 688, f"{value:,}"),
    ]])


def test_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE", tmp_path / "state")
    monkeypatch.setattr(update, "SITE", tmp_path / "site")
    monkeypatch.setattr(update, "ROOT", tmp_path)
    (tmp_path / "watchlist.txt").write_text("BBB\n")
    (tmp_path / "overrides.csv").write_text("ticker,period_end,months,value\nBBB,30/06/2020,3,999000\n")

    pdfs, calls = {}, {"list": 0}

    def fake_list(code, year):
        calls["list"] += 1
        if code != "BBB":
            return []
        out = []
        for i, m in enumerate((3, 6, 9, 12)):
            ids = f"{year}{m:02d}"
            rel_m, rel_y = (m + 1, year) if m < 12 else (1, year + 1)
            pdfs[ids] = _4c(year, m, 1000 + (year - 2016) * 100 + i * 10)
            out.append({"ids_id": ids, "released": f"{rel_y}-{rel_m:02d}-25",
                        "title": "Quarterly Activities and Appendix 4C Cashflow Report"})
        out.append({"ids_id": f"x{year}", "released": f"{year}-05-01", "title": "Investor Presentation"})
        return out

    monkeypatch.setattr(asx, "list_announcements", fake_list)
    monkeypatch.setattr(asx, "download_pdf", lambda ids: pdfs[ids])
    monkeypatch.setattr(yahoo, "fetch", lambda t: [])
    comp = tmp_path / "companies.json"
    comp.write_text(json.dumps([{"t": "AAA", "n": "Aaa Ltd", "s": "Materials"},
                                {"t": "BBB", "n": "Bbb Ltd", "s": "Software"}]))
    monkeypatch.setattr(sys, "argv", ["x", "--no-git", "--offline-companies", str(comp)])
    update.main()

    idx = json.loads((tmp_path / "site" / "index.json").read_text())
    assert dict((r[0], r[3]) for r in idx["companies"]) == {"AAA": "none", "BBB": "ready"}
    bbb = json.loads((tmp_path / "site" / "BBB.json").read_text())
    q = bbb["series"]["Quarterly"]
    assert len(q) >= 40
    jun20 = next(r for r in q if r["end"] == "2020-06")
    assert jun20["v"] == 999000 and jun20["basis"] == "reported"  # override wins
    assert bbb["series"]["Annual"] and bbb["cagr"]["rate"] > 0
    assert all(d["status"] == "parsed" for d in bbb["docs"])

    # second run: past-year listings are final, so only the current year is re-read
    first_calls = calls["list"]
    update.main()
    assert calls["list"] == first_calls  # checked < 10 hours ago: ASX not hit again
    (tmp_path / "state" / "_meta.json").write_text('{"last_full_check": "2020-01-01T00:00:00"}')
    update.main()
    assert calls["list"] - first_calls == 2  # one current-year list per ticker
