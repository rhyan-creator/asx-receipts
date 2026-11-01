"""Parser tests on synthetic filings laid out like real ASX documents."""
import io
import sys
from pathlib import Path

import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper import asx, parse, series  # noqa: E402


def _pdf(pages):
    """pages: list of lists of (x, y, text). Draws numbers first to scramble stream order."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for items in pages:
        nums = [i for i in items if i[2][:1].isdigit() or i[2][:1] in "(-"]
        rest = [i for i in items if i not in nums]
        for x, y, t in nums + rest:
            c.setFont("Helvetica", 9)
            c.drawString(x, y, t)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_appendix_4c():
    p1 = [(50, 800, "Quarterly Activities Report"), (50, 780, "Strong growth in customer receipts this quarter.")]
    p2 = [
        (50, 800, "Appendix 4C"), (50, 788, "Quarterly cash flow report for entities subject to Listing Rule 4.7B"),
        (50, 770, "ABN"), (300, 770, "Quarter ended (“current quarter”)"),
        (50, 758, "12 345 678 901"), (300, 758, "30 September 2024"),
        (50, 730, "Consolidated statement of cash flows"), (330, 730, "Current quarter"), (430, 730, "Year to date"),
        (330, 720, "$A'000"), (430, 720, "(3 months) $A'000"),
        (50, 700, "1."), (70, 700, "Cash flows from operating activities"),
        (50, 688, "1.1"), (70, 688, "Receipts from customers"), (340, 688, "4,512"), (440, 688, "4,512"),
        (50, 676, "1.2"), (70, 676, "Payments for"), (340, 676, "(1,234)"), (440, 676, "(1,234)"),
        (50, 664, "1.5"), (70, 664, "Government grants and tax incentives"), (340, 664, "250"), (440, 664, "250"),
    ]
    pt = parse.parse_pdf(_pdf([p1, p2]), "q", "2024-10-24")
    assert pt["months"] == 3
    assert pt["period_end"] == "2024-09-30"
    assert pt["value"] == 4_512_000
    assert pt["currency"] == "AUD"


def test_5b_usd_and_fallback_date():
    p = [
        (50, 800, "Appendix 5B"), (50, 788, "Mining exploration entity or oil and gas exploration entity"),
        (330, 730, "Current quarter"), (330, 720, "US$'000"),
        (50, 688, "1.1"), (70, 688, "Receipts from customers"), (340, 688, "-"), (440, 688, "-"),
    ]
    pt = parse.parse_pdf(_pdf([p]), "q", "2023-01-30")
    assert pt["value"] == 0
    assert pt["currency"] == "USD"
    assert pt["period_end"] == "2022-12-31"


def test_annual_report_note_column_wrap_and_summary_page():
    summary = [(50, 800, "Five-year summary"), (50, 780, "Receipts from customers"), (300, 780, "99,999")]
    cfs = [
        (50, 800, "Consolidated Statement of Cash Flows"), (50, 788, "For the year ended 30 June 2024"),
        (300, 760, "Note"), (360, 760, "2024"), (440, 760, "2023"), (360, 750, "$'000"), (440, 750, "$'000"),
        (50, 730, "Cash flows from operating activities"),
        (50, 716, "Receipts from customers (inclusive of"),
        (50, 706, "goods and services tax)"), (300, 706, "6"), (360, 706, "58,321"), (440, 706, "51,002"),
        (50, 692, "Payments to suppliers and employees"), (360, 692, "(45,000)"), (440, 692, "(40,000)"),
    ]
    pt = parse.parse_pdf(_pdf([summary, cfs]), "a", "2024-08-27")
    assert pt["value"] == 58_321_000, pt
    assert pt["period_end"] == "2024-06-30"
    assert pt["months"] == 12


def test_half_year_in_dollars():
    p = [
        (50, 800, "Condensed consolidated statement of cash flows"),
        (50, 788, "for the half-year ended 31 December 2023"),
        (360, 760, "31 Dec 2023"), (440, 760, "31 Dec 2022"), (360, 750, "$"), (440, 750, "$"),
        (50, 730, "Cash flows from operating activities"),
        (50, 716, "Receipts from customers"), (360, 716, "3,456,789"), (440, 716, "3,000,000"),
    ]
    pt = parse.parse_pdf(_pdf([p]), "h", "2024-02-22")
    assert pt["value"] == 3_456_789
    assert pt["period_end"] == "2023-12-31"
    assert pt["months"] == 6


def test_classify_and_listing():
    assert asx.classify("Quarterly Activities - Appendix 4C Cashflow Report") == "q"
    assert asx.classify("Appendix 4D - HY24") == "h"
    assert asx.classify("Interim Financial Report half-year ended 31 December 2023") == "h"
    assert asx.classify("Annual Report 2019") == "a"
    assert asx.classify("FY19 Preliminary Final Report (Appendix 4E)") == "a"
    assert asx.classify("Changes to Preliminary Financial Report - Appendix 4E") is None
    assert asx.classify("Notice of Annual General Meeting") is None
    assert asx.classify("H1 FY19 Investor Presentation") is None
    html = """<table><tr><td>24/10/2024 <span>8:30 am</span></td><td></td><td>
      <a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=02871844">
      Quarterly Activities - Appendix 4C Cashflow Report<br><img src="x.png"> 12 pages 1.1MB</a></td></tr>
      <tr><td>27/08/2024 <span>8:00 am</span></td><td></td><td>
      <a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=02844697">Annual Report
      <img src="x.png"> 94 pages 4.7MB</a></td></tr></table>"""
    rows = asx.parse_listing(html)
    assert rows[0] == {"ids_id": "02871844", "released": "2024-10-24",
                       "title": "Quarterly Activities - Appendix 4C Cashflow Report"}
    assert rows[1]["title"] == "Annual Report"
    terms = '<form><input type="hidden" name="pdfURL" value="/asxpdf/20241024/pdf/06abc.pdf"></form>'
    m = asx._PDF_LINK.search(terms)
    assert next(g for g in m.groups() if g) == "/asxpdf/20241024/pdf/06abc.pdf"


def test_series():
    rows = []
    for i, (end, v) in enumerate([("2023-09-30", 10), ("2023-12-31", 12), ("2024-03-31", 11), ("2024-06-30", 15),
                                  ("2024-09-30", 14)]):
        rows.append({"period_end": end, "months": 3, "value": v, "currency": "AUD", "source": "asx",
                     "ids_id": str(i), "title": "4C"})
    rows.append({"period_end": "2023-06-30", "months": 12, "value": 40, "currency": "AUD", "source": "asx",
                 "ids_id": "a", "title": "AR"})
    rows.append({"period_end": "2022-12-31", "months": 6, "value": 18, "currency": "AUD", "source": "asx",
                 "ids_id": "h", "title": "HY"})
    out = series.build(pd.DataFrame(rows))
    ann = out["Annual"].set_index("label")["value"]
    assert ann["FY24"] == 48 and ann["FY23"] == 40
    hy = out["Half-yearly"].set_index("m")["value"]
    assert hy[pd.Period("2023-06", "M")] == 22  # FY23 − H1
    ttm = out["TTM"].set_index("m")["value"]
    assert ttm[pd.Period("2024-09", "M")] == 52
    assert round(series.cagr(out["Annual"])[0], 4) == 0.2
