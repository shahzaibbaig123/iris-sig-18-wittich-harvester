import dataclasses

from wittich_harvester.catalogue import CatalogueTitle, discover
from wittich_harvester.http import PoliteClient


def run_discover(settings, transport, **discovery):
    cfg = dataclasses.replace(settings, discovery=dataclasses.replace(settings.discovery, **discovery))
    return discover(PoliteClient(cfg.http, transport, sleep=lambda _s: None), cfg), transport


def test_sweep_finds_one_title_per_state(settings, transport):
    catalogue, _ = run_discover(settings, transport)
    in_scope = {t.title_id: t for t in catalogue.in_scope()}
    assert {in_scope[i].state_code for i in ("130", "1329", "1740", "2701")} == {"RP", "HE", "TH", "SN"}
    assert in_scope["130"].ars == "073345003"
    excluded = {t.title_id: t.reason for t in catalogue.titles if not t.in_scope}
    assert excluded["800119"].startswith("excluded_title:^meinOrt")  # app feed, not a gazette


def test_saturated_prefix_is_narrowed(settings, transport):
    catalogue, transport = run_discover(settings, transport, zip_prefixes=("07",), max_prefix_length=3)
    assert catalogue.saturated_prefixes == ["07"]
    assert catalogue.prefixes_queried == ["07"] + [f"07{d}" for d in "0123456789"]
    # The narrower pages are not in the fixtures: recorded as fetch errors, no crash.
    assert {e.code for e in catalogue.errors} == {"http_404"}
    assert len(catalogue.in_scope()) > 0


def test_saturation_at_maximum_length_is_a_visible_warning(settings, transport):
    catalogue, _ = run_discover(settings, transport, zip_prefixes=("07",), max_prefix_length=2)
    assert [(e.code, e.severity) for e in catalogue.errors] == [("search_saturated", "warning")]


def test_overview_cross_check(settings, transport):
    catalogue, _ = run_discover(settings, transport, zip_prefixes=("07",), max_prefix_length=2, use_overview=True)
    assert catalogue.overview_in_scope == 119  # Sachsen 92 + Thüringen 27
    assert 0 < catalogue.overview_found_by_sweep < catalogue.overview_in_scope
    added = [t for t in catalogue.titles if t.sources == ["overview"]]
    assert added and all(t.ars is None for t in added)


def test_ars_of_several_codes_is_their_shared_area():
    title = CatalogueTitle("1", "x", "RP", "", "", ["073345003009", "073345003012"], [], True, "in_scope")
    assert title.ars == "073345003"
    assert dataclasses.replace(title, regiocodes=[]).ars is None
