-- IRIS-SIG-18 staging schema for PostgreSQL 16 / PostGIS 3.4.
-- Every statement is idempotent, so `wittich-harvester init-db` can re-run it.
-- The brief names iris_staging.signal_ingest but does not define it; this is
-- the proposed DDL (see README, "Assumptions").

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris_staging;

CREATE TABLE IF NOT EXISTS iris_staging.ingest_run (
    run_id          uuid PRIMARY KEY,
    source_system   text        NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text        NOT NULL
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    config          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    stats           jsonb
);

CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Traceability: stable source key and where it came from.
    source_system       text NOT NULL,
    source_record_id    text NOT NULL,
    source_url          text NOT NULL,

    -- Signal
    signal_type         text NOT NULL,
    notice_category     text,
    title               text NOT NULL,
    summary             text,
    body_text           text,
    published_on        date,           -- from the source; NULL is flagged, never guessed
    section             text,

    -- Publication context
    publication_id      text NOT NULL,
    publication_title   text,
    issue_number        integer,
    issue_year          integer,
    issue_url           text,

    -- Geography
    state_code          char(2) NOT NULL CHECK (state_code IN ('RP', 'HE', 'TH', 'SN')),
    ars                 text CHECK (ars ~ '^[0-9]{2}([0-9]{3}([0-9]{4}([0-9]{3})?)?)?$'),
    municipality_name   text,
    place_text          text,
    geom                geometry(Point, 25832),
    geo_precision       text CHECK (geo_precision IN
                            ('municipality', 'municipal_association', 'district')),
    geo_status          text NOT NULL CHECK (geo_status IN
                            ('resolved', 'fallback_publication_area', 'ambiguous',
                             'unresolved', 'invalid')),
    geo_note            text,

    -- Quality and change detection
    quality_flags       text[] NOT NULL DEFAULT '{}',
    content_hash        char(64) NOT NULL,
    raw                 jsonb NOT NULL,

    first_seen_run_id   uuid NOT NULL REFERENCES iris_staging.ingest_run (run_id),
    last_seen_run_id    uuid NOT NULL REFERENCES iris_staging.ingest_run (run_id),
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_signal_ingest_source UNIQUE (source_system, source_record_id),

    -- Invalid coordinates are rejected, never stored silently.
    CONSTRAINT ck_signal_ingest_geom CHECK (
        geom IS NULL OR (
            ST_SRID(geom) = 25832
            AND ST_IsValid(geom)
            AND ST_X(geom) BETWEEN 250000 AND 950000
            AND ST_Y(geom) BETWEEN 5200000 AND 6150000
        )
    ),
    -- A point exists exactly when the location was resolved (directly or by
    -- falling back to the publication area).
    CONSTRAINT ck_signal_ingest_geo_status CHECK (
        (geo_status IN ('resolved', 'fallback_publication_area')) = (geom IS NOT NULL)
    ),
    CONSTRAINT ck_signal_ingest_precision CHECK (
        (geom IS NULL) = (geo_precision IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_geom
    ON iris_staging.signal_ingest USING gist (geom);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_published_on
    ON iris_staging.signal_ingest (published_on);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_state
    ON iris_staging.signal_ingest (state_code, signal_type);
CREATE INDEX IF NOT EXISTS ix_signal_ingest_issue
    ON iris_staging.signal_ingest (source_system, publication_id, issue_year, issue_number);

CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest_error (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              uuid NOT NULL REFERENCES iris_staging.ingest_run (run_id),
    source_system       text NOT NULL,
    stage               text NOT NULL CHECK (stage IN
                            ('robots', 'fetch', 'parse', 'validate', 'geo', 'load')),
    severity            text NOT NULL CHECK (severity IN ('error', 'warning')),
    code                text NOT NULL,
    message             text NOT NULL,
    source_url          text,
    source_record_id    text,
    payload             jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_error_run
    ON iris_staging.signal_ingest_error (run_id, stage, code);

-- Incremental retrieval: the newest issue fully processed per publication.
CREATE TABLE IF NOT EXISTS iris_staging.harvest_cursor (
    source_system       text NOT NULL,
    publication_id      text NOT NULL,
    last_issue_year     integer NOT NULL,
    last_issue_number   integer NOT NULL,
    last_published_on   date,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, publication_id)
);
