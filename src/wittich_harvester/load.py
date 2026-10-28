"""PostgreSQL/PostGIS loading: idempotent upserts keyed on the source id.

Re-running a harvest never creates a second row for an article:
  * new (source_system, source_record_id)       -> inserted
  * known key, different content_hash           -> updated
  * known key, same content_hash                -> unchanged (only last_seen moves)
Each title is loaded in one transaction together with its cursor, so a
failure leaves neither half-loaded records nor a cursor that skips them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from wittich_harvester.normalize import ErrorRecord, SignalRecord

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "sql" / "001_schema.sql"

COLUMNS = (
    "source_system",
    "source_record_id",
    "source_url",
    "signal_type",
    "notice_category",
    "title",
    "summary",
    "body_text",
    "published_on",
    "section",
    "publication_id",
    "publication_title",
    "issue_number",
    "issue_year",
    "issue_url",
    "state_code",
    "ars",
    "municipality_name",
    "place_text",
    "geo_precision",
    "geo_status",
    "geo_note",
    "quality_flags",
    "content_hash",
    "raw",
)

_STAGE_TABLE = f"""
CREATE TEMP TABLE IF NOT EXISTS tmp_signal_ingest (
    {", ".join(f"{c} {t}" for c, t in (
        ("source_system", "text"), ("source_record_id", "text"), ("source_url", "text"),
        ("signal_type", "text"), ("notice_category", "text"), ("title", "text"), ("summary", "text"),
        ("body_text", "text"), ("published_on", "date"), ("section", "text"), ("publication_id", "text"),
        ("publication_title", "text"), ("issue_number", "integer"), ("issue_year", "integer"),
        ("issue_url", "text"), ("state_code", "char(2)"), ("ars", "text"), ("municipality_name", "text"),
        ("place_text", "text"), ("geo_precision", "text"), ("geo_status", "text"), ("geo_note", "text"),
        ("quality_flags", "text[]"), ("content_hash", "char(64)"), ("raw", "jsonb"),
    ))},
    x double precision,
    y double precision
) ON COMMIT DELETE ROWS
"""

_UPSERT = f"""
WITH upsert AS (
    INSERT INTO iris_staging.signal_ingest AS s
        ({", ".join(COLUMNS)}, geom, first_seen_run_id, last_seen_run_id)
    SELECT {", ".join(f"t.{c}" for c in COLUMNS)},
           CASE WHEN t.x IS NULL OR t.y IS NULL THEN NULL
                ELSE ST_SetSRID(ST_MakePoint(t.x, t.y), 25832) END,
           %(run_id)s, %(run_id)s
    FROM tmp_signal_ingest t
    ON CONFLICT (source_system, source_record_id) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in ("source_system", "source_record_id"))},
        geom = EXCLUDED.geom,
        last_seen_run_id = EXCLUDED.last_seen_run_id,
        last_seen_at = now(),
        updated_at = now()
    WHERE s.content_hash IS DISTINCT FROM EXCLUDED.content_hash
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted) FROM upsert
"""

_TOUCH = """
UPDATE iris_staging.signal_ingest s
SET last_seen_run_id = %(run_id)s, last_seen_at = now()
FROM tmp_signal_ingest t
WHERE s.source_system = t.source_system
  AND s.source_record_id = t.source_record_id
  AND s.last_seen_run_id IS DISTINCT FROM %(run_id)s
"""

_CURSOR = """
INSERT INTO iris_staging.harvest_cursor
    (source_system, publication_id, last_issue_year, last_issue_number, last_published_on)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (source_system, publication_id) DO UPDATE SET
    last_issue_year = EXCLUDED.last_issue_year,
    last_issue_number = EXCLUDED.last_issue_number,
    last_published_on = EXCLUDED.last_published_on,
    updated_at = now()
WHERE (harvest_cursor.last_issue_year, harvest_cursor.last_issue_number)
    < (EXCLUDED.last_issue_year, EXCLUDED.last_issue_number)
"""


class LoadError(Exception):
    """A title's records could not be written; its transaction was rolled back."""


@dataclass
class LoadCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def add(self, other: "LoadCounts") -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged


@dataclass(frozen=True)
class Cursor:
    issue_year: int
    issue_number: int
    published_on: date | None = None

    @property
    def key(self) -> tuple[int, int]:
        return (self.issue_year, self.issue_number)


class Store:
    def __init__(self, dsn: str):
        # Autocommit, so every conn.transaction() block is a real transaction
        # (not a savepoint inside an implicit one) and commits on exit.
        self.conn = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        with self.conn.transaction():
            self.conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))

    # -- runs -----------------------------------------------------------------

    def start_run(self, run_id: UUID, source_system: str, config: dict) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO iris_staging.ingest_run (run_id, source_system, status, config) VALUES (%s, %s, 'running', %s)",
                (run_id, source_system, Jsonb(config)),
            )

    def finish_run(self, run_id: UUID, status: str, stats: dict) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE iris_staging.ingest_run SET status = %s, finished_at = now(), stats = %s WHERE run_id = %s",
                (status, Jsonb(json.loads(json.dumps(stats, default=str))), run_id),
            )

    def cursors(self, source_system: str) -> dict[str, Cursor]:
        rows = self.conn.execute(
            "SELECT publication_id, last_issue_year, last_issue_number, last_published_on "
            "FROM iris_staging.harvest_cursor WHERE source_system = %s",
            (source_system,),
        ).fetchall()
        return {pid: Cursor(year, nr, published) for pid, year, nr, published in rows}

    # -- loading --------------------------------------------------------------

    def load_title(
        self,
        run_id: UUID,
        source_system: str,
        publication_id: str,
        records: list[SignalRecord],
        errors: list[ErrorRecord],
        cursor: Cursor | None,
    ) -> LoadCounts:
        """Records, errors and cursor for one title, atomically."""
        counts = LoadCounts()
        try:
            self._load_title(run_id, source_system, publication_id, records, errors, cursor, counts)
        except psycopg.Error as exc:
            raise LoadError(f"{type(exc).__name__}: {exc}".strip()) from exc
        return counts

    def _load_title(self, run_id, source_system, publication_id, records, errors, cursor, counts) -> None:
        with self.conn.transaction():
            if records:
                self.conn.execute(_STAGE_TABLE)
                with self.conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO tmp_signal_ingest ({', '.join(COLUMNS)}, x, y) "
                        f"VALUES ({', '.join(['%s'] * (len(COLUMNS) + 2))})",
                        [self._row(r) for r in records],
                    )
                    cur.execute(_UPSERT, {"run_id": run_id})
                    counts.inserted, counts.updated = cur.fetchone()
                    cur.execute(_TOUCH, {"run_id": run_id})
                counts.unchanged = len(records) - counts.inserted - counts.updated
            self._insert_errors(run_id, source_system, errors)
            if cursor is not None:
                self.conn.execute(
                    _CURSOR,
                    (source_system, publication_id, cursor.issue_year, cursor.issue_number, cursor.published_on),
                )

    def record_errors(self, run_id: UUID, source_system: str, errors: list[ErrorRecord]) -> None:
        with self.conn.transaction():
            self._insert_errors(run_id, source_system, errors)

    def _insert_errors(self, run_id: UUID, source_system: str, errors: list[ErrorRecord]) -> None:
        if not errors:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO iris_staging.signal_ingest_error "
                "(run_id, source_system, stage, severity, code, message, source_url, source_record_id, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        run_id,
                        source_system,
                        e.stage,
                        e.severity,
                        e.code,
                        e.message,
                        e.source_url,
                        e.source_record_id,
                        Jsonb(e.payload) if e.payload is not None else None,
                    )
                    for e in errors
                ],
            )

    @staticmethod
    def _row(record: SignalRecord) -> tuple:
        values = []
        for column in COLUMNS:
            value = getattr(record, column)
            values.append(Jsonb(value) if column == "raw" else value)
        return (*values, record.x, record.y)
