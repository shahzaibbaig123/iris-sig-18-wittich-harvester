"""E-paper route (pattern B, PDF) for titles without an online edition.

The e-paper viewer page (/frontend/mvc/catalog/by-name/{id}/newest) carries
a config object with the issue id ("1391_22_2026"), the complete-issue PDF
URL and the publisher's switch ``save_complete_enabled``. The PDF is used
only when that switch is on and the catalogue is not a ``/preview/`` one:
those are the cases where every reader is offered the download.

The PDF has no article markup, so notices are segmented from typography:
in the editorial font family, larger bold lines are section headings or
headlines, and body-size lines are text. Advertisements are set in other
families and are skipped.
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from wittich_harvester.parsers import ParseError
from wittich_harvester.text import clean

logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding chatter on ad fonts

_ISSUE_TITLE = re.compile(r"<title>\s*(\d+)_(\d+)_(\d{4})\s*</title>")
_FLAG = re.compile(r"(\w+_enabled)\s*:\s*'?(true|false)'?")
_URL = re.compile(r"(\w+Url)\s*:\s*'([^']*)'")
_MONTHS = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "dezember"]
_MASTHEAD_DATE = re.compile(
    r"(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag),?\s+(?:den\s+)?(\d{1,2})\.\s*(" + "|".join(_MONTHS) + r")\s+(\d{4})",
    re.IGNORECASE,
)
_RUNNING_HEADER = re.compile(r"^.{0,60}[–-]\s*\d+\s*[–-]\s*(?:Nr\.|Ausgabe)\s*\d+/\d{4}\s*$")
_FONT_STEM = re.compile(r"(MT|PS|LTStd|LT|Std|Pro)$")
PAGE_BODY_MIN_CHARS = 400


@dataclass(frozen=True)
class EpaperViewer:
    title_id: str
    issue_number: int
    issue_year: int
    complete_pdf_path: str | None
    save_complete_enabled: bool
    preview: bool

    @property
    def downloadable(self) -> bool:
        return self.save_complete_enabled and not self.preview and bool(self.complete_pdf_path)


def parse_viewer(html: str) -> EpaperViewer:
    if not html or not html.strip():
        raise ParseError("empty_body", "e-paper viewer page is empty")
    issue = _ISSUE_TITLE.search(html)
    if issue is None:
        raise ParseError("unexpected_markup", "e-paper viewer has no '<id>_<issue>_<year>' title")
    flags = {k: v == "true" for k, v in _FLAG.findall(html)}
    urls = {k: v.replace("\\/", "/") for k, v in _URL.findall(html)}
    path = urls.get("saveCompleteUrl")
    return EpaperViewer(
        title_id=issue.group(1),
        issue_number=int(issue.group(2)),
        issue_year=int(issue.group(3)),
        complete_pdf_path=path,
        save_complete_enabled=flags.get("save_complete_enabled", False),
        preview="/preview/" in (path or ""),
    )


@dataclass
class PdfBlock:
    page: int  # 1-based
    section: str
    headline: str
    body: str = ""


@dataclass
class PdfIssue:
    pages: int
    issue_date: date | None
    masthead: str
    document_title: str
    blocks: list[PdfBlock] = field(default_factory=list)


def _stem(font: str) -> str:
    name = font.lstrip("/").split("+")[-1]
    return _FONT_STEM.sub("", re.split(r"[-,]", name)[0])


def _fragments(page) -> list[tuple[float, str, str]]:
    out: list[tuple[float, str, str]] = []

    def visit(text, cm, tm, font, size):
        scale = abs((tm[3] if tm else 1) * (cm[3] if cm else 1))
        base = font.get("/BaseFont", "") if hasattr(font, "get") else ""
        out.append((round(abs(size) * scale, 1), str(base), text))

    page.extract_text(visitor_text=visit)
    return out


def parse_pdf_issue(data: bytes, headline_ratio: float = 1.2, section_ratio: float = 1.6) -> PdfIssue:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not data.startswith(b"%PDF"):
        raise ParseError("not_a_pdf", "download is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [_fragments(page) for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ParseError("malformed_pdf", f"PDF could not be read: {exc}") from exc
    if not pages:
        raise ParseError("empty_pdf", "PDF has no pages")

    # The editorial body style is the font family and size that carry most text.
    weight: Counter = Counter()
    for frags in pages:
        for size, font, text in frags:
            weight[(_stem(font), size)] += len(text.strip())
    if not weight:
        raise ParseError("no_text_layer", "PDF has no extractable text")
    (family, body_size), _ = weight.most_common(1)[0]

    first_text = clean(" ".join(t for _, _, t in pages[0]))
    masthead = _MASTHEAD_DATE.search(first_text)
    issue_date = None
    if masthead:
        day, month, year = int(masthead.group(1)), _MONTHS.index(masthead.group(2).lower()) + 1, int(masthead.group(3))
        try:
            issue_date = date(year, month, day)
        except ValueError:
            issue_date = None
    metadata = reader.metadata or {}
    issue = PdfIssue(
        pages=len(pages),
        issue_date=issue_date,
        masthead=masthead.group(0) if masthead else "",
        document_title=clean(str(metadata.get("/Title", ""))),
    )

    section = ""
    block: PdfBlock | None = None
    pending_headline: list[str] = []

    def flush_headline(page_no: int) -> None:
        nonlocal block
        if pending_headline:
            block = PdfBlock(page=page_no, section=section, headline=clean(" ".join(pending_headline)))
            issue.blocks.append(block)
            pending_headline.clear()

    for page_no, frags in enumerate(pages, start=1):
        # Some pages set their text larger (e.g. 11 pt instead of 9 pt): judge
        # headlines against the page's own body size when it has enough text.
        page_weight: Counter = Counter()
        for size, font, text in frags:
            if _stem(font) == family:
                page_weight[size] += len(text.strip())
        page_body = body_size
        if page_weight and sum(page_weight.values()) >= PAGE_BODY_MIN_CHARS:
            page_body = page_weight.most_common(1)[0][0]
        for size, font, text in frags:
            if not text.strip():
                if "\n" in text and block is not None and not pending_headline:
                    block.body += "\n"  # keep line breaks: running headers are dropped per line
                continue
            if _stem(font) != family:
                continue
            if size >= page_body * section_ratio:
                flush_headline(page_no)
                section = clean(text)
                block = None
            elif size >= page_body * headline_ratio:
                pending_headline.append(text)
            elif size >= page_body * 0.8:
                flush_headline(page_no)
                if block is not None:
                    block.body += text if text.endswith("\n") else text + "\n"
        flush_headline(page_no)
    for b in issue.blocks:
        # Hyphenation arrives as a separate "-" line: "Betriebsabrech / - / nung".
        body = re.sub(r"(\w)\s*\n\s*-\s*\n\s*(\w)", r"\1\2", b.body)
        lines = [clean(line) for line in body.splitlines()]
        b.body = "\n".join(line for line in lines if line and not _RUNNING_HEADER.match(line))
    return issue
