"""End-to-end over the fixtures, without a database."""

import dataclasses
from collections import Counter

from conftest import page
from wittich_harvester.load import Cursor
from wittich_harvester.pipeline import Harvester, TitleHarvest
from wittich_harvester.transport import FixtureRoute

TOC = "/titel/{t}/ausgabe/{nr}/2026/rubrik/alle/seite/{p}/sortiert/inhaltsverzeichnis"
ART_130_NOTICE = "/titel/130/ausgabe/43/2026/artikel/00000000000055313217-OL-130-2026-43-43-0"


def by_title(result):
    return {t.title.title_id: t for t in result.titles}


def test_offline_run_produces_traceable_notices(run_harvest):
    result, transport = run_harvest()
    titles = by_title(result)
    assert result.status == "succeeded"
    assert {k: t.status for k, t in titles.items()} == {
        "130": "ok",
        "1329": "download_disabled",  # Hessen, e-paper only, newest issue a preview without download
        "1740": "ok",
        "2701": "ok",
    }
    assert Counter(r.state_code for r in result.records) == {"SN": 9, "TH": 7, "RP": 4}
    for r in result.records:
        assert r.signal_type == "municipal_notice"
        assert r.source_url.endswith(f"/artikel/{r.source_record_id}")
        assert r.published_on is not None and r.title and r.body_text
        assert r.geo_status == "resolved" and r.x is not None
        assert len(r.content_hash) == 64
    # Only notices are fetched in full: 128 listed articles, 39 requests in total
    # (37 on ol.wittich.de; robots.txt and the viewer page on epaper.wittich.de).
    assert sum(t.articles_seen for t in result.titles) == 128
    assert result.requests == len(transport.calls) == 39
    disabled = titles["1329"]
    assert disabled.route == "epaper" and not disabled.records
    assert [e.code for e in disabled.errors] == ["epaper_download_disabled"]


def test_offline_run_is_deterministic(run_harvest):
    first, _ = run_harvest()
    second, _ = run_harvest()
    key = lambda res: sorted((r.source_record_id, r.content_hash) for r in res.records)  # noqa: E731
    assert key(first) == key(second)


def test_publication_dates_come_from_the_manifest(run_harvest):
    result, _ = run_harvest()
    dates: dict[str, set[str]] = {}
    for r in result.records:
        dates.setdefault(r.publication_id, set()).add(r.published_on.isoformat())
    # Kept exactly as the source states them (2026-10-25 is a Sunday), never
    # derived from the issue number. Issue 43/2026 of title 130 also carries
    # 22.10. items (church news, see test_parsers), but none are notices.
    assert dates == {"130": {"2026-10-23"}, "1740": {"2026-10-25"}, "2701": {"2026-10-23"}}


def test_duplicate_toc_entry_is_reported_and_collapsed(run_harvest):
    result, _ = run_harvest()
    th = by_title(result)["1740"]
    assert th.articles_seen == 28  # 29 entries listed, one article twice
    assert [e.code for e in th.errors] == ["toc_duplicate_entries"]


def test_failure_of_one_title_does_not_stop_the_run(run_harvest):
    result, _ = run_harvest(overrides={TOC.format(t=2701, nr=20, p=0): [FixtureRoute(status=500)]})
    titles = by_title(result)
    assert titles["2701"].status == "failed"
    assert titles["2701"].errors[0].code == "server_error"
    assert titles["130"].status == "ok" and titles["1740"].status == "ok"
    assert result.status == "partial"


def test_malformed_newest_issue_answer(run_harvest):
    result, _ = run_harvest(overrides={"/internal/neuste/ausgabe/1740": [FixtureRoute(body="<html>Fehler</html>")]})
    th = by_title(result)["1740"]
    assert th.status == "failed"
    assert (th.errors[0].stage, th.errors[0].code) == ("parse", "malformed_json")


def test_missing_article_body_is_flagged_not_dropped(run_harvest):
    result, _ = run_harvest(overrides={ART_130_NOTICE: [FixtureRoute(status=404)]})
    record = next(r for r in result.records if r.source_record_id.startswith("00000000000055313217"))
    assert record.body_text is None
    assert "missing_body" in record.quality_flags
    assert record.section == "Andere Behörden"  # still known from the manifest
    th = by_title(result)["130"]
    assert th.status == "partial"
    assert any(e.code == "http_404" and e.source_record_id == record.source_record_id for e in th.errors)


def test_missing_manifest_leaves_dates_empty_and_visible(run_harvest):
    overrides = {}
    for path, name in [
        ("/titel/2701/ausgabe/20/2026/artikel/{}", f"art_{aid}.html")
        for aid in ["00000000000055204488-OL-2701-2026-43-20-0", "00000000000055204491-OL-2701-2026-43-20-0", "00000000000055204497-OL-2701-2026-43-20-0", "00000000000055204479-OL-2701-2026-43-20-0", "00000000000055204476-OL-2701-2026-43-20-0", "00000000000055204485-OL-2701-2026-43-20-0", "00000000000055204482-OL-2701-2026-43-20-0", "00000000000055204473-OL-2701-2026-43-20-0", "00000000000055204500-OL-2701-2026-43-20-0", "00000000000055202406-OL-2701-2026-43-20-0"]
    ]:
        html = page(name)
        cut = html.index("article_list")
        overrides[path.format(name[4:-5])] = [FixtureRoute(body=html[:cut] + "</script></body></html>")]
    result, _ = run_harvest(overrides=overrides)
    th = by_title(result)["2701"]
    assert any(e.code == "manifest_missing" for e in th.errors)
    assert th.records, "notices with cues in headline/teaser are still found"
    for r in th.records:
        assert r.published_on is None and "missing_published_on" in r.quality_flags


def test_short_table_of_contents_is_reported(run_harvest):
    empty_page = page("toc_130_43_2026_p4.html")
    result, _ = run_harvest(overrides={TOC.format(t=130, nr=43, p=3): [FixtureRoute(body=empty_page)]})
    th = by_title(result)["130"]
    assert th.articles_seen == 60
    assert any(e.code == "toc_count_mismatch" and "advertises 79" in e.message for e in th.errors)


def test_robots_block_stops_all_fetching(run_harvest):
    result, transport = run_harvest(overrides={"/robots.txt": [FixtureRoute(body="User-agent: *\nDisallow: /\n")]})
    assert result.status == "failed"
    assert transport.calls == ["/robots.txt"]


def test_issue_selection_uses_the_cursor(settings, resolver):
    harvester = Harvester(settings, None, resolver)
    available = [(43, 2026), (42, 2026), (41, 2026), (40, 2026)]
    out = TitleHarvest(title=None)
    assert harvester.select_issues(available, None, out) == [(43, 2026)]
    assert harvester.select_issues(available, Cursor(2026, 43), out) == [(43, 2026)]  # overlap re-read
    assert harvester.select_issues(available, Cursor(2026, 41), out) == [(43, 2026), (42, 2026), (41, 2026)]
    assert not out.errors
    capped = dataclasses.replace(settings, harvest=dataclasses.replace(settings.harvest, max_issues_per_title=2))
    assert Harvester(capped, None, resolver).select_issues(available, Cursor(2025, 52), out) == [(43, 2026), (42, 2026)]
    assert out.errors[0].code == "issue_backlog_capped"


def test_failed_issue_does_not_advance_the_cursor(run_harvest):
    # Cursor at 42/2026: the run reads 43 (new) and 42 (overlap). 42 is not in
    # the fixtures, so it fails, and the cursor must stay where it was.
    result, _ = run_harvest(cursors={"130": Cursor(2026, 42)})
    th = by_title(result)["130"]
    assert th.issues_processed == [(43, 2026)] and th.issues_failed == [(42, 2026)]
    assert th.cursor is None
    assert th.status == "partial"
    assert len(th.records) == 4
