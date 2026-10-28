"""Issue table of contents: /titel/{id}/ausgabe/{nr}/{year}/rubrik/alle/seite/{n}/sortiert/inhaltsverzeichnis

Twenty articles per page, paginated server-side. A title without an online
edition redirects to ``/ausgabe/0/1900`` and renders no article count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wittich_harvester.parsers import ParseError, soup
from wittich_harvester.text import clean

_COUNT = re.compile(r"(\d+)\s+Artikel in dieser Ausgabe")
_TOC_URL = re.compile(r"/titel/(?P<title>\d+)/ausgabe/(?P<nr>\d+)/(?P<year>\d{4})/rubrik/[^/]+/seite/(?P<page>\d+)/")
_ARTICLE_HREF = re.compile(r"^/titel/(?P<title>\d+)/ausgabe/(?P<nr>\d+)/(?P<year>\d{4})/artikel/(?P<id>[^/?#]+)$")


@dataclass(frozen=True)
class ArticleStub:
    article_id: str
    path: str
    place_label: str
    headline: str
    teaser: str
    position: int  # 0-based position within the issue (table-of-contents order)


@dataclass
class TocPage:
    title_id: str | None
    issue_number: int | None
    issue_year: int | None
    page: int | None
    article_count: int | None
    stubs: list[ArticleStub] = field(default_factory=list)
    # (issue_number, issue_year), newest first, from the issue selector.
    available_issues: list[tuple[int, int]] = field(default_factory=list)
    rubrics: list[str] = field(default_factory=list)

    @property
    def has_online_edition(self) -> bool:
        return self.issue_year not in (None, 1900) and self.article_count is not None


def parse_toc(html: str, url: str, per_page: int = 20) -> TocPage:
    doc = soup(html)
    canonical = ""
    if (meta := doc.find("meta", property="og:url")) and meta.get("content"):
        canonical = meta["content"]
    match = _TOC_URL.search(canonical) or _TOC_URL.search(url)
    toc = TocPage(
        title_id=match["title"] if match else None,
        issue_number=int(match["nr"]) if match else None,
        issue_year=int(match["year"]) if match else None,
        page=int(match["page"]) if match else None,
        article_count=None,
    )
    if toc.issue_year == 1900:
        return toc  # no online edition: an empty, well-formed answer

    if doc.find(id="container-content-left") is None:
        raise ParseError("unexpected_markup", "table of contents has no #container-content-left")
    if count := _COUNT.search(doc.get_text(" ")):
        toc.article_count = int(count.group(1))
    else:
        raise ParseError("missing_article_count", "no 'N Artikel in dieser Ausgabe' marker")

    if select := doc.find("select", id="nav-selector-issue"):
        for option in select.find_all("option"):
            nr, _, year = (option.get("value") or "").partition(",")
            if nr.isdigit() and year.isdigit():
                toc.available_issues.append((int(nr), int(year)))
    if select := doc.find("select", id="nav-selector-filter"):
        toc.rubrics = [clean(o.get_text()) for o in select.find_all("option") if o.get("value") != "alle"]

    offset = (toc.page or 0) * per_page
    article_list = doc.find(id="article-list")
    for anchor in article_list.find_all("a", href=True) if article_list else []:
        href = _ARTICLE_HREF.match(anchor["href"])
        if not href:
            continue
        block = anchor.find(attrs={"data-role": "article-list-block"})
        if block is None:
            continue
        headline = block.find(attrs={"data-role": "headline"})
        toc.stubs.append(
            ArticleStub(
                article_id=href["id"],
                path=anchor["href"],
                place_label=clean(_text(block.find(attrs={"data-role": "gemeinde"}))),
                headline=clean(_text(headline)),
                teaser=clean(_text(block.find(attrs={"data-role": "teaser"}))).rstrip("…").strip(),
                position=offset + len(toc.stubs),
            )
        )
    return toc


def _text(node) -> str:
    return node.get_text(" ") if node is not None else ""
