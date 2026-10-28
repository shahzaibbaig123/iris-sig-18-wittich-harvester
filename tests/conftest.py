"""Shared test fixtures. Everything runs offline against tests/fixtures."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest

from wittich_harvester.catalogue import discover
from wittich_harvester.config import load_settings
from wittich_harvester.geo import Gazetteer, GeoResolver
from wittich_harvester.http import PoliteClient
from wittich_harvester.pipeline import Harvester
from wittich_harvester.transport import FixtureRoute, FixtureTransport

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 28)  # capture date of the fixtures


def page(name: str) -> str:
    return (FIXTURES / "pages" / name).read_text(encoding="utf-8")


def fixture_text(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def settings():
    return load_settings(overlay=FIXTURES / "offline.toml", env={})


@pytest.fixture(scope="session")
def gazetteer(settings):
    return Gazetteer.load(settings.geo.gazetteer_path)


@pytest.fixture
def resolver(gazetteer, settings):
    return GeoResolver(gazetteer, settings.geo)


@pytest.fixture
def transport():
    return FixtureTransport.from_manifest(FIXTURES / "routes.json")


@pytest.fixture
def run_harvest(settings, resolver):
    """Discover + harvest over a transport; returns (result, transport)."""

    def _run(transport=None, store=None, cursors=None, overrides: dict[str, list[FixtureRoute]] | None = None, settings_=None):
        transport = transport or FixtureTransport.from_manifest(FIXTURES / "routes.json")
        for path, routes in (overrides or {}).items():
            from collections import deque

            transport.routes[path] = deque(routes)
        cfg = settings_ or settings
        client = PoliteClient(cfg.http, transport, sleep=lambda _s: None)
        catalogue = discover(client, cfg)
        titles = [t for t in catalogue.in_scope() if t.title_id in cfg.harvest.titles]
        harvester = Harvester(cfg, client, resolver, store, today=TODAY)
        run_id = uuid.uuid4()
        if store is not None:
            store.start_run(run_id, cfg.source_system, {"test": True})
        result = harvester.run(titles, cursors or {}, run_id=run_id)
        if store is not None:
            store.finish_run(run_id, result.status, {"load": vars(result.counts)})
        return result, transport

    return _run
