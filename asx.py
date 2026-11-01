"""Reads the public ASX announcement archive and downloads filing PDFs."""
from __future__ import annotations

import re
import threading
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.asx.com.au"
LIST_URL = BASE + "/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=Y&year={year}"
DOC_URL = BASE + "/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={ids}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-AU,en;q=0.9",
}

_local = threading.local()
_rate_lock = threading.Lock()
_last_call = [0.0]
MIN_GAP = 0.25  # seconds between requests to the ASX, to be polite


def _session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return _local.s


def _get(url: str, **kw) -> requests.Response:
    with _rate_lock:
        wait = MIN_GAP - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    for attempt in range(3):
        try:
            r = _session().get(url, timeout=(20, 180), **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- classification
EXCLUDE = re.compile(
    r"presentation|notice|webinar|briefing|teleconference|conference call|investor (day|update)|"
    r"corporate governance|appendix 4g|changes? to|correction|amended time|date of|"
    r"lodgement|extension|release of|results date|transcript|letter|podcast|video|"
    r"sustainability|esg|modern slavery|remuneration",
    re.I,
)
QUARTERLY = re.compile(r"\b4C\b|\b5B\b|cash\s*-?\s*flow|quarterly (activit|report|update|review)", re.I)
HALF = re.compile(r"\b4D\b|half[\s-]*year|interim (financial )?report|half yearly|financial report.*(31 dec|30 jun).*half|\bHY\d{2}\b|\b1H\b", re.I)
ANNUAL = re.compile(r"annual report|annual financial|\b4E\b|preliminary final|full[\s-]*year (statutory|financial)|financial (report|statements)", re.I)


def classify(title: str) -> str | None:
    """Return 'q', 'h', 'a' or None for a headline."""
    if EXCLUDE.search(title):
        return None
    if QUARTERLY.search(title):
        return "q"
    if HALF.search(title):
        return "h"
    if ANNUAL.search(title):
        return "a"
    return None


_TAIL = re.compile(r"\s*\d+\s+pages?\s+[\d.,]+\s*[KMG]B\s*$", re.I)


def parse_listing(html: str) -> list[dict]:
    """Extract (released, ids_id, title) for every announcement row on an archive page."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"idsId=(\d+)", a["href"])
        if not m or m.group(1) in seen:
            continue
        ids = m.group(1)
        title = _TAIL.sub("", " ".join(a.get_text(" ").split())).strip()
        row = a.find_parent("tr")
        date_txt = row.get_text(" ") if row else ""
        d = re.search(r"(\d{2})/(\d{2})/(\d{4})", date_txt)
        if not d:
            continue
        seen.add(ids)
        out.append({
            "ids_id": ids,
            "released": f"{d.group(3)}-{d.group(2)}-{d.group(1)}",
            "title": title,
        })
    return out


def list_announcements(code: str, year: int) -> list[dict]:
    r = _get(LIST_URL.format(code=code.upper(), year=year))
    return parse_listing(r.text)


def relevant(docs: list[dict]) -> list[dict]:
    """Keep filings that may hold a receipts line; drop plain activity reports when a 4C/5B shares the day."""
    keep = []
    for d in docs:
        k = classify(d["title"])
        if k:
            keep.append({**d, "kind": k})
    strong_days = {d["released"] for d in keep if d["kind"] == "q" and re.search(r"4C|5B|cash", d["title"], re.I)}
    out = []
    for d in keep:
        if d["kind"] == "q" and d["released"] in strong_days and not re.search(r"4C|5B|cash", d["title"], re.I):
            continue
        out.append(d)
    return out


# ---------------------------------------------------------------- download
_PDF_LINK = re.compile(r"""(?:name=["']pdfURL["'][^>]*value=["']([^"']+)["'])|(?:value=["']([^"']+\.pdf)["'])|"""
                       r"""(?:href=["']([^"']*asxpdf[^"']+\.pdf)["'])|(?:["'](/asxpdf/[^"']+\.pdf)["'])""", re.I)


def download_pdf(ids_id: str) -> bytes:
    """ASX shows a terms page first; the real PDF link sits in that page."""
    r = _get(DOC_URL.format(ids=ids_id))
    if r.content[:5] == b"%PDF-":
        return r.content
    m = _PDF_LINK.search(r.text)
    if not m:
        raise RuntimeError("could not find PDF link on ASX terms page")
    link = next(g for g in m.groups() if g)
    if link.startswith("/"):
        link = BASE + link
    r2 = _get(link, headers={"Referer": DOC_URL.format(ids=ids_id)})
    if r2.content[:5] != b"%PDF-":
        raise RuntimeError("ASX returned something other than a PDF")
    return r2.content


def doc_link(ids_id: str) -> str:
    return DOC_URL.format(ids=ids_id)
