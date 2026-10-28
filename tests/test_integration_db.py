"""PostgreSQL 16 / PostGIS 3.4 integration tests.

Skipped unless IRIS_TEST_DATABASE_URL is set. The session creates one
throwaway database on that server (the role needs CREATEDB, as the
docker-compose superuser has), empties it before every test and drops it at
the end, so no existing data is touched.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from conftest import page
from wittich_harvester.cli import VALIDATION_SQL, _queries
from wittich_harvester.load import Cursor, LoadError, Store
from wittich_harvester.normalize import ErrorRecord
from wittich_harvester.pipeline import Harvester
from wittich_harvester.transport import FixtureRoute

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("IRIS_TEST_DATABASE_URL")
ART_130 = "00000000000055313217-OL-130-2026-43-43-0"


@pytest.fixture(scope="session")
def db_url():
    if not BASE_URL:
        pytest.skip("IRIS_TEST_DATABASE_URL is not set")
    name = f"iris_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(BASE_URL)
    params["dbname"] = name
    yield make_conninfo(**params)
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def store(db_url):
    store = Store(db_url)
    store.init_schema()
    store.conn.execute(
        "TRUNCATE iris_staging.signal_ingest_error, iris_staging.signal_ingest, "
        "iris_staging.harvest_cursor, iris_staging.ingest_run"
    )
    yield store
    store.close()


def scalar(store: Store, query: str, *params):
    return store.conn.execute(query, params).fetchone()[0]


def test_schema_applies_twice(store):
    store.init_schema()
    tables = {
        r[0]
        for r in store.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'iris_staging'"
        )
    }
    assert {"signal_ingest", "signal_ingest_error", "ingest_run", "harvest_cursor"} <= tables
    assert scalar(store, "SELECT postgis_lib_version()").startswith("3.4")


def test_rerun_inserts_nothing_new(store, run_harvest):
    first, _ = run_harvest(store=store)
    assert (first.counts.inserted, first.counts.updated, first.counts.unchanged) == (20, 0, 0)

    second, _ = run_harvest(store=store, cursors=store.cursors("wittich_ol"))
    assert (second.counts.inserted, second.counts.updated, second.counts.unchanged) == (0, 0, 20)

    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest") == 20
    assert scalar(store, "SELECT count(DISTINCT source_record_id) FROM iris_staging.signal_ingest") == 20
    # Provenance: first seen in run 1, last seen in run 2.
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest WHERE first_seen_run_id = %s", first.run_id) == 20
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest WHERE last_seen_run_id = %s", second.run_id) == 20
    statuses = [r[0] for r in store.conn.execute("SELECT status FROM iris_staging.ingest_run ORDER BY started_at")]
    assert statuses == ["succeeded", "succeeded"]


def test_changed_article_is_updated_in_place(store, run_harvest):
    run_harvest(store=store)
    changed = page(f"art_{ART_130}.html").replace("<h1>Kreisverwaltung Germersheim</h1>", "<h1>Kreisverwaltung Germersheim (Korrektur)</h1>")
    path = f"/titel/130/ausgabe/43/2026/artikel/{ART_130}"
    second, _ = run_harvest(store=store, cursors=store.cursors("wittich_ol"), overrides={path: [FixtureRoute(body=changed)]})
    assert (second.counts.inserted, second.counts.updated, second.counts.unchanged) == (0, 1, 19)
    assert scalar(store, "SELECT title FROM iris_staging.signal_ingest WHERE source_record_id = %s", ART_130) == (
        "Kreisverwaltung Germersheim (Korrektur)"
    )
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest") == 20


def test_duplicates_within_a_run_are_loaded_once(store, run_harvest, monkeypatch):
    original = Harvester.harvest_title

    def doubled(self, title, cursor):
        out = original(self, title, cursor)
        out.records = out.records + out.records  # every record twice
        return out

    monkeypatch.setattr(Harvester, "harvest_title", doubled)
    result, _ = run_harvest(store=store)
    assert result.counts.inserted == 20
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest") == 20
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest_error WHERE code = 'duplicate_identical'") == 20


def test_errors_are_stored_with_the_run(store, run_harvest):
    result, _ = run_harvest(store=store, overrides={f"/titel/130/ausgabe/43/2026/artikel/{ART_130}": [FixtureRoute(status=404)]})
    rows = store.conn.execute(
        "SELECT stage, code, source_record_id FROM iris_staging.signal_ingest_error WHERE run_id = %s AND severity = 'error'",
        (result.run_id,),
    ).fetchall()
    assert ("fetch", "http_404", ART_130) in rows
    flags = scalar(store, "SELECT quality_flags FROM iris_staging.signal_ingest WHERE source_record_id = %s", ART_130)
    assert "missing_body" in flags


@pytest.mark.parametrize(
    ("geom", "geo_status"),
    [
        ("ST_SetSRID(ST_MakePoint(5437126.5, 448234.9), 25832)", "resolved"),  # swapped axes
        ("ST_SetSRID(ST_MakePoint(0, 0), 25832)", "resolved"),
        ("ST_SetSRID(ST_MakePoint(448234.9, 5437126.5), 25832)", "unresolved"),  # point without resolution
        ("NULL", "resolved"),  # resolution without point
    ],
)
def test_database_rejects_invalid_geometry(store, geom, geo_status):
    run_id = uuid.uuid4()
    store.start_run(run_id, "test", {})
    with pytest.raises(psycopg.errors.CheckViolation):
        store.conn.execute(
            f"""INSERT INTO iris_staging.signal_ingest
                (source_system, source_record_id, source_url, signal_type, title, publication_id, state_code,
                 geom, geo_precision, geo_status, content_hash, raw, first_seen_run_id, last_seen_run_id)
                VALUES ('test', 'x', 'u', 'municipal_notice', 't', '1', 'RP', {geom},
                        {"'municipality'" if geom != "NULL" else "NULL"}, %s, repeat('0', 64), '{{}}', %s, %s)""",
            (geo_status, run_id, run_id),
        )


def test_wrong_srid_is_rejected(store):
    run_id = uuid.uuid4()
    store.start_run(run_id, "test", {})
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        store.conn.execute(
            """INSERT INTO iris_staging.signal_ingest
               (source_system, source_record_id, source_url, signal_type, title, publication_id, state_code,
                geom, geo_precision, geo_status, content_hash, raw, first_seen_run_id, last_seen_run_id)
               VALUES ('test', 'x', 'u', 'municipal_notice', 't', '1', 'RP', ST_SetSRID(ST_MakePoint(8.2, 49.1), 4326),
                       'municipality', 'resolved', repeat('0', 64), '{}', %s, %s)""",
            (run_id, run_id),
        )


def test_cursor_never_moves_backwards(store):
    run_id = uuid.uuid4()
    store.start_run(run_id, "wittich_ol", {})
    store.load_title(run_id, "wittich_ol", "130", [], [], Cursor(2026, 43, date(2026, 10, 23)))
    store.load_title(run_id, "wittich_ol", "130", [], [], Cursor(2026, 40, date(2026, 10, 2)))
    assert store.cursors("wittich_ol")["130"] == Cursor(2026, 43, date(2026, 10, 23))


def test_failed_load_rolls_back_the_whole_title(store, run_harvest):
    result, _ = run_harvest()  # no store: just records
    records = [r for r in result.records if r.publication_id == "130"]
    records[-1].state_code = "XX"  # violates the state CHECK
    run_id = uuid.uuid4()
    store.start_run(run_id, "wittich_ol", {})
    with pytest.raises(LoadError):
        store.load_title(
            run_id, "wittich_ol", "130", records, [ErrorRecord("validate", "x", "y", "warning")], Cursor(2026, 43)
        )
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest") == 0
    assert scalar(store, "SELECT count(*) FROM iris_staging.signal_ingest_error") == 0
    assert "130" not in store.cursors("wittich_ol")


def test_validation_queries_pass_after_a_run(store, run_harvest):
    run_harvest(store=store)
    for name, query in _queries(VALIDATION_SQL.read_text(encoding="utf-8")):
        assert store.conn.execute(query).fetchall() == [], name
