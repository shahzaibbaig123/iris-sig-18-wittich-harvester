"""Title catalogue pages: postcode search results and /uebersicht.

Search results (/internal/typ/ZIP/begriff/{prefix}) are HTML fragments that
render every title twice: a mobile block that carries the ARS
(``data-regiocode``) and a desktop block that carries the ``<h2>`` name.
Both are merged by ``data-id``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from wittich_harvester.parsers import ParseError, soup
from wittich_harvester.text import clean

# "Jockgrim - Rheinland-Pfalz": split on the last spaced dash, since state
# names contain unspaced ones.
_STATE_LABEL = re.compile(r"^(?P<place>.*)\s+-\s+(?P<state>\S.*)$")
_FREQUENCY = re.compile(r"Erscheinungsweise:\s*(?P<freq>.*)$", re.IGNORECASE)
_TITLE_HREF = re.compile(r"^/titel/(\d+)$")


@dataclass
class SearchTitle:
    title_id: str
    name: str = ""
    place_label: str = ""
    state_name: str = ""
    frequency: str = ""
    regiocodes: list[str] = field(default_factory=list)
    external_url: str | None = None


@dataclass(frozen=True)
class OverviewTitle:
    title_id: str
    name: str
    state_name: str
    postcode: str


def parse_search_results(html: str) -> list[SearchTitle]:
    """Return the titles in a search fragment; an empty fragment means no hits."""
    if not html or not html.strip():
        return []
    doc = BeautifulSoup(html, "lxml")
    blocks = doc.find_all(attrs={"data-role": "title-block"})
    if not blocks:
        # The endpoint answers with a bare fragment. A full page (or any other
        # markup) without title blocks means the endpoint changed.
        if doc.find(["nav", "form", "title"]) or doc.get_text(strip=True):
            raise ParseError("unexpected_markup", "search response has no title blocks")
        return []
    titles: dict[str, SearchTitle] = {}
    for block in blocks:
        title_id = (block.get("data-id") or "").strip()
        if not title_id.isdigit():
            continue
        entry = titles.setdefault(title_id, SearchTitle(title_id=title_id))
        if not entry.name and (h2 := block.find("h2")):
            entry.name = clean(h2.get_text())
        for div in block.find_all("div", class_="font-bold"):
            match = _STATE_LABEL.match(clean(div.get_text()))
            if match:
                entry.place_label = entry.place_label or match["place"]
                entry.state_name = entry.state_name or match["state"].strip()
        for div in block.find_all("div", class_="size-font-first-decrement"):
            match = _FREQUENCY.search(clean(div.get_text()))
            if match:
                entry.frequency = entry.frequency or match["freq"]
        for node in block.find_all(attrs={"data-role": "title-search-localbook"}):
            code = (node.get("data-regiocode") or "").strip()
            if code.isdigit() and code not in entry.regiocodes:
                entry.regiocodes.append(code)
        for node in block.find_all(attrs={"data-role": "title-search-external-site"}):
            entry.external_url = entry.external_url or (node.get("data-url") or None)
    for entry in titles.values():
        entry.name = entry.name or entry.place_label
    return list(titles.values())


def parse_overview(html: str) -> list[OverviewTitle]:
    """Titles grouped by state ("Alle Titel nach Bundesland geordnet")."""
    doc = soup(html)
    container = doc.find(id="title-list-by-state")
    if container is None:
        raise ParseError("unexpected_markup", "overview has no #title-list-by-state")
    results: list[OverviewTitle] = []
    state = postcode = ""
    for node in container.find_all(["h2", "h4", "a"]):
        if node.name == "h2":
            state = clean(node.get_text())
        elif node.name == "h4":
            postcode = clean(node.get_text())
        elif (match := _TITLE_HREF.match(node.get("href") or "")) and state:
            results.append(OverviewTitle(match.group(1), clean(node.get_text()), state, postcode))
    if not results:
        raise ParseError("no_titles", "overview lists no titles")
    return results
