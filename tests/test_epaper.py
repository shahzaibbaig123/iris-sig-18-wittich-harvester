"""E-paper route (pattern B, PDF).

Viewer pages are real captures (tests/fixtures/epaper). The PDF is synthetic,
built below with the typography of a real issue (Arial-like body 9 pt,
bold 12 pt headlines, bold 16 pt section headings, ads in another font), so
no copyrighted or personal content is committed.
"""

from datetime import date

import pytest

from conftest import fixture_text
from wittich_harvester.load import Cursor
from wittich_harvester.parsers import ParseError
from wittich_harvester.parsers.epaper import parse_pdf_issue, parse_viewer
from wittich_harvester.transport import FixtureRoute, Response

BODY, BOLD, AD = "F1", "F2", "F3"
FONTS = {BODY: "Helvetica", BOLD: "Helvetica-Bold", AD: "Courier-Bold"}
FILLER = "Die Verwaltung informiert die Bürgerinnen und Bürger über aktuelle Themen der Gemeinde."


def make_pdf(pages: list[list[tuple[str, float, str]]], title: str = "Musterstädter Anzeiger 22 2026") -> bytes:
    """A minimal PDF: one text line per (font, size, text) entry, top to bottom."""

    def literal(text: str) -> bytes:
        raw = text.encode("cp1252")
        return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"

    objects: list[bytes] = []
    font_ids = {}
    for key, name in FONTS.items():
        objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode())
        font_ids[key] = len(objects)
    fonts = " ".join(f"/{k} {i} 0 R" for k, i in font_ids.items())
    page_ids = []
    pages_id = len(objects) + 1 + 2 * len(pages)  # objects: fonts, (content, page)*n, pages, catalog, info
    for lines in pages:
        y, stream = 800, b""
        for font, size, text in lines:
            stream += f"BT /{font} {size} Tf 40 {y} Td ".encode() + literal(text) + b" Tj ET\n"
            y -= int(size * 1.6)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << {fonts} >> >> /Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objects))
    objects.append(f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode())
    objects.append(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    catalog_id = len(objects)
    objects.append(b"<< /Title " + literal(title) + b" /Creator (LINUS WITTICH Medien KG) >>")
    info_id = len(objects)

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


ISSUE_PAGES = [
    [  # page 1: masthead and an advertisement in another font
        (BODY, 9, "1391/Jahrgang 61 | Freitag, den 30. Oktober 2026 Nummer 22"),
        (AD, 24, "AUTOHAUS MUSTER"),
        (AD, 10, "Reifenwechsel jetzt buchen"),
        *[(BODY, 9, FILLER)] * 4,
    ],
    [  # page 2: a club section, then the official notice
        (BOLD, 16, "Vereine und Verbände"),
        (BOLD, 12, "Musikverein Musterstadt e. V."),
        (BODY, 9, "Probe am Freitag um 19 Uhr."),
        (BOLD, 16, "Politik"),
        (BOLD, 12, "Einladung zur Sitzung"),
        (BOLD, 12, "der Stadtverordnetenversammlung"),
        (BODY, 9, "BEKANNTMACHUNG"),
        (BODY, 9, "Die Stadtverordnetenversammlung tritt am Montag, 02.11.2026, um 19.30 Uhr zusammen."),
        (BODY, 9, "Tagesordnung: 1. Mitteilungen 2. Haushaltssatzung 2027 3. Betriebsabrech"),
        (BODY, 9, "-"),
        (BODY, 9, "nung 2024"),
        *[(BODY, 9, FILLER)] * 3,
    ],
    [  # page 3: running header, then the notice continues
        (BODY, 9, "Musterstadt – 3 – Nr. 22/2026"),
        (BODY, 9, "Die Sitzung ist öffentlich."),
        (BOLD, 12, "Hilfe bei Demenz"),
        (BODY, 9, "Beratung jeden Dienstag im Rathaus."),
        *[(BODY, 9, FILLER)] * 4,
    ],
    [  # page 4: body set larger (11 pt), headline 14 pt
        (BOLD, 14, "Benefizkonzert in der Stadthalle"),
        *[(BODY, 11, FILLER)] * 6,
    ],
]
PDF = make_pdf(ISSUE_PAGES)
VIEWER_ENABLED = fixture_text("epaper/viewer_1391.html").replace("1391_22_2026", "1329_22_2026")
PDF_PATH = "/frontend/catalogs/555516/1/pdf/complete.pdf"
VIEWER_PATH = "/frontend/mvc/catalog/by-name/1329/newest"


# -- viewer ---------------------------------------------------------------------


def test_viewer_with_download_enabled():
    viewer = parse_viewer(fixture_text("epaper/viewer_1391.html"))
    assert (viewer.title_id, viewer.issue_number, viewer.issue_year) == ("1391", 22, 2026)
    assert viewer.complete_pdf_path == PDF_PATH
    assert viewer.downloadable


def test_preview_viewer_is_not_downloadable():
    viewer = parse_viewer(fixture_text("epaper/viewer_1329_preview.html"))
    assert viewer.preview and not viewer.save_complete_enabled
    assert not viewer.downloadable


def test_viewer_without_issue_title_is_an_error():
    with pytest.raises(ParseError):
        parse_viewer("<html><title>Wartung</title></html>")


# -- PDF segmentation -------------------------------------------------------------


def test_pdf_is_segmented_by_typography():
    issue = parse_pdf_issue(PDF)
    assert issue.pages == 4
    assert issue.issue_date == date(2026, 10, 30)  # from the masthead, as printed
    assert issue.document_title == "Musterstädter Anzeiger 22 2026"
    blocks = {b.headline: b for b in issue.blocks}
    assert list(blocks) == [
        "Musikverein Musterstadt e. V.",
        "Einladung zur Sitzung der Stadtverordnetenversammlung",  # two headline lines joined
        "Hilfe bei Demenz",
        "Benefizkonzert in der Stadthalle",  # page 4 judged against its own 11 pt body
    ]
    notice = blocks["Einladung zur Sitzung der Stadtverordnetenversammlung"]
    assert (notice.page, notice.section) == (2, "Politik")
    assert notice.body.startswith("BEKANNTMACHUNG")
    assert "Die Sitzung ist öffentlich." in notice.body  # continued on page 3
    assert "Nr. 22/2026" not in notice.body  # running header dropped
    assert "3. Betriebsabrechnung 2024" in notice.body  # hyphenated line break joined
    assert "AUTOHAUS" not in " ".join(b.body for b in issue.blocks)  # ad font skipped
    assert blocks["Benefizkonzert in der Stadthalle"].body.count(FILLER) == 6


def test_not_a_pdf_and_broken_pdf():
    with pytest.raises(ParseError) as exc:
        parse_pdf_issue(b"<html>Fehler</html>")
    assert exc.value.code == "not_a_pdf"
    with pytest.raises(ParseError):
        parse_pdf_issue(b"%PDF-1.4\n garbage")


# -- pipeline ---------------------------------------------------------------------


@pytest.fixture
def run_epaper(run_harvest, transport):
    def _run(pdf: bytes = PDF, viewer: str = VIEWER_ENABLED, cursors=None, **kw):
        # Serve the synthetic PDF as bytes; everything else from the fixtures.
        original = transport.get

        def get(url, headers, timeout):
            if url.endswith(PDF_PATH):
                transport.calls.append(PDF_PATH)
                return Response(url=url, status=200, content=pdf, headers={"Content-Type": "application/pdf"})
            return original(url, headers, timeout)

        transport.get = get
        overrides = {VIEWER_PATH: [FixtureRoute(body=viewer)]}
        return run_harvest(transport=transport, overrides=overrides, cursors=cursors, **kw)

    return _run


def by_title(result):
    return {t.title.title_id: t for t in result.titles}


def test_epaper_title_yields_notice_records(run_epaper):
    result, transport = run_epaper()
    he = by_title(result)["1329"]
    assert (he.route, he.status) == ("epaper", "ok")
    assert he.articles_seen == 4 and he.notices == 1
    (record,) = he.records
    assert record.source_system == "wittich_epaper"
    assert record.source_url == f"https://epaper.wittich.de{PDF_PATH}#page=2"
    assert record.source_record_id.startswith("1329-2026-22-p002-")
    assert record.title == "Einladung zur Sitzung der Stadtverordnetenversammlung"
    assert record.notice_category == "sitzung"
    assert record.published_on == date(2026, 10, 30)
    assert "issue_level_date" in record.quality_flags
    assert record.state_code == "HE"
    # No place in the PDF section: located at the publication area, and said so.
    assert record.geo_status == "fallback_publication_area" and record.x is not None
    assert he.cursor == Cursor(2026, 22, date(2026, 10, 30))
    assert PDF_PATH in transport.calls
    assert result.status == "succeeded"


def test_epaper_ids_are_stable_across_runs(run_epaper):
    first, _ = run_epaper()
    second, _ = run_epaper()
    key = lambda res: [(r.source_record_id, r.content_hash) for r in by_title(res)["1329"].records]  # noqa: E731
    assert key(first) == key(second)


def test_issue_already_harvested_is_not_downloaded_again(run_epaper):
    result, transport = run_epaper(cursors={"1329": Cursor(2026, 22)})
    assert by_title(result)["1329"].status == "up_to_date"
    assert PDF_PATH not in transport.calls


def test_download_disabled_is_respected(run_epaper):
    result, transport = run_epaper(viewer=fixture_text("epaper/viewer_1329_preview.html"))
    he = by_title(result)["1329"]
    assert he.status == "download_disabled" and not he.records
    assert not any("pdf" in call for call in transport.calls)


def test_missing_masthead_date_is_flagged(run_epaper):
    pages = [[line for line in ISSUE_PAGES[0] if "Oktober" not in line[2]], *ISSUE_PAGES[1:]]
    result, _ = run_epaper(pdf=make_pdf(pages))
    he = by_title(result)["1329"]
    assert any(e.code == "pdf_issue_date_missing" for e in he.errors)
    assert he.records[0].published_on is None
    assert "missing_published_on" in he.records[0].quality_flags


def test_broken_pdf_fails_only_that_title(run_epaper):
    result, _ = run_epaper(pdf=b"%PDF-1.4\n garbage")
    titles = by_title(result)
    assert titles["1329"].status == "failed"
    assert titles["1329"].errors[0].stage == "parse"
    assert titles["130"].status == "ok"
    assert result.status == "partial"
