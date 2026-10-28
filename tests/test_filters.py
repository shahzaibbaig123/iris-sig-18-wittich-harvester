import pytest

from wittich_harvester.filters import classify, title_scope

STATES = ("RP", "HE", "TH", "SN")


def scope(settings, name="Jockgrim", state="Rheinland-Pfalz", freq="wöchentlich Freitag", codes=("073345003",)):
    return title_scope(name, state, freq, list(codes), STATES, settings.discovery)


# -- titles ---------------------------------------------------------------------


def test_state_comes_from_the_ars(settings):
    decision = scope(settings, state="")
    assert decision.in_scope and decision.state_code == "RP"


@pytest.mark.parametrize(
    ("codes", "label", "expected"),
    [
        (("120640000000",), "Brandenburg", "out_of_state_scope"),
        (("073345003",), "Thüringen", "state_label_conflict:Thüringen"),
        (("073345003", "160735005067"), "", "ars_spans_states"),
        ((), "", "no_state"),
    ],
)
def test_titles_outside_scope_are_dropped_with_a_reason(settings, codes, label, expected):
    decision = scope(settings, state=label, codes=codes)
    assert not decision.in_scope
    assert decision.reason == expected


def test_label_is_used_when_no_ars(settings):
    decision = scope(settings, state="Sachsen", codes=())
    assert decision.in_scope and decision.state_code == "SN"


@pytest.mark.parametrize(
    ("name", "freq", "reason"),
    [
        ("meinOrt Gebiet Germersheim", "täglich", "excluded_title:^meinOrt\\b"),
        ("SD Move It Südpfalz", "1 x im Jahr", "excluded_title:^SD\\b"),
        ("Kleinanzeigen Kassel", "wöchentlich", "excluded_title:Kleinanzeigen"),
        ("Gemeindeblatt X", "1 x im Jahr", "excluded_frequency:1 x im Jahr"),
    ],
)
def test_non_gazette_products_are_excluded(settings, name, freq, reason):
    decision = scope(settings, name=name, freq=freq)
    assert not decision.in_scope
    assert decision.reason == reason


# -- notices --------------------------------------------------------------------


def test_official_section_is_a_notice(settings):
    d = classify("Amtliche Bekanntmachungen", "Sitzungstermine der Ausschüsse", "", settings.filter)
    assert d.is_notice and d.tier == "strong" and d.category == "sitzung"


def test_non_official_section_wins_over_amtlich_substring(settings):
    d = classify("Nichtamtlicher Teil", "Jagdgenossenschaft Lippelsdorf", "Bekanntmachung", settings.filter)
    assert not d.is_notice and d.reason == "excluded_section:nichtamtlich"


def test_place_named_section_is_not_mistaken_for_church_news(settings):
    # "Kirchen (Sieg)" is a town; its section must not hit the "kirchen" exclusion.
    d = classify("Stadt Kirchen (Sieg)", "Öffentliche Bekanntmachung der Haushaltssatzung", "", settings.filter)
    assert d.is_notice and d.tier == "weak"
    assert classify("Kirchen in der Verbandsgemeinde", "Jahresrechnung 2025", "", settings.filter).is_notice is False


def test_excluded_headline_beats_official_section(settings):
    d = classify("Amtlicher Teil", "Redaktionsschluss", "", settings.filter)
    assert not d.is_notice and d.reason == "excluded_headline:redaktionsschluss"


def test_weak_section_needs_a_cue(settings):
    flt = settings.filter
    assert classify("Ortsgemeinde Jockgrim", "Gremientermine", "", flt).is_notice
    assert classify("Ortsgemeinde Neupotz", "Christbäume gesucht", "Ihre Hilfe ist gefragt", flt).is_notice is False
    cue = classify("Ortsgemeinde Hatzenbühl", "Einladung", "Die öffentliche Bekanntmachung erfolgte am 15.10.", flt)
    assert cue.is_notice and cue.reason == "weak_section+cue:bekanntmach"


def test_parents_committee_is_not_a_council_meeting(settings):
    d = classify("Ortsgemeinde Hatzenbühl", "Kita Wirbelwind - Elternausschuss der Kita 2026-27", "", settings.filter)
    assert not d.is_notice


def test_category_prefers_the_headline(settings):
    teaser = "Entscheidung über das gemeindliche Einvernehmen nach § 36 BauGB zum Bauantrag"
    assert classify("Ortsgemeinde Jockgrim", "Gremientermine", teaser, settings.filter).category == "sitzung"
    assert classify("Amtlicher Teil", "Amtsgericht Rudolstadt", "Terminsbestimmung", settings.filter).category == "zustellung_justiz"
    assert classify("Amtlicher Teil", "Informationen des Bürgermeisters", "", settings.filter).category == "general"


# Regressions from the live run of 2026-10-28 (titles 104 and 2600).


def test_mixed_section_is_not_strong(settings):
    flt = settings.filter
    mixed = "Gemeinsame Informationen und Bekanntmachungen"
    assert not classify(mixed, "Kreisschau der Rassekaninchen", "Der Verein lädt ein", flt).is_notice
    assert classify(mixed, "Bekanntmachung der Haushaltssatzung 2027", "", flt).is_notice
    assert classify("Amtliche Bekanntmachungen", "Die Stadtbibliothek informiert", "", flt).tier == "strong"


def test_authority_names_count_in_headlines_only(settings):
    flt = settings.filter
    bus = classify("Bellheim", "Fahrten mit dem Bürgerbus", "unterstützt von der Kreisverwaltung", flt)
    assert not bus.is_notice
    prescription = classify("Bellheim", "Rheumaliga Bellheim", "Bitte eine gültige Verordnung mitbringen", flt)
    assert not prescription.is_notice
    authority = classify("Andere Mitteilungen", "Kreisverwaltung Germersheim", "", flt)
    assert authority.is_notice and authority.reason == "weak_section+headline_cue:kreisverwaltung"
