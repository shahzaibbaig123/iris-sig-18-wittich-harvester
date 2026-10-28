# IRIS-SIG-18: LINUS WITTICH official gazette harvester

Harvests municipal notices (*amtliche Bekanntmachungen*) from the LINUS WITTICH
online gazettes for Rheinland-Pfalz, Hessen, Thüringen and Sachsen. They are
normalized to EPSG:25832 point records and loaded idempotently into
`iris_staging.signal_ingest` (PostgreSQL 16 / PostGIS 3.4). It uses integration
pattern B (HTML).

```sh
uv sync
uv run pytest                                  # 86 offline tests (integration tests skip without a DB)
uv run wittich-harvester run --offline --no-db # full pipeline over the fixtures, no network, no DB
```

## 1. Source verification (2026-10-28)

| Endpoint | Finding | Used |
|---|---|---|
| `https://wittich.de/amtsblaetter` (from the brief) | **HTTP 404**. The endpoint is obsolete. | no |
| `www.wittich.de` | Marketing site. Its robots.txt has `Disallow: /*?*` for all agents, so the site search and the query-string sitemaps are off limits. | no |
| `epaper.wittich.de` | Page-flip e-paper. The viewer is JavaScript with no published PDF or text URL. We did not reverse-engineer it. | issue URL only |
| **`ol.wittich.de`** ("Onlinelesen") | The official online edition: one HTML page per article, with paginated tables of contents, server-rendered. | **yes** |

Endpoints used on `ol.wittich.de`. All are plain GET requests on path-only URLs:

- `/internal/typ/ZIP/begriff/{postcode prefix}` finds titles by postcode.
  Each result carries the title id, name, state label, frequency and the
  **ARS** (*Amtlicher Regionalschlüssel*) of the title's area. Results stop at
  about 50, so saturated prefixes are narrowed one digit at a time.
- `/uebersicht` lists all titles by state, but only BB, SN, ST and TH (199
  titles). It is used to add SN/TH titles the sweep missed, and as a
  completeness cross-check.
- `/internal/neuste/ausgabe/{id}` returns the newest online issue
  (`{"title":130,"year":2026,"issue":43}`), or `{}` when there is none.
- `/titel/{id}/ausgabe/{nr}/{year}/rubrik/alle/seite/{n}/sortiert/inhaltsverzeichnis`
  is the issue table of contents: 20 articles per page, an "N Artikel" count,
  the place label, headline and teaser per article, and the list of earlier
  issues back to 2022.
- `/titel/{id}/ausgabe/{nr}/{year}/artikel/{articleId}` is the article. Each
  article page also embeds a JavaScript **manifest of the whole issue**:
  `{id, date, caption, rubric}` for every article.

**Publication dates** come only from that manifest, per article, and are never
derived from the issue number. Within one issue they differ: issue 43/2026 of
title 130 has items dated 22.10. and 23.10., and title 1740 dates its issue on
a Sunday (25.10.). A missing date stays `NULL` and is flagged.

**robots.txt.** `ol.wittich.de` does not block all agents (its
`User-agent: * / Disallow: /` lines are commented out), but it lists about
1,000 named bots. Among them are `Harvest` and `es`. Under the conservative
substring matching of the original robots.txt convention (also used by
Python's `urllib.robotparser`), a crawler called "…Harvester" or
`python-requests` would be refused. The harvester therefore identifies as
`IRIS-GazetteBot/0.1 (+mailto:$WITTICH_CONTACT)`, a name that matches no
listed agent. It reads robots.txt at runtime with that same strict matching,
and stops for any host that blocks it. It also honours `Crawl-delay`, keeps at
least 1 s between requests to a host, and backs off on 429 and 5xx responses.

**Coverage found.** Many RP, TH and SN gazettes have current issues online,
but not all. Several titles probed have no online edition (e.g. RP 183 Wörth),
and some are stale (SN 3074: last issue 11/2025). Those are reported as
`no_online_edition` or stale. **None of the 20 Hessian titles probed (1329
Fuldatal among them) has an online edition**: Wittich publishes them as
e-paper only (see §8).

## 2. How it works

```
discover ─► titles in scope ─► newest issue ─► issues to read (cursor) ─► TOC pages
   │                                                                        │
   └ catalogue cache (7 days)            manifest (dates, sections) ◄───────┤
                                         classify every TOC entry ──────────┘
                                         fetch notices only ─► normalize ─► geo ─► load
```

| Module | Role |
|---|---|
| `http.py`, `transport.py` | robots.txt, throttling, retries; `FixtureTransport` for offline use |
| `parsers/` | search results, overview, TOC, article and manifest. They raise `ParseError` on markup they don't recognize. |
| `catalogue.py` | Postcode sweep with adaptive narrowing, overview merge, title scope |
| `filters.py` | Title scope (state, product type) and notice classification, each with a reason |
| `geo.py` | VG250 gazetteer lookup and coordinate validation |
| `normalize.py` | `SignalRecord`, content hash, quality flags, in-batch de-duplication |
| `load.py` | Upsert, errors and cursor in one transaction per title |
| `pipeline.py`, `report.py`, `cli.py` | Orchestration, quality report, commands |

All tunable rules live in `config/harvester.toml`: states, postcode prefixes,
rate limits, issue window, and the section, headline, cue and category
patterns. Secrets and the operator contact come only from the environment.

## 3. Mapping and assumptions

`sql/001_schema.sql` holds the proposed DDL for `iris_staging.signal_ingest`;
the brief names the table but does not define it. Supporting tables are
`ingest_run`, `signal_ingest_error` and `harvest_cursor`.

| Column | Source |
|---|---|
| `source_system` | constant `wittich_ol` |
| `source_record_id` | article id, e.g. `00000000000055313196-OL-130-2026-43-43-0` (unique together with `source_system`) |
| `source_url` | the article URL on `ol.wittich.de` |
| `signal_type` | constant `municipal_notice` |
| `notice_category` | first category pattern matching the headline, else headline + teaser: `sitzung`, `bauleitplanung`, `satzung_haushalt`, `wahl`, `vergabe_stelle`, `verkehr`, `zustellung_justiz`, `umwelt_sicherheit`, or `general` |
| `title` | article `<h1>`, falling back to the TOC headline, then the manifest caption |
| `summary` / `body_text` | TOC teaser / article text (paragraphs, list items and table rows) |
| `published_on` | manifest `date` for the article; `NULL` plus a flag if absent |
| `section` | manifest `rubric` (the gazette section), else the article's `article:section` |
| `publication_id`, `publication_title`, `issue_number`, `issue_year`, `issue_url` | title id, the title as printed on the article, issue from the TOC |
| `state_code`, `ars`, `municipality_name`, `place_text` | see geography below |
| `geom` | `POINT` in EPSG:25832; `geo_precision`, `geo_status` and `geo_note` say how it was obtained |
| `quality_flags` | e.g. `missing_published_on`, `published_on_outlier`, `missing_body`, `headline_mismatch`, `geo_<status>` |
| `content_hash` | SHA-256 over the normalized content fields (not run ids or timestamps) |
| `raw` | TOC entry, manifest entry, classification reason and geo candidates, for traceability |

**Geographic scope.** A title's state comes from the first two digits of its
ARS (07 RP, 06 HE, 16 TH, 14 SN). The "… - Rheinland-Pfalz" label is only a
cross-check; a contradiction excludes the title. Non-gazette products are
excluded by name and frequency: app feeds (`meinOrt`), special prints (`SD …`),
classifieds, guides, yearly products.

**Domain scope: what counts as a notice.** Each TOC entry is classified from
its manifest section, headline and teaser:
1. A headline such as "Redaktionsschluss", "Glückwünsche", "Sprechstunde" or
   "… e.V." is never a notice.
2. Sections such as "Nichtamtlicher Teil", "Kirchen …", "Vereine",
   "Volkshochschule" or "Wichtiges auf einen Blick" are never notices.
   Exception: a section named after a place ("Ortsgemeinde X", "Stadt X") is
   never matched against these patterns, because towns like *Kirchen (Sieg)*
   would collide.
3. Every entry in an official section ("Amtlicher Teil", "Amtliche
   Bekanntmachungen", "Andere Behörden") is a notice.
4. In any other section, an entry needs a cue. Legal-act words (*Bekannt­machung*,
   *Bekanntgabe*, *Satzung*, *öffentliche Sitzung*, *Allgemeinverfügung*)
   count in the headline or teaser. Authority names (*Amtsgericht*,
   *Kreisverwaltung*, *Zweckverband*, …) count in the headline only. A headline
   matching a category (e.g. *Gemeinderat*, *Vollsperrung*) also counts.
   Mixed sections such as "Gemeinsame Informationen und Bekanntmachungen"
   are treated this way, not as official.

Only notices are fetched in full. The manifest is read from the entry most
likely to be a notice, so it rarely costs an extra request. Every decision's
reason is kept in `raw` and summarized in the quality report.

**Geography.** The gazetteer is BKG VG250 as of 01.01.2026, reduced to
representative points of municipalities, Verbände and districts
(`data/gazetteer/`, attribution in `SOURCE.md`). Evidence is used most
specific first:
1. The section names a place ("Ortsgemeinde Hatzenbühl" → that municipality).
2. The TOC place label ("Probstzella", "VG Schiefergebirge"). A label that
   repeats the publication area, or is just "Verbandsgemeinde", means the whole
   area. A label listing several municipalities ("Probstzella, Lehesten und
   Gräfenthal") falls back to their common Verband.
3. Otherwise the publication area is used, with
   `geo_status = 'fallback_publication_area'` and a note explaining why.

Names are matched **only inside the publication's own Verband or district**:
Thüringen has two municipalities called Lehesten, and scoping picks the right
one. Wittich codes some titles with their seat municipality (title 1740, VG
Schiefergebirge, is coded as Probstzella). The publication area is therefore
the Verband containing that code when the Verband has several members; a
one-member Verband (Einheitsgemeinde) is the municipality itself. A name with
several candidates gets **no point** (`ambiguous`, candidates listed); an
unknown name with no area is `unresolved`. Every point is checked against
Germany's EPSG:25832 envelope and its state's extent. Failures become
`invalid` with `geom NULL`, a flag and an error row. The database enforces the
same rules with CHECK constraints: SRID 25832, `ST_IsValid`, envelope, and a
point if and only if the status is resolved or fallback. Precision is
municipality level at best; notices are not geocoded to street level.

## 4. Repeatability, incremental retrieval and failure handling

- **Idempotent load.** Upsert on `(source_system, source_record_id)`. An
  unchanged `content_hash` only moves `last_seen_*`; a changed one updates the
  row; a re-run inserts nothing. Records, error rows and the title's cursor
  are written in **one transaction per title**.
- **Incremental retrieval.** `harvest_cursor` stores the newest fully
  processed issue per title. The next run reads newer issues, plus
  `overlap_issues` (default 1) already-seen issues to catch late corrections,
  capped at `max_issues_per_title`; skipped backlog is reported. The cursor
  only advances through consecutive successful issues and never moves
  backwards.
- **Pagination.** TOC pages are read until the advertised count is listed, or
  a page is empty. Duplicates across pages (real: title 1740 lists one article
  twice) and count mismatches become warnings.
- **Network failures.** Timeouts, connection errors and 5xx responses are
  retried with exponential backoff; 429 respects `Retry-After`. Other 4xx
  responses are not retried. robots.txt with a 5xx response or unreachable
  means the host is skipped; a 4xx response means no rules.
- **Malformed or empty data.** Unknown markup raises `ParseError`, which is
  logged. An empty search fragment means no hits. `{}` from the newest-issue
  endpoint means no online edition. A notice whose page fails is still stored
  with `missing_body`. A missing manifest leaves dates `NULL` and flagged.
- **Isolation.** One failing title never stops the run. The run status is
  `succeeded`, `partial` or `failed`, and every error carries its stage
  (`robots`, `fetch`, `parse`, `validate`, `geo`, `load`), code, URL and
  record id.

## 5. Setup and running

Requirements: [uv](https://docs.astral.sh/uv/) (Python 3.12), plus Docker for
the database.

```sh
uv sync
cp .env.example .env                      # set POSTGRES_PASSWORD, DATABASE_URL and WITTICH_CONTACT
docker compose up -d db                   # postgis/postgis:16-3.4 on localhost:5434
uv run --env-file .env wittich-harvester init-db
```

| Command | What it does |
|---|---|
| `wittich-harvester run --offline` | Fixtures instead of the network (`tests/fixtures/routes.json`), loaded into the DB |
| `wittich-harvester run --offline --no-db` | The same, written only to `data/runs/<run_id>/` |
| `wittich-harvester discover` | Builds and caches the title catalogue for all four states |
| `wittich-harvester run` | Full live harvest: discover, harvest, load |
| `wittich-harvester run --titles 130,1740 --issues-per-title 1` | Bounded live run |
| `wittich-harvester validate` | Runs `sql/validation_queries.sql`; non-zero exit on violations |
| `wittich-harvester export --out x.csv [--jsonl run/signals.jsonl]` | CSV without body text, from the DB or a run |

Prefix commands with `uv run --env-file .env`. Every run writes
`signals.jsonl`, `errors.jsonl` and `quality_report.{md,json}`. With a
database, the run, its errors and its statistics are also stored in
`iris_staging`.

## 6. Tests

```sh
uv run pytest                    # offline: parsers, robots and HTTP, filters, geo, normalization, pipeline, discovery
docker compose up -d db
IRIS_TEST_DATABASE_URL=postgresql://iris:<password>@localhost:5434/iris uv run pytest -m integration
```

The fixtures are trimmed, pseudonymized captures of real pages; see
`tests/fixtures/SOURCE.md`. Offline tests cover:
- parsing, including malformed and empty pages;
- robots matching against the real files, including the `Harvest` and `es`
  groups;
- retries, backoff, `Retry-After` and throttling, on a fake clock;
- scope and notice classification, including regressions found in live runs;
- geocoding, including ambiguous names, swapped axes and points outside the
  state;
- hash stability and de-duplication;
- the end-to-end pipeline with injected 404, 500, malformed JSON, a missing
  manifest, a short TOC and a robots block.

The integration tests create a throwaway database on the given server. They
check that:
- the schema applies twice;
- **a re-run gives 0 inserted, 0 updated and 20 unchanged, and still 20 rows**;
- a changed article gives 1 update;
- duplicates within a run load once;
- the CHECK constraints reject invalid geometry and a wrong SRID;
- the cursor never moves backwards;
- a failed load rolls back the whole title;
- all validation queries return no rows.

## 7. Sample output (`sample_output/`)

A bounded live run on 2026-10-28 read 9 titles, newest issue each, with 82
requests at 1 req/s:

- RP 130, 183, 104; HE 1329; TH 1740, 1691, 1803; SN 2701, 2600.
- 293 articles listed, **48 notices**: SN 26, TH 13, RP 9.
- All 48 have a date, a body and a point: 42 at municipality level, 6 at
  Verband level.
- Two warnings, both genuine duplicate TOC entries.
- 183 (Wörth) and 1329 (Fuldatal) have no online edition.

Files:
- `signal_ingest_sample.csv`: those records, without body text.
- `quality_report.md` / `.json`: the run's error and quality report.
- `errors.jsonl`: the run's errors.

Notices are public official publications. The sample still leaves out body
text and teasers.

## 8. Known limitations

- **Hessen.** None of the 20 Hessian titles probed has an online edition;
  they exist only as e-paper, which offers no lawful machine-readable route. Hessian titles
  are discovered and reported as `no_online_edition`, but they yield no
  records. Wittich also covers only parts of Hessen.
- **Undocumented endpoints.** `/internal/...` are the site's own UI endpoints.
  They are robots-allowed and fixture-tested, but undocumented, so markup
  changes surface as `parse` errors, not silent gaps.
- **Discovery completeness can't be proven.** Postcode results are capped, so
  the sweep narrows saturated prefixes. The overview cross-check covers SN and
  TH only. Prefixes still saturated at `max_prefix_length` are reported.
- **Classification is rule-based.** Expect some misses and false positives in
  sections without an official marker; every decision is traceable through
  `raw.decision`. The rules were tuned on 9 titles.
- **Location** is municipality level at best, from the gazetteer as of
  01.01.2026. Ortsteile (e.g. Dresden's Ortschaften) fall back to the
  municipality.
- **Personal data.** Official notices can name private persons (court
  auctions, building applications). Downstream retention rules apply.
- **Run time.** A full weekly run means roughly 10 requests per title at
  1 req/s, i.e. one to two hours for all four states (not measured).
