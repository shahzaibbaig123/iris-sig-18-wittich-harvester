-- IRIS-SIG-18 validation queries. Each query returns the rows that VIOLATE
-- its rule, so an empty result means the check passed.
-- Run all of them:   uv run --env-file .env wittich-harvester validate
-- Or with psql:      docker compose exec -T db psql -U iris -d iris < sql/validation_queries.sql

-- check: duplicate_source_keys
SELECT source_system, source_record_id, count(*)
FROM iris_staging.signal_ingest
GROUP BY 1, 2
HAVING count(*) > 1;

-- check: geometry_srid_validity_envelope
SELECT source_record_id, ST_SRID(geom), ST_AsText(geom)
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND (ST_SRID(geom) <> 25832
       OR NOT ST_IsValid(geom)
       OR GeometryType(geom) <> 'POINT'
       OR ST_X(geom) NOT BETWEEN 250000 AND 950000
       OR ST_Y(geom) NOT BETWEEN 5200000 AND 6150000);

-- check: geometry_matches_geo_status
SELECT source_record_id, geo_status, geom IS NOT NULL AS has_geom
FROM iris_staging.signal_ingest
WHERE (geo_status IN ('resolved', 'fallback_publication_area')) <> (geom IS NOT NULL);

-- check: point_outside_its_state
-- Per-state boxes in EPSG:25832: extent of the state's gazetteer points plus
-- 5 km, the same rule the harvester applies before storing a point.
WITH box(state_code, minx, miny, maxx, maxy) AS (
    VALUES ('RP', 290000, 5420000, 468000, 5644000),
           ('HE', 410000, 5469000, 587000, 5726000),
           ('TH', 560000, 5561000, 759000, 5723000),
           ('SN', 704000, 5563000, 923000, 5735000)
)
SELECT s.source_record_id, s.state_code, ST_X(s.geom), ST_Y(s.geom)
FROM iris_staging.signal_ingest s
JOIN box b USING (state_code)
WHERE s.geom IS NOT NULL
  AND NOT (ST_X(s.geom) BETWEEN b.minx AND b.maxx AND ST_Y(s.geom) BETWEEN b.miny AND b.maxy);

-- check: ars_not_in_state
SELECT source_record_id, state_code, ars
FROM iris_staging.signal_ingest
WHERE ars IS NOT NULL
  AND left(ars, 2) <> CASE state_code WHEN 'RP' THEN '07' WHEN 'HE' THEN '06' WHEN 'TH' THEN '16' WHEN 'SN' THEN '14' END;

-- check: required_fields_missing
SELECT source_record_id
FROM iris_staging.signal_ingest
WHERE coalesce(trim(title), '') = ''
   OR coalesce(trim(source_url), '') = ''
   OR source_url !~ '^https://ol\.wittich\.de/titel/[0-9]+/ausgabe/[0-9]+/[0-9]{4}/artikel/'
   OR signal_type <> 'municipal_notice';

-- check: missing_date_not_flagged
SELECT source_record_id
FROM iris_staging.signal_ingest
WHERE published_on IS NULL AND NOT ('missing_published_on' = ANY (quality_flags));

-- check: published_on_implausible
SELECT source_record_id, published_on, issue_year
FROM iris_staging.signal_ingest
WHERE published_on IS NOT NULL
  AND (published_on > current_date + 60 OR abs(extract(year FROM published_on) - issue_year) > 1);

-- check: content_hash_malformed
SELECT source_record_id, content_hash
FROM iris_staging.signal_ingest
WHERE content_hash !~ '^[0-9a-f]{64}$';

-- check: runs_left_running
SELECT run_id, started_at
FROM iris_staging.ingest_run
WHERE status = 'running' AND started_at < now() - interval '6 hours';

-- check: unresolved_locations_without_note
SELECT source_record_id, geo_status
FROM iris_staging.signal_ingest
WHERE geo_status IN ('ambiguous', 'unresolved', 'invalid') AND coalesce(geo_note, '') = '';
