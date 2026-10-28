import math

import pytest

from wittich_harvester.geo import Gazetteer, GeoResolver, Place

JOCKGRIM_VG = "073345003"
SCHIEFERGEBIRGE_SEAT = "160735005067"  # how Wittich codes title 1740 (Probstzella)
HEIDENAU = "146280160160"


def test_section_names_the_municipality(resolver):
    geo = resolver.resolve("RP", JOCKGRIM_VG, "Ortsgemeinde Hatzenbühl", "Jockgrim")
    assert geo.status == "resolved"
    assert geo.precision == "municipality"
    assert (geo.ars, geo.municipality_name) == ("073345003009", "Hatzenbühl")
    assert geo.x is not None and geo.y is not None


def test_label_repeating_the_publication_area_means_the_whole_area(resolver):
    geo = resolver.resolve("RP", JOCKGRIM_VG, "Andere Behörden", "Jockgrim")
    assert (geo.status, geo.precision, geo.ars) == ("resolved", "municipal_association", JOCKGRIM_VG)


def test_seat_coded_title_covers_its_verwaltungsgemeinschaft(gazetteer, resolver):
    area = gazetteer.publication_area(SCHIEFERGEBIRGE_SEAT)
    assert (area.level, area.name) == ("municipal_association", "Schiefergebirge")
    geo = resolver.resolve("TH", SCHIEFERGEBIRGE_SEAT, "Amtlicher Teil", "VG Schiefergebirge")
    assert (geo.status, geo.precision) == ("resolved", "municipal_association")


def test_einheitsgemeinde_resolves_to_the_municipality(resolver):
    geo = resolver.resolve("SN", HEIDENAU, "Amtliche Bekanntmachungen", "Heidenau")
    assert (geo.status, geo.precision, geo.ars) == ("resolved", "municipality", HEIDENAU)


def test_label_listing_several_municipalities_falls_back_to_their_verband(resolver):
    geo = resolver.resolve("TH", SCHIEFERGEBIRGE_SEAT, "Nichtamtlicher Teil", "Probstzella, Lehesten und Gräfenthal")
    assert geo.status == "fallback_publication_area"
    assert geo.precision == "municipal_association"
    assert "3 municipalities" in geo.note


def test_names_are_matched_inside_the_publication_area_only(resolver):
    # Thuringia has two "Lehesten"; within VG Schiefergebirge only one fits.
    geo = resolver.resolve("TH", SCHIEFERGEBIRGE_SEAT, "", "Lehesten")
    assert (geo.status, geo.ars) == ("resolved", "160735005046")


def test_ambiguous_name_gets_no_point(resolver):
    geo = resolver.resolve("TH", None, "", "Lehesten")
    assert geo.status == "ambiguous"
    assert geo.x is None and geo.precision is None
    assert sorted(geo.candidates) == ["160735005046", "160745015051"]


def test_unknown_place_without_area_is_unresolved(resolver):
    geo = resolver.resolve("RP", None, "", "Atlantis")
    assert geo.status == "unresolved" and geo.x is None
    assert "not found" in geo.note


def test_unknown_place_falls_back_to_the_publication_area_with_a_note(resolver):
    geo = resolver.resolve("RP", JOCKGRIM_VG, "Ortsgemeinde Atlantis", "")
    assert geo.status == "fallback_publication_area"
    assert "'Atlantis' not found" in geo.note


@pytest.mark.parametrize(
    ("x", "y", "reason"),
    [
        (5437126.5, 448234.9, "envelope"),  # swapped axes
        (448234.9, 5437126.5 + 2_000_000, "envelope"),
        (800000.0, 5650000.0, "extent of RP"),  # valid UTM32 point, but in Saxony
        (math.nan, 5437126.5, "NaN"),
    ],
)
def test_invalid_coordinates_are_rejected(resolver, x, y, reason):
    assert reason in resolver.validate(x, y, "RP")


def test_bad_gazetteer_point_becomes_invalid_not_stored(settings):
    places = [
        Place("municipal_association", "073345003", "", "Jockgrim", "Verbandsgemeinde", "RP", 449562.8, 5438717.1),
        Place("municipality", "073345003012", "07334012", "Jockgrim", "Gemeinde", "RP", 449562.8, 5438717.1),
        # Corrupted row: x and y swapped.
        Place("municipality", "073345003009", "07334009", "Hatzenbühl", "Gemeinde", "RP", 5440627.3, 444470.3),
    ]
    resolver = GeoResolver(Gazetteer(places), settings.geo)
    geo = resolver.resolve("RP", "073345003", "Ortsgemeinde Hatzenbühl", "Jockgrim")
    assert geo.status == "invalid"
    assert geo.x is None and geo.y is None and geo.precision is None
    assert "rejected point" in geo.note


def test_bare_association_label_denotes_the_publication_area(resolver):
    # Title 104 labels VG-wide items just "Verbandsgemeinde" (live run 2026-10-28).
    geo = resolver.resolve("RP", "073345001", "Amtsblatt der Verbandsgemeinde Bellheim", "Verbandsgemeinde")
    assert (geo.status, geo.precision, geo.ars) == ("resolved", "municipal_association", "073345001")
    assert "denotes the publication area" in geo.note
