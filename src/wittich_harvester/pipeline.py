"""Harvest pipeline: title -> issues -> table of contents -> manifest -> notices -> records.

Per title:
  1. /internal/neuste/ausgabe/{id} names the newest online issue ({} = none).
  2. Issues newer than the stored cursor (plus ``overlap_issues`` already-seen
     ones, for late corrections) are selected; a first run takes the newest
     ``initial_issues``.
  3. Each issue's table of contents is paged until the advertised article
     count is reached or a page comes back empty.
  4. One article page supplies the issue manifest: per-article date and
     section. Only articles classified as notices are fetched in full.
A failing issue never stops the title, and a failing title never stops the
run; both are recorded as errors and reflected in the run status.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from wittich_harvester.catalogue import CatalogueTitle
from wittich_harvester.config import Settings
from wittich_harvester.filters import NoticeDecision, classify
from wittich_harvester.geo import GeoResolver
from wittich_harvester.http import FetchError, PoliteClient, RobotsDisallowed
from wittich_harvester.load import Cursor, LoadCounts, LoadError, Store
from wittich_harvester.normalize import ErrorRecord, SignalRecord, date_flags, dedupe, headline_flags
from wittich_harvester.parsers import ParseError
from wittich_harvester.parsers.article import ArticlePage, ManifestItem, parse_article
from wittich_harvester.parsers.issue import ArticleStub, TocPage, parse_toc

log = logging.getLogger(__name__)

Issue = tuple[int, int]  # (issue_number, issue_year)
MANIFEST_ATTEMPTS = 3


def _order(issue: Issue) -> tuple[int, int]:
    return (issue[1], issue[0])


class IssueFailed(Exception):
    def __init__(self, error: ErrorRecord):
        super().__init__(error.message)
        self.error = error


@dataclass
class TitleHarvest:
    title: CatalogueTitle
    status: str = "ok"  # ok | partial | failed | no_online_edition
    newest_issue: Issue | None = None
    issues_selected: list[Issue] = field(default_factory=list)
    issues_processed: list[Issue] = field(default_factory=list)
    issues_failed: list[Issue] = field(default_factory=list)
    stale: bool = False
    articles_seen: int = 0
    notices: int = 0
    records: list[SignalRecord] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    cursor: Cursor | None = None
    counts: LoadCounts = field(default_factory=LoadCounts)

    def summary(self) -> dict:
        fmt = lambda i: f"{i[0]}/{i[1]}" if i else None  # noqa: E731
        return {
            "title_id": self.title.title_id,
            "name": self.title.overview_name or self.title.name,
            "state_code": self.title.state_code,
            "ars": self.title.ars,
            "status": self.status,
            "stale": self.stale,
            "newest_issue": fmt(self.newest_issue),
            "issues_processed": [fmt(i) for i in self.issues_processed],
            "issues_failed": [fmt(i) for i in self.issues_failed],
            "articles_seen": self.articles_seen,
            "notices": self.notices,
            "records": len(self.records),
            "errors": sum(1 for e in self.errors if e.severity == "error"),
            "warnings": sum(1 for e in self.errors if e.severity == "warning"),
            "inserted": self.counts.inserted,
            "updated": self.counts.updated,
            "unchanged": self.counts.unchanged,
        }


@dataclass
class RunResult:
    run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    titles: list[TitleHarvest] = field(default_factory=list)
    decisions: Counter = field(default_factory=Counter)
    tiers: Counter = field(default_factory=Counter)
    counts: LoadCounts = field(default_factory=LoadCounts)
    requests: int = 0
    loaded: bool = False

    @property
    def records(self) -> list[SignalRecord]:
        return [r for t in self.titles for r in t.records]

    @property
    def errors(self) -> list[ErrorRecord]:
        return [e for t in self.titles for e in t.errors]


class Harvester:
    def __init__(
        self,
        settings: Settings,
        client: PoliteClient,
        resolver: GeoResolver,
        store: Store | None = None,
        today: date | None = None,
    ):
        self.settings = settings
        self.client = client
        self.resolver = resolver
        self.store = store
        self.today = today or date.today()
        self.decisions: Counter = Counter()
        self.tiers: Counter = Counter()

    # -- URLs -----------------------------------------------------------------

    def newest_url(self, title_id: str) -> str:
        return f"{self.settings.base_url}/internal/neuste/ausgabe/{title_id}"

    def toc_url(self, title_id: str, issue: Issue, page: int) -> str:
        nr, year = issue
        return f"{self.settings.base_url}/titel/{title_id}/ausgabe/{nr}/{year}/rubrik/alle/seite/{page}/sortiert/inhaltsverzeichnis"

    # -- run ------------------------------------------------------------------

    def run(self, titles: list[CatalogueTitle], cursors: dict[str, Cursor] | None = None, run_id=None) -> RunResult:
        cursors = cursors or {}
        result = RunResult(run_id=run_id or uuid.uuid4(), started_at=datetime.now(timezone.utc))
        source = self.settings.source_system
        for title in titles:
            try:
                harvest = self.harvest_title(title, cursors.get(title.title_id))
            except Exception as exc:  # isolate the title; never lose the run
                harvest = TitleHarvest(title, status="failed")
                harvest.errors.append(
                    ErrorRecord(
                        "fetch",
                        "unexpected_exception",
                        f"{type(exc).__name__}: {exc}",
                        payload={"traceback": traceback.format_exc(limit=5)},
                    )
                )
                log.exception("title %s failed", title.title_id)
            harvest.records, duplicate_errors = dedupe(harvest.records)
            harvest.errors.extend(duplicate_errors)
            if self.store is not None:
                try:
                    harvest.counts = self.store.load_title(
                        result.run_id, source, title.title_id, harvest.records, harvest.errors, harvest.cursor
                    )
                except LoadError as exc:
                    harvest.status = "failed"
                    harvest.errors.append(ErrorRecord("load", "load_failed", str(exc)))
                    self.store.record_errors(result.run_id, source, harvest.errors)
                result.counts.add(harvest.counts)
            summary = harvest.summary()
            log.info(
                "title %s: %s, %d records, %d errors, %d warnings",
                title.title_id,
                harvest.status,
                summary["records"],
                summary["errors"],
                summary["warnings"],
            )
            result.titles.append(harvest)
        result.loaded = self.store is not None
        result.decisions, result.tiers = self.decisions, self.tiers
        result.requests = self.client.request_count
        result.finished_at = datetime.now(timezone.utc)
        result.status = run_status(result)
        return result

    # -- title ----------------------------------------------------------------

    def harvest_title(self, title: CatalogueTitle, cursor: Cursor | None) -> TitleHarvest:
        out = TitleHarvest(title)
        url = self.newest_url(title.title_id)
        try:
            data = json.loads(self.client.get(url).text or "{}")
        except FetchError as exc:
            return self._fail(out, ErrorRecord(_stage(exc), exc.code, str(exc), source_url=url))
        except json.JSONDecodeError as exc:
            return self._fail(out, ErrorRecord("parse", "malformed_json", f"newest-issue answer is not JSON: {exc}", source_url=url))
        if not data:
            out.status = "no_online_edition"
            return out
        try:
            newest: Issue = (int(data["issue"]), int(data["year"]))
        except (KeyError, TypeError, ValueError):
            return self._fail(out, ErrorRecord("parse", "unexpected_json", f"newest-issue answer lacks issue/year: {data!r}", source_url=url))
        if newest[0] == 0 or newest[1] == 1900:
            out.status = "no_online_edition"
            return out
        out.newest_issue = newest
        out.stale = newest[1] < self.today.year - self.settings.harvest.stale_after_years

        try:
            first_page = self._toc(title, newest, 0)
        except IssueFailed as exc:
            out.issues_failed.append(newest)
            return self._fail(out, exc.error)
        available = sorted(set(first_page.available_issues) | {newest}, key=_order, reverse=True)
        out.issues_selected = self.select_issues(available, cursor, out)

        for issue in out.issues_selected:
            try:
                records = self._issue(title, issue, first_page if issue == newest else None, out)
            except IssueFailed as exc:
                out.errors.append(exc.error)
                out.issues_failed.append(issue)
                continue
            out.records.extend(records)
            out.issues_processed.append(issue)

        # The cursor advances through consecutive successes, oldest first, so a
        # failed issue is retried by the next run instead of being skipped.
        for issue in sorted(out.issues_selected, key=_order):
            if issue in out.issues_failed:
                break
            if cursor is None or _order(issue) > cursor.key:
                dates = [r.published_on for r in out.records if r.issue_number == issue[0] and r.issue_year == issue[1] and r.published_on]
                cursor = Cursor(issue[1], issue[0], max(dates) if dates else None)
                out.cursor = cursor

        if not out.issues_processed:
            out.status = "failed"
        elif out.issues_failed or any(e.severity == "error" for e in out.errors):
            out.status = "partial"
        return out

    def select_issues(self, available: list[Issue], cursor: Cursor | None, out: TitleHarvest) -> list[Issue]:
        """``available`` is newest first."""
        harvest = self.settings.harvest
        if cursor is None:
            return available[: harvest.initial_issues]
        newer = [i for i in available if _order(i) > cursor.key]
        seen = [i for i in available if _order(i) <= cursor.key][: harvest.overlap_issues]
        chosen = newer + seen
        if len(newer) > harvest.max_issues_per_title:
            out.errors.append(
                ErrorRecord(
                    "validate",
                    "issue_backlog_capped",
                    f"{len(newer)} new issues since {cursor.issue_number}/{cursor.issue_year}; "
                    f"only the newest {harvest.max_issues_per_title} are read",
                    severity="warning",
                )
            )
        return chosen[: harvest.max_issues_per_title]

    # -- issue ----------------------------------------------------------------

    def _issue(self, title: CatalogueTitle, issue: Issue, first_page: TocPage | None, out: TitleHarvest) -> list[SignalRecord]:
        first_page = first_page or self._toc(title, issue, 0)
        issue_url = self.toc_url(title.title_id, issue, 0)
        total = first_page.article_count or 0
        stubs: dict[str, ArticleStub] = {}
        repeats = self._add_stubs(stubs, first_page.stubs)
        listed = len(first_page.stubs)
        page = 1
        # Page until the advertised count is listed (entries, not unique ids:
        # the source sometimes lists one article twice) or a page is empty.
        while listed < total and page < self.settings.harvest.max_toc_pages:
            try:
                toc = self._toc(title, issue, page)
            except IssueFailed as exc:
                out.errors.append(exc.error)
                break
            if not toc.stubs:
                break
            repeats += self._add_stubs(stubs, toc.stubs)
            listed += len(toc.stubs)
            page += 1
        if repeats:
            out.errors.append(
                ErrorRecord(
                    "validate",
                    "toc_duplicate_entries",
                    f"issue {issue[0]}/{issue[1]} lists {repeats} article(s) more than once; kept one each",
                    "warning",
                    issue_url,
                )
            )
        if listed != total:
            out.errors.append(
                ErrorRecord(
                    "validate",
                    "toc_count_mismatch",
                    f"issue {issue[0]}/{issue[1]} advertises {total} articles, {listed} were listed",
                    "warning",
                    issue_url,
                )
            )
        if not stubs:
            out.errors.append(ErrorRecord("validate", "empty_issue", f"issue {issue[0]}/{issue[1]} lists no articles", "warning", issue_url))
            return []
        out.articles_seen += len(stubs)

        # Every article page embeds the issue manifest. Read it from the
        # entries most likely to be notices: their pages are needed anyway.
        articles: dict[str, ArticlePage] = {}
        manifest: dict[str, ManifestItem] = {}
        likely = lambda s: classify("", s.headline, s.teaser, self.settings.filter).is_notice  # noqa: E731
        candidates = sorted(stubs.values(), key=lambda s: (not likely(s), s.position))[:MANIFEST_ATTEMPTS]
        for stub in candidates:
            page_ = self._article(stub, out)
            if page_ is None:
                continue
            articles[stub.article_id] = page_
            for problem in page_.manifest_errors:
                out.errors.append(ErrorRecord("parse", "manifest_bad_date", problem, "warning", page_.url))
            if page_.manifest:
                manifest = {m.article_id: m for m in page_.manifest}
                break
        if not manifest:
            out.errors.append(
                ErrorRecord(
                    "parse",
                    "manifest_missing",
                    f"no issue manifest for {issue[0]}/{issue[1]}: sections and dates unknown",
                    source_url=issue_url,
                )
            )
        dates = Counter(m.published_on for m in manifest.values() if m.published_on)
        modal_date = dates.most_common(1)[0][0] if dates else None

        records = []
        for stub in stubs.values():
            item = manifest.get(stub.article_id)
            if manifest and item is None:
                out.errors.append(
                    ErrorRecord("validate", "not_in_manifest", "listed article missing from the issue manifest", "warning",
                                self._article_url(stub), stub.article_id)
                )
            decision = classify(item.rubric if item else "", stub.headline, stub.teaser, self.settings.filter)
            self.decisions[decision.reason] += 1
            self.tiers[f"{decision.tier}:{'notice' if decision.is_notice else 'skipped'}"] += 1
            if not decision.is_notice:
                continue
            out.notices += 1
            article = articles.get(stub.article_id) or self._article(stub, out)
            record = self._record(title, issue, issue_url, stub, item, article, decision, modal_date, out)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _add_stubs(stubs: dict[str, ArticleStub], new: list[ArticleStub]) -> int:
        repeats = 0
        for stub in new:
            if stub.article_id in stubs:
                repeats += 1
            else:
                stubs[stub.article_id] = stub
        return repeats

    # -- pages ----------------------------------------------------------------

    def _toc(self, title: CatalogueTitle, issue: Issue, page: int) -> TocPage:
        url = self.toc_url(title.title_id, issue, page)
        try:
            toc = parse_toc(self.client.get(url).text, url)
        except FetchError as exc:
            raise IssueFailed(ErrorRecord(_stage(exc), exc.code, str(exc), source_url=url)) from exc
        except ParseError as exc:
            raise IssueFailed(ErrorRecord("parse", exc.code, str(exc), source_url=url)) from exc
        if (toc.issue_number, toc.issue_year) != issue and toc.has_online_edition:
            raise IssueFailed(
                ErrorRecord(
                    "validate",
                    "toc_issue_mismatch",
                    f"asked for issue {issue[0]}/{issue[1]}, got {toc.issue_number}/{toc.issue_year}",
                    source_url=url,
                )
            )
        if not toc.has_online_edition:
            raise IssueFailed(
                ErrorRecord("validate", "issue_not_online", f"issue {issue[0]}/{issue[1]} has no online edition", "warning", url)
            )
        return toc

    def _article_url(self, stub: ArticleStub) -> str:
        return f"{self.settings.base_url}{stub.path}"

    def _article(self, stub: ArticleStub, out: TitleHarvest) -> ArticlePage | None:
        url = self._article_url(stub)
        try:
            page = parse_article(self.client.get(url).text, url)
        except FetchError as exc:
            out.errors.append(ErrorRecord(_stage(exc), exc.code, str(exc), source_url=url, source_record_id=stub.article_id))
            return None
        except ParseError as exc:
            out.errors.append(ErrorRecord("parse", exc.code, str(exc), source_url=url, source_record_id=stub.article_id))
            return None
        if page.article_id and page.article_id != stub.article_id:
            out.errors.append(
                ErrorRecord("validate", "article_id_mismatch", f"page reports {page.article_id}", "warning", url, stub.article_id)
            )
        return page

    # -- record ---------------------------------------------------------------

    def _record(
        self,
        title: CatalogueTitle,
        issue: Issue,
        issue_url: str,
        stub: ArticleStub,
        item: ManifestItem | None,
        article: ArticlePage | None,
        decision: NoticeDecision,
        modal_date: date | None,
        out: TitleHarvest,
    ) -> SignalRecord | None:
        url = self._article_url(stub)
        headline = (article.headline if article else "") or stub.headline or (item.caption if item else "")
        if not headline:
            out.errors.append(ErrorRecord("validate", "missing_title", "notice has no headline in any source", source_url=url, source_record_id=stub.article_id))
            return None
        section = (item.rubric if item else "") or (article.section if article else "")
        published_on = item.published_on if item else None

        flags: list[str] = []
        if not section:
            flags.append("missing_section")
        flags += date_flags(published_on, modal_date, issue[1])
        if article is None:
            flags.append("missing_body")
        elif not article.body_text:
            flags.append("empty_body")
        flags += headline_flags(stub.headline, item.caption if item else None, article.headline if article else None)

        geo = self.resolver.resolve(title.state_code, title.ars, section, stub.place_label)
        if geo.status != "resolved":
            flags.append(f"geo_{geo.status}")
        if geo.status in ("invalid", "ambiguous", "unresolved"):
            out.errors.append(
                ErrorRecord(
                    "geo",
                    "invalid_coordinates" if geo.status == "invalid" else f"location_{geo.status}",
                    geo.note or geo.status,
                    "warning",
                    url,
                    stub.article_id,
                    {"place_text": geo.place_text, "candidates": geo.candidates} if geo.candidates or geo.place_text else None,
                )
            )

        record = SignalRecord(
            source_system=self.settings.source_system,
            source_record_id=stub.article_id,
            source_url=url,
            signal_type=self.settings.filter.signal_type,
            notice_category=decision.category,
            title=headline,
            summary=stub.teaser or None,
            body_text=(article.body_text or None) if article else None,
            published_on=published_on,
            section=section or None,
            publication_id=title.title_id,
            publication_title=(article.publication_title if article else "") or title.overview_name or title.name,
            issue_number=issue[0],
            issue_year=issue[1],
            issue_url=issue_url,
            state_code=title.state_code,
            ars=geo.ars or title.ars,
            municipality_name=geo.municipality_name,
            place_text=geo.place_text or stub.place_label or None,
            x=geo.x,
            y=geo.y,
            geo_precision=geo.precision,
            geo_status=geo.status,
            geo_note=geo.note,
            quality_flags=sorted(set(flags)),
            raw={
                "toc": {"headline": stub.headline, "teaser": stub.teaser, "place_label": stub.place_label, "position": stub.position},
                "manifest": {"date": item.raw_date, "caption": item.caption, "rubric": item.rubric} if item else None,
                "article": (
                    {"headline": article.headline, "section": article.section, "publication_title": article.publication_title, "issue_label": article.issue_label}
                    if article
                    else None
                ),
                "decision": {"tier": decision.tier, "reason": decision.reason},
                "title": {"id": title.title_id, "name": title.name, "ars": title.ars, "frequency": title.frequency},
                "geo": {"note": geo.note, "candidates": geo.candidates},
            },
        )
        record.compute_hash()
        return record

    def _fail(self, out: TitleHarvest, error: ErrorRecord) -> TitleHarvest:
        out.errors.append(error)
        out.status = "failed"
        return out


def _stage(exc: FetchError) -> str:
    return "robots" if isinstance(exc, RobotsDisallowed) else "fetch"


def run_status(result: RunResult) -> str:
    if not result.titles:
        return "failed"  # nothing to harvest: discovery failed or the filter matched nothing
    harvested = [t for t in result.titles if t.status != "no_online_edition"]
    if harvested and all(t.status == "failed" for t in harvested):
        return "failed"
    if any(t.status in ("failed", "partial") for t in result.titles):
        return "partial"
    return "succeeded"
