# IRIS-SIG-18: LINUS WITTICH official gazette harvester

Harvests municipal notices (*amtliche Bekanntmachungen*) from the LINUS WITTICH
gazettes for Rheinland-Pfalz, Hessen, Thüringen and Sachsen. They are
normalized to EPSG:25832 point records and loaded idempotently into
`iris_staging.signal_ingest` (PostgreSQL 16 / PostGIS 3.4). It implements
integration pattern B in both forms: **HTML** from the online edition, and
**PDF** from the e-paper for titles that have no online edition.

```sh
uv sync
uv run pytest                                  # 97 offline tests (13 integration tests skip without a DB)
uv run wittich-harvester run --offline --no-db # full pipeline over the fixtures, no network, no DB
```

## 1. Source verification (2026-10-28)

| Endpoint | Finding | Used |
|---|---|---|
| `https://wittich.de/amtsblaetter` (from the brief) | **HTTP 404**. The endpoint is obsolete. | no |
| `www.wittich.de` | Marketing site. Its robots.txt has `Disallow: /*?*` for all agents, so the site search and the query-string sitemaps are off limits. | no |
| **`ol.wittich.de`** ("Onlinelesen") | The official online edition: one server-rendered HTML page per article, with paginated tables of contents | **yes**, HTML route |
| **`epaper.wittich.de`** | Page-flip e-paper; robots.txt is empty. Its viewer config states whether readers may download the complete issue as PDF. | **yes**, PDF route, only where the download is offered |

**HTML route** (`ol.wittich.de`). All requests are plain GET requests on
path-only URLs:

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
  the place label, headline and teaser per article, and earlier issues back to
  2022.
- `/titel/{id}/ausgabe/{nr}/{year}/artikel/{articleId}` is the article. Each
  article page also embeds a JavaScript **manifest of the whole issue**:
  `{id, date, caption, rubric}` for every article.

**PDF route** (`epaper.wittich.de`). For a title with no online edition, the
viewer page `/frontend/mvc/catalog/by-name/{id}/newest` names the issue
(`1391_22_2026`). Its config holds `save_complete_enabled` and the
complete-issue PDF URL (`/frontend/catalogs/{catalog}/{n}/pdf/complete.pdf`).
The PDF is downloaded **only** when `save_complete_enabled` is true and the
catalogue is not a `/preview/` one. That is exactly when the viewer shows
every reader the "save complete issue" button; the PDF is then served as a
normal attachment. Otherwise the title is reported as `download_disabled` and
nothing is fetched. Of 14 e-paper-only titles checked on 2026-10-28:
- 8 offered the download;
- 2 newest issues (HE 1329, 1353) were previews with downloads disabled;
- 4 titles had no e-paper.

**Publication dates** are verified from the source, never derived from the
issue number:
- **HTML:** the per-article manifest date. Within one issue dates differ:
  issue 43/2026 of title 130 has items dated 22.10. and 23.10., and title 1740
  dates its issue on a Sunday (25.10.).
- **PDF:** the masthead ("Freitag, den 30. Oktober 2026 … Nummer 22"). This is
  an issue-level date, flagged `issue_level_date`.

A missing date stays `NULL` and is flagged.

**robots.txt.** `ol.wittich.de` does not block all agents (its
`User-agent: * / Disallow: /` lines are commented out), but it lists about
1,000 named bots. Among them are `Harvest` and `es`. Under the conservative
substring matching of the original robots.txt convention (also used by
Python's `urllib.robotparser`), a crawler called "…Harvester" or
`python-requests` would be refused. The harvester therefore identifies as
`IRIS-GazetteBot/0.1 (+mailto:$WITTICH_CONTACT)`, a name that matches no
listed agent. It reads robots.txt at runtime for every host with that strict
matching, and stops for any host that blocks it. It also honours
`Crawl-delay`, keeps at least 1 s between requests to a host, and backs off on
429 and 5xx responses.

**Coverage found.** A full live title search of all four states ran on
2026-10-28 (`sample_output/catalogue_summary.json`):
- 283 requests (279 postcode queries) found 1,490 titles.
- **741 are in scope: RP 155, HE 264, TH 155, SN 167.** The rest are
  out-of-state titles, app feeds, special prints and the like.
- The overview cross-check found 117 of the 119 SN/TH overview titles in the
  sweep.

Many RP, TH and SN gazettes have an online edition; some have none (e.g. RP
183 Wörth). **None of the 20 Hessian titles probed has an online edition**,
so Hessen comes through the PDF route.

## 2. How it works

```
discover ─► titles in scope ─► newest issue? ─┬─ yes: issues to read (cursor) ─► TOC pages ─► manifest
                                              │        classify every TOC entry ─► fetch notices only
                                              └─ no:  e-paper viewer ─► download offered? ─► complete PDF
                                                       segment by typography ─► classify every block
                                    ─► normalize ─► geo ─► load (one transaction per title)
```

| Module | Role |
|---|---|
| `http.py`, `transport.py` | robots.txt, throttling, retries; `FixtureTransport` for offline use |
| `parsers/catalogue.py`, `issue.py`, `article.py` | search results, overview, TOC, article and manifest |
| `parsers/epaper.py` | viewer config and PDF segmentation |
| `catalogue.py` | Postcode sweep with adaptive narrowing, overview merge, title scope |
| `filters.py` | Title scope (state, product type) and notice classification, each with a reason |
| `geo.py` | VG250 gazetteer lookup and coordinate validation |
| `normalize.py` | `SignalRecord`, content hash, quality flags, in-batch de-duplication |
| `load.py` | Upsert, errors and cursor in one transaction per title |
| `pipeline.py`, `report.py`, `cli.py` | Orchestration, quality report, commands |

All parsers raise `ParseError` on markup they don't recognize, so nothing fails
silently.

**PDF segmentation.** A PDF has no article markup, so blocks are found from
the typography of the issue's editorial font family (the family carrying most
of the text):
- text at 1.6× the page's body size or more is a **section heading** (e.g.
  "Politik", "Vereine und Verbände");
- text at 1.2× or more is a **headline** (consecutive lines are joined);
- body-size text is the **block body**, which continues across pages.

Advertisements set in other font families are skipped, and running headers and
soft hyphens are cleaned. On a real 44-page Hessian issue this gave 99 blocks
in their sections, and exactly one notice: the Bekanntmachung of a
Stadtverordnetenversammlung meeting with its agenda.

All tunable rules live in `config/harvester.toml`: states, postcode prefixes,
rate limits, issue window, PDF size limit and font ratios, and the section,
headline, cue and category patterns. Secrets and the operator contact come
only from the environment.

## 3. Mapping and assumptions

`sql/001_schema.sql` holds the proposed DDL for `iris_staging.signal_ingest`;
the brief names the table but does not define it. Supporting tables are
`ingest_run`, `signal_ingest_error` and `harvest_cursor`.

| Column | HTML route | PDF route |
|---|---|---|
| `source_system` | `wittich_ol` | `wittich_epaper` |
| `source_record_id` | article id, e.g. `00000000000055313196-OL-130-2026-43-43-0` | `{title}-{year}-{issue}-p{page}-{hash of page and headline}`, e.g. `1391-2026-22-p016-8e3e35890f` |
| `source_url` | the article URL on `ol.wittich.de` | complete-issue PDF URL `#page=N` |
| `signal_type` | constant `municipal_notice` | same |
| `notice_category` | first category pattern matching the headline, else headline + text: `sitzung`, `bauleitplanung`, `satzung_haushalt`, `wahl`, `vergabe_stelle`, `verkehr`, `zustellung_justiz`, `umwelt_sicherheit`, or `general` | same |
| `title` | article `<h1>`, falling back to the TOC headline, then the manifest caption | block headline |
| `summary` / `body_text` | TOC teaser / article text | first 300 characters / block text |
| `published_on` | manifest date for the article | masthead issue date (flagged `issue_level_date`) |
| `section` | manifest `rubric`, else the article's `article:section` | the PDF section heading in force |
| `publication_*`, `issue_*` | title id and name; issue from the TOC | title id and name; issue from the viewer |
| `state_code`, `ars`, `municipality_name`, `place_text` | see geography below | same |
| `geom` | `POINT` in EPSG:25832; `geo_precision`, `geo_status` and `geo_note` say how it was obtained | same |
| `quality_flags` | e.g. `missing_published_on`, `published_on_outlier`, `missing_body`, `headline_mismatch`, `geo_<status>` | plus `issue_level_date`, `empty_body` |
| `content_hash` | SHA-256 over the normalized content fields (not run ids or timestamps) | same |
| `raw` | TOC entry, manifest entry, classification reason, geo candidates | page, masthead, document title, classification reason |

`source_record_id` is unique together with `source_system`.

**Geographic scope.** A title's state comes from the first two digits of its
ARS (07 RP, 06 HE, 16 TH, 14 SN). The "… - Rheinland-Pfalz" label is only a
cross-check; a contradiction excludes the title. Non-gazette products are
excluded by name and frequency: app feeds (`meinOrt`), special prints (`SD …`),
classifieds, guides, yearly products.

**Domain scope: what counts as a notice.** Each TOC entry or PDF block is
classified from its section, headline and text:
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
   count in the headline or text. Authority names (*Amtsgericht*,
   *Kreisverwaltung*, *Zweckverband*, …) count in the headline only. A headline
   matching a category (e.g. *Gemeinderat*, *Vollsperrung*) also counts.
   Mixed sections such as "Gemeinsame Informationen und Bekanntmachungen"
   are treated this way, not as official.

On the HTML route only notices are fetched in full. The manifest is read from
the entry most likely to be a notice, so it rarely costs an extra request.
Every decision's reason is kept in `raw` and summarized in the quality report.

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
   `geo_status = 'fallback_publication_area'` and a note explaining why. PDF
   blocks usually end up here, because the PDF has no place labels.

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
municipality level at best.

## 4. Repeatability, incremental retrieval and failure handling

- **Idempotent load.** Upsert on `(source_system, source_record_id)`. An
  unchanged `content_hash` only moves `last_seen_*`; a changed one updates the
  row; a re-run inserts nothing. Records, error rows and the title's cursor
  are written in **one transaction per title**.
- **Incremental retrieval.** `harvest_cursor` stores the newest fully
  processed issue per title.
  - HTML route: the next run reads newer issues, plus `overlap_issues`
    (default 1) already-seen issues to catch late corrections, capped at
    `max_issues_per_title`; skipped backlog is reported.
  - PDF route: an issue already harvested is not downloaded again
    (`up_to_date`) unless `reread_same_issue` is set.
  - The cursor only advances through consecutive successful issues and never
    moves backwards.
- **Pagination.** TOC pages are read until the advertised count is listed, or
  a page is empty. Duplicates across pages (real: titles 104, 130 and 1740
  list an article twice) and count mismatches become warnings.
- **Network failures.** Timeouts, connection errors and 5xx responses are
  retried with exponential backoff; 429 respects `Retry-After`. Other 4xx
  responses are not retried. robots.txt with a 5xx response or unreachable
  means the host is skipped; a 4xx response means no rules. PDFs larger than
  `max_pdf_mb` are refused.
- **Malformed or empty data.** Unknown markup or a broken PDF raises
  `ParseError`, which is logged. An empty search fragment means no hits. `{}`
  from the newest-issue endpoint sends the title to the PDF route, and a 404
  there means there is no e-paper either. A notice whose page fails is still
  stored with `missing_body`. A missing manifest or masthead date leaves dates
  `NULL` and flagged.
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
| `wittich-harvester discover` | Builds and caches the title catalogue for all four states (about 280 requests) |
| `wittich-harvester run` | Full live harvest: discover, harvest, load |
| `wittich-harvester run --titles 130,1391 --issues-per-title 1` | Bounded live run |
| `wittich-harvester validate` | Runs `sql/validation_queries.sql`; non-zero exit on violations |
| `wittich-harvester export --out x.csv [--jsonl run/signals.jsonl]` | CSV without body text, from the DB or a run |

Prefix commands with `uv run --env-file .env`. Every run writes
`signals.jsonl`, `errors.jsonl` and `quality_report.{md,json}`. With a
database, the run, its errors and its statistics are also stored in
`iris_staging`.

## 6. Tests

```sh
uv run pytest                    # 97 offline tests
docker compose up -d db
IRIS_TEST_DATABASE_URL=postgresql://iris:<password>@localhost:5434/iris uv run pytest -m integration   # 13 tests
```

Both suites passed on 2026-10-28, the integration suite against
`postgis/postgis:16-3.4`.

The HTML fixtures are trimmed, pseudonymized captures of real pages
(`tests/fixtures/SOURCE.md`). The e-paper viewer pages are real captures. The
test PDF is synthetic, built in `tests/test_epaper.py` with a real issue's
typography, so no copyrighted or personal content is committed.

Offline tests cover:
- parsing, including malformed and empty pages;
- robots matching against the real files, including the `Harvest` and `es`
  groups;
- retries, backoff, `Retry-After` and throttling, on a fake clock;
- scope and notice classification, including regressions found in live runs;
- geocoding, including ambiguous names, swapped axes and points outside the
  state;
- hash stability and de-duplication;
- PDF segmentation: sections, multi-line headlines, page continuations, ads,
  running headers, hyphenation and per-page body size;
- the respected download switch;
- the end-to-end pipeline with injected 404, 500, malformed JSON, a missing
  manifest, a short TOC, a broken PDF and a robots block.

The integration tests create a throwaway database. They check that:
- the schema applies twice;
- **a re-run gives 0 inserted, 0 updated and 20 unchanged, and still 20 rows**;
- a changed article gives 1 update;
- duplicates within a run load once;
- the CHECK constraints reject invalid geometry and a wrong SRID;
- the cursor never moves backwards;
- a failed load rolls back the whole title;
- all validation queries return no rows.

## 7. Sample output (`sample_output/`)

Two live runs on 2026-10-28 used 11 titles, newest issue each, loaded into
PostGIS:
- RP 130, 104, 183;
- TH 1740, 1691, 1803;
- SN 2701, 2600;
- HE 1391, 1329, 1353.

| File | Content |
|---|---|
| `signal_ingest_sample.csv` | The 49 loaded notices (RP 9, TH 13, SN 26, HE 1 from the PDF route), exported from the database without body text. Every row has a date and a point. |
| `quality_report.md` / `.json`, `errors.jsonl` | Run 1: 86 requests, **49 inserted**. RP 183's PDF (an 8-page community paper) held no notices. HE 1329 and 1353 were `download_disabled`. |
| `rerun_quality_report.md` | Run 2, same command: 84 requests, **0 inserted, 0 updated, 48 unchanged**. The PDF titles were `up_to_date`. |
| `validation_results.txt` | `wittich-harvester validate` after both runs: all 11 checks pass |
| `catalogue_summary.json` | The full title search described in §1 |

## 8. Known limitations

- **Hessen and other e-paper-only titles** are covered only where the
  publisher offers the complete-issue download. Issues published as previews
  are skipped and retried on the next run.
- **PDF segmentation is typographic.** A notice can be split or merged when a
  layout departs from the usual heading styles. PDF dates are issue-level, and
  PDF locations are usually the publication area.
- **Bandwidth.** PDFs are 4–15 MB each. A weekly run over all e-paper-only
  titles means roughly 1–2 GB of downloads (estimated, not measured).
- **Undocumented endpoints.** `ol.wittich.de/internal/...` and the e-paper
  viewer config are the sites' own UI endpoints. They are robots-allowed and
  fixture-tested, but undocumented, so changes surface as `parse` errors, not
  silent gaps.
- **Discovery.** Postcode results are capped, so saturated prefixes are
  narrowed. In the full run, one 4-digit prefix (3616) still returned the
  threshold of 40 titles and is reported. The overview cross-check covers SN
  and TH only.
- **Classification is rule-based.** It was tuned on the 11 titles of the
  live runs. Every
  decision is traceable through `raw.decision`, but expect some misses and
  false positives in sections without an official marker.
- **Location** is municipality level at best, from the gazetteer as of
  01.01.2026. Ortsteile fall back to their municipality.
- **Personal data.** Official notices can name private persons (court
  auctions, building applications). Downstream retention rules apply.
- **Run time.** The live runs measured about 8 requests per title at 1 req/s,
  so a full weekly run of the 741 in-scope titles takes about 1.5–2 hours plus
  PDF transfer (extrapolated).
