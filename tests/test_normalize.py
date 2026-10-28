from dataclasses import replace
from datetime import date

from wittich_harvester.normalize import SignalRecord, date_flags, dedupe, headline_flags
from wittich_harvester.text import clean, fold


def record(**changes) -> SignalRecord:
    base = SignalRecord(
        source_system="wittich_ol",
        source_record_id="00000000000055313196-OL-130-2026-43-43-0",
        source_url="https://ol.wittich.de/titel/130/ausgabe/43/2026/artikel/00000000000055313196-OL-130-2026-43-43-0",
        signal_type="municipal_notice",
        notice_category="sitzung",
        title="Einladung zur Einwohnerversammlung",
        summary="Am Dienstag …",
        body_text="Am Dienstag, 27.10.2026 …",
        published_on=date(2026, 10, 23),
        section="Ortsgemeinde Hatzenbühl",
        publication_id="130",
        publication_title="Amtsblatt VG Jockgrim",
        issue_number=43,
        issue_year=2026,
        issue_url=None,
        state_code="RP",
        ars="073345003009",
        municipality_name="Hatzenbühl",
        place_text="Ortsgemeinde Hatzenbühl",
        x=444470.3,
        y=5440627.3,
        geo_precision="municipality",
        geo_status="resolved",
        geo_note=None,
        raw={"fetched": "first"},
    )
    rec = replace(base, **changes)
    rec.compute_hash()
    return rec


def test_hash_is_stable_and_ignores_raw_payload():
    assert record().content_hash == record().content_hash
    assert record().content_hash == record(raw={"fetched": "later"}).content_hash
    assert len(record().content_hash) == 64


def test_hash_changes_with_content():
    assert record().content_hash != record(title="Einladung zur Einwohnerversammlung (geändert)").content_hash
    assert record().content_hash != record(published_on=date(2026, 10, 22)).content_hash


def test_flag_order_does_not_change_the_hash():
    assert record(quality_flags=["a", "b"]).content_hash == record(quality_flags=["b", "a"]).content_hash


def test_date_flags():
    issue_date = date(2026, 10, 23)
    assert date_flags(None, issue_date, 2026) == ["missing_published_on"]
    assert date_flags(date(2026, 10, 22), issue_date, 2026) == []
    assert date_flags(date(2026, 8, 1), issue_date, 2026) == ["published_on_outlier"]
    assert date_flags(date(2019, 1, 1), None, 2026) == ["published_on_outlier"]


def test_headline_mismatch_ignores_case_and_invisible_characters():
    assert headline_flags("Gremientermine", "GREMIENTERMINE", "​Gremientermine") == []
    assert headline_flags("Gremientermine", "Sitzungstermine", None) == ["headline_mismatch"]


def test_dedupe_keeps_first_and_reports_repeats():
    first, same, changed = record(), record(), record(title="Anders")
    unique, errors = dedupe([first, same, changed])
    assert unique == [first]
    assert [e.code for e in errors] == ["duplicate_identical", "duplicate_conflict"]
    assert all(e.severity == "warning" for e in errors)


def test_text_cleaning():
    assert clean("​​Information&nbsp;zur  Einwohner­versammlung ") == "Information zur Einwohnerversammlung"
    assert fold("Gräfenthal") == fold("GRAEFENTHAL") == "graefenthal"
