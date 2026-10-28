from datetime import date

import pytest

from conftest import fixture_text, page
from wittich_harvester.parsers import ParseError
from wittich_harvester.parsers.article import parse_article, parse_manifest
from wittich_harvester.parsers.catalogue import parse_overview, parse_search_results
from wittich_harvester.parsers.issue import parse_toc

TOC_130 = "https://ol.wittich.de/titel/130/ausgabe/43/2026/rubrik/alle/seite/0/sortiert/inhaltsverzeichnis"


# -- catalogue ----------------------------------------------------------------


def test_search_results_merge_mobile_and_desktop_blocks():
    titles = {t.title_id: t for t in parse_search_results(fixture_text("search/zip_76.html"))}
    jockgrim = titles["130"]
    assert jockgrim.name == "Jockgrim"  # from the desktop <h2>
    assert jockgrim.regiocodes == ["073345003"]  # from the mobile block
    assert jockgrim.state_name == "Rheinland-Pfalz"  # hyphenated state survives the label split
    assert jockgrim.frequency == "wöchentlich Freitag"
    assert len(titles) == 32  # each title once, although rendered twice


def test_search_results_empty_fragment_means_no_hits():
    assert parse_search_results("") == []
    assert parse_search_results("   \n ") == []


def test_search_results_unexpected_page_is_an_error():
    with pytest.raises(ParseError) as exc:
        parse_search_results("<html><head><title>Wartung</title></head><body>Wartungsarbeiten</body></html>")
    assert exc.value.code == "unexpected_markup"


def test_overview_lists_titles_by_state():
    titles = parse_overview(fixture_text("search/uebersicht.html"))
    states = {t.state_name for t in titles}
    assert states == {"Brandenburg", "Sachsen", "Sachsen-Anhalt", "Thüringen"}
    assert len(titles) == 199
    first = titles[0]
    assert (first.title_id, first.postcode, first.state_name) == ("2853", "03058", "Brandenburg")


# -- table of contents --------------------------------------------------------


def test_toc_page_zero():
    toc = parse_toc(page("toc_130_43_2026_p0.html"), TOC_130)
    assert (toc.title_id, toc.issue_number, toc.issue_year, toc.page) == ("130", 43, 2026, 0)
    assert toc.article_count == 79
    assert len(toc.stubs) == 20
    assert toc.available_issues[:2] == [(43, 2026), (42, 2026)]
    assert "Ortsgemeinde Hatzenbühl" in toc.rubrics
    stub = toc.stubs[0]
    assert stub.article_id == "00000000000055313352-OL-130-2026-43-43-0"
    assert stub.path.endswith("/artikel/00000000000055313352-OL-130-2026-43-43-0")
    assert stub.place_label == "Jockgrim"
    assert stub.position == 0


def test_toc_positions_continue_across_pages_and_last_page_is_empty():
    pages = [parse_toc(page(f"toc_130_43_2026_p{n}.html"), TOC_130) for n in range(5)]
    assert [len(p.stubs) for p in pages] == [20, 20, 20, 19, 0]
    assert pages[1].stubs[0].position == 20
    assert sum(len(p.stubs) for p in pages) == pages[0].article_count


def test_toc_without_online_edition_is_empty_not_an_error():
    toc = parse_toc(page("toc_1329_empty.html"), "https://ol.wittich.de/titel/1329")
    assert toc.issue_year == 1900
    assert not toc.has_online_edition
    assert toc.stubs == []


def test_toc_malformed_page_raises():
    broken = page("toc_130_43_2026_p0.html").replace("Artikel in dieser Ausgabe", "")
    with pytest.raises(ParseError) as exc:
        parse_toc(broken, TOC_130)
    assert exc.value.code == "missing_article_count"
    with pytest.raises(ParseError) as exc:
        parse_toc("", TOC_130)
    assert exc.value.code == "empty_body"


# -- articles -----------------------------------------------------------------


def test_article_fields_and_manifest():
    art = parse_article(page("art_00000000000055313196-OL-130-2026-43-43-0.html"), "u")
    assert art.article_id == "00000000000055313196-OL-130-2026-43-43-0"
    assert art.headline == "Einladung zur Einwohnerversammlung"
    assert art.section == "Ortsgemeinde Hatzenbühl"
    assert art.publication_title == "Amtsblatt VG Jockgrim"
    assert art.issue_label == "Ausgabe 43/2026"
    assert art.body_text.startswith("Am Dienstag, 27.10.2026 um 19:00 Uhr")
    assert "1. Daseinsvorsorge in Hatzenbühl:" in art.body_text  # table rows kept once
    assert art.body_text.count("Daseinsvorsorge in Hatzenbühl") == 1
    assert len(art.manifest) == 79
    item = next(m for m in art.manifest if m.article_id == art.article_id)
    assert item.published_on == date(2026, 10, 23)
    assert item.rubric == "Ortsgemeinde Hatzenbühl"


def test_manifest_dates_differ_within_an_issue():
    # Issue 43/2026 is mostly dated 23.10., some items 22.10.: the date comes
    # from the source per article, never from the issue number.
    items, errors = parse_manifest(page("art_00000000000055313196-OL-130-2026-43-43-0.html"))
    assert not errors
    assert {m.published_on for m in items} == {date(2026, 10, 23), date(2026, 10, 22)}


def test_headline_invisible_characters_are_removed():
    art = parse_article(page("art_00000000000055204488-OL-2701-2026-43-20-0.html"), "u")
    assert art.headline == "Information zur Einwohnerversammlung"  # source has U+200B prefixes


def test_manifest_bad_date_is_reported_not_guessed():
    html = "<script>article_list.push({ id: 'X-1', date: 'kein Datum', caption: 'A', rubric: 'B' });</script>"
    items, errors = parse_manifest(html)
    assert items[0].published_on is None
    assert errors and "X-1" in errors[0]


def test_article_without_headline_raises():
    html = page("art_00000000000055313196-OL-130-2026-43-43-0.html").replace(
        "<h1>Einladung zur Einwohnerversammlung</h1>", ""
    )
    with pytest.raises(ParseError) as exc:
        parse_article(html, "u")
    assert exc.value.code == "missing_headline"
