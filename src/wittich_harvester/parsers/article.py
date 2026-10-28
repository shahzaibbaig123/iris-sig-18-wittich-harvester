"""Article pages: /titel/{id}/ausgabe/{nr}/{year}/artikel/{articleId}

Besides the article itself, every article page embeds a JavaScript manifest
of the whole issue (``article_list.push({id, date, caption, rubric})``). That
manifest is the only place the source states a per-article publication date
and section, so it is parsed here as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import Comment

from wittich_harvester.parsers import ParseError, soup
from wittich_harvester.text import clean

_ISSUE_LABEL = re.compile(r"Ausgabe\s+(\d+)/(\d{4})")
_MANIFEST_ITEM = re.compile(
    r"article_list\.push\(\{\s*"
    r"id:\s*'(?P<id>(?:[^'\\]|\\.)*)',\s*"
    r"date:\s*'(?P<date>(?:[^'\\]|\\.)*)',\s*"
    r"caption:\s*'(?P<caption>(?:[^'\\]|\\.)*)',\s*"
    r"rubric:\s*'(?P<rubric>(?:[^'\\]|\\.)*)'\s*"
    r"\}\)"
)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class ManifestItem:
    article_id: str
    published_on: date | None
    raw_date: str
    caption: str
    rubric: str


@dataclass
class ArticlePage:
    article_id: str | None
    url: str
    headline: str
    section: str
    body_text: str
    publication_title: str
    issue_label: str
    manifest: list[ManifestItem] = field(default_factory=list)
    manifest_errors: list[str] = field(default_factory=list)


def parse_article(html: str, url: str) -> ArticlePage:
    doc = soup(html)
    content = doc.find(id="container-content-left")
    if content is None:
        raise ParseError("unexpected_markup", "article has no #container-content-left")

    canonical = url
    if (meta := doc.find("meta", property="og:url")) and meta.get("content"):
        canonical = meta["content"]
    section = ""
    if (meta := doc.find("meta", property="article:section")) and meta.get("content"):
        section = clean(meta["content"])

    headline = _after_comment(content, "header", "h1")
    body = _after_comment(content, "main text", "div")
    if headline is None:
        raise ParseError("missing_headline", "article has no <h1> after the header marker")

    publication_title = issue_label = ""
    for row in content.select(".invisible-on-small-display .row"):
        text = clean(row.get_text(" "))
        if not publication_title and row.find(class_="font-bold"):
            publication_title = text
        elif not issue_label and (m := _ISSUE_LABEL.search(text)):
            issue_label = m.group(0)
            if not section:
                section = clean(text.replace(issue_label, ""))

    page = ArticlePage(
        article_id=canonical.rstrip("/").rsplit("/", 1)[-1] if "/artikel/" in canonical else None,
        url=canonical,
        headline=clean(headline.get_text(" ")),
        section=section,
        body_text=_body_text(body),
        publication_title=publication_title,
        issue_label=issue_label,
    )
    page.manifest, page.manifest_errors = parse_manifest(html)
    return page


def parse_manifest(html: str) -> tuple[list[ManifestItem], list[str]]:
    items: list[ManifestItem] = []
    errors: list[str] = []
    for match in _MANIFEST_ITEM.finditer(html):
        raw_date = match["date"]
        published = None
        if iso := _ISO_DATE.match(raw_date):
            try:
                published = date(int(iso[1]), int(iso[2]), int(iso[3]))
            except ValueError:
                errors.append(f"invalid date {raw_date!r} for {match['id']}")
        else:
            errors.append(f"unparseable date {raw_date!r} for {match['id']}")
        items.append(
            ManifestItem(
                article_id=_unescape_js(match["id"]),
                published_on=published,
                raw_date=raw_date,
                caption=clean(_unescape_js(match["caption"])),
                rubric=clean(_unescape_js(match["rubric"])),
            )
        )
    return items, errors


def _after_comment(container, marker: str, tag: str):
    """First ``tag`` after the ``<!-- marker -->`` comment the templates emit."""
    for comment in container.find_all(string=lambda s: isinstance(s, Comment)):
        if comment.strip() == marker:
            return comment.find_next(tag)
    return None


def _body_text(node) -> str:
    if node is None:
        return ""
    for tag in node.find_all(["script", "style"]):
        tag.decompose()
    for br in node.find_all("br"):
        br.replace_with("\n")
    blocks = [clean(el.get_text(" ")) for el in node.find_all(["p", "h2", "h3", "h4", "h5", "li", "tr"])]
    blocks = [b for b in blocks if b]
    if not blocks:
        return clean(node.get_text(" "))
    # Nested blocks (a <p> inside a <td> inside a <tr>) repeat text; keep the
    # outermost occurrence only.
    out: list[str] = []
    for block in blocks:
        if out and block in out[-1]:
            continue
        out.append(block)
    return "\n".join(out)


def _unescape_js(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)
