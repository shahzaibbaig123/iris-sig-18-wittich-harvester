"""Title discovery: which gazettes exist, in which state, for which area.

1. Postcode sweep over /internal/typ/ZIP/begriff/{prefix}. A result page caps
   at about 50 titles, so a saturated prefix is narrowed by one more digit.
2. /uebersicht lists TH and SN titles (not RP or HE). It adds titles the
   sweep missed (their ARS is then looked up by exact postcode) and gives a
   completeness check for the report.
Only path-style URLs are used; www.wittich.de forbids query strings for
crawlers, and these endpoints need none.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wittich_harvester.config import STATE_NAMES, Settings
from wittich_harvester.filters import title_scope
from wittich_harvester.http import FetchError, PoliteClient
from wittich_harvester.normalize import ErrorRecord
from wittich_harvester.parsers import ParseError
from wittich_harvester.parsers.catalogue import SearchTitle, parse_overview, parse_search_results

log = logging.getLogger(__name__)


@dataclass
class CatalogueTitle:
    title_id: str
    name: str
    state_code: str | None
    state_name: str
    frequency: str
    regiocodes: list[str]
    sources: list[str]
    in_scope: bool
    reason: str
    overview_name: str | None = None
    postcode: str | None = None

    @property
    def ars(self) -> str | None:
        """The title's area. All observed titles carry exactly one code; with
        several, their shared prefix is cut back to a valid ARS length."""
        if not self.regiocodes:
            return None
        common = self.regiocodes[0]
        for code in self.regiocodes[1:]:
            while not code.startswith(common):
                common = common[:-1]
        for length in (12, 9, 5, 2):
            if len(common) >= length:
                return common[:length]
        return None


@dataclass
class Catalogue:
    created_at: str
    titles: list[CatalogueTitle]
    prefixes_queried: list[str] = field(default_factory=list)
    saturated_prefixes: list[str] = field(default_factory=list)
    overview_in_scope: int = 0
    overview_found_by_sweep: int = 0
    errors: list[ErrorRecord] = field(default_factory=list)

    def in_scope(self) -> list[CatalogueTitle]:
        return [t for t in self.titles if t.in_scope]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Catalogue":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["titles"] = [CatalogueTitle(**t) for t in data["titles"]]
        data["errors"] = [ErrorRecord(**e) for e in data.get("errors", [])]
        return cls(**data)

    def age_days(self, now: datetime) -> float:
        return (now - datetime.fromisoformat(self.created_at)).total_seconds() / 86400


def discover(client: PoliteClient, settings: Settings, prefixes: list[str] | None = None) -> Catalogue:
    disc = settings.discovery
    base = settings.base_url
    found: dict[str, SearchTitle] = {}
    sources: dict[str, set[str]] = {}
    errors: list[ErrorRecord] = []
    queried: list[str] = []
    saturated: list[str] = []

    def search(term: str) -> list[SearchTitle] | None:
        url = f"{base}/internal/typ/ZIP/begriff/{term}"
        try:
            results = parse_search_results(client.get(url).text)
        except FetchError as exc:
            errors.append(ErrorRecord("fetch", exc.code, str(exc), source_url=url))
            return None
        except ParseError as exc:
            errors.append(ErrorRecord("parse", exc.code, str(exc), source_url=url))
            return None
        for title in results:
            _merge(found.setdefault(title.title_id, SearchTitle(title.title_id)), title)
            sources.setdefault(title.title_id, set()).add("search")
        return results

    queue = deque(prefixes if prefixes is not None else disc.zip_prefixes)
    while queue:
        prefix = queue.popleft()
        queried.append(prefix)
        results = search(prefix)
        if results is not None and len(results) >= disc.saturation_threshold:
            if len(prefix) < disc.max_prefix_length:
                saturated.append(prefix)
                queue.extend(prefix + digit for digit in "0123456789")
            else:
                errors.append(
                    ErrorRecord(
                        "validate",
                        "search_saturated",
                        f"prefix {prefix} returned {len(results)} titles at the maximum prefix length; "
                        "titles may be missing",
                        severity="warning",
                    )
                )

    overview: dict[str, tuple[str, str, str]] = {}
    overview_in_scope = overview_found = 0
    if disc.use_overview:
        url = f"{base}/uebersicht"
        try:
            entries = parse_overview(client.get(url).text)
        except (FetchError, ParseError) as exc:
            stage = "fetch" if isinstance(exc, FetchError) else "parse"
            errors.append(ErrorRecord(stage, exc.code, str(exc), source_url=url))
            entries = []
        wanted = {STATE_NAMES[s] for s in settings.states}
        for entry in entries:
            if entry.state_name not in wanted:
                continue
            overview_in_scope += 1
            overview[entry.title_id] = (entry.name, entry.state_name, entry.postcode)
            if entry.title_id in found:
                overview_found += 1
                continue
            # Exact postcode lookup returns the missing title with its ARS.
            if prefixes is None and entry.postcode:
                search(entry.postcode)
            if entry.title_id not in found:
                found[entry.title_id] = SearchTitle(entry.title_id, name=entry.name, state_name=entry.state_name)
            sources.setdefault(entry.title_id, set()).add("overview")

    titles = []
    for title_id, title in sorted(found.items(), key=lambda kv: int(kv[0])):
        decision = title_scope(
            title.name, title.state_name, title.frequency, title.regiocodes, settings.states, disc
        )
        ov = overview.get(title_id)
        titles.append(
            CatalogueTitle(
                title_id=title_id,
                name=title.name,
                state_code=decision.state_code,
                state_name=title.state_name,
                frequency=title.frequency,
                regiocodes=title.regiocodes,
                sources=sorted(sources.get(title_id, ())),
                in_scope=decision.in_scope,
                reason=decision.reason,
                overview_name=ov[0] if ov else None,
                postcode=ov[2] if ov else None,
            )
        )
    return Catalogue(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        titles=titles,
        prefixes_queried=queried,
        saturated_prefixes=saturated,
        overview_in_scope=overview_in_scope,
        overview_found_by_sweep=overview_found,
        errors=errors,
    )


def _merge(target: SearchTitle, other: SearchTitle) -> None:
    target.name = target.name or other.name
    target.place_label = target.place_label or other.place_label
    target.state_name = target.state_name or other.state_name
    target.frequency = target.frequency or other.frequency
    target.external_url = target.external_url or other.external_url
    for code in other.regiocodes:
        if code not in target.regiocodes:
            target.regiocodes.append(code)
