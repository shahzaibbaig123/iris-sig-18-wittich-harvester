"""Command line: wittich-harvester init-db | discover | run | validate | export."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from wittich_harvester.catalogue import Catalogue, CatalogueTitle, discover
from wittich_harvester.config import PROJECT_ROOT, Settings, contact_configured, load_settings
from wittich_harvester.geo import Gazetteer, GeoResolver
from wittich_harvester.http import PoliteClient
from wittich_harvester.transport import FixtureTransport, RequestsTransport

log = logging.getLogger("wittich_harvester")

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
OFFLINE_ROUTES = FIXTURES / "routes.json"
OFFLINE_OVERLAY = FIXTURES / "offline.toml"
DATA_DIR = PROJECT_ROOT / "data"
VALIDATION_SQL = PROJECT_ROOT / "sql" / "validation_queries.sql"

EXPORT_COLUMNS = (
    "source_system", "source_record_id", "source_url", "signal_type", "notice_category", "title",
    "published_on", "section", "publication_id", "publication_title", "issue_number", "issue_year",
    "state_code", "ars", "municipality_name", "place_text", "geo_precision", "geo_status", "geo_note",
    "quality_flags", "content_hash",
)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wittich-harvester", description=__doc__)
    parser.add_argument("--config", type=Path, help="TOML config (default: config/harvester.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or update the iris_staging schema (idempotent)")

    def scope_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--offline", action="store_true", help="serve every request from tests/fixtures")
        p.add_argument("--states", type=_csv, help="comma-separated subset of RP,HE,TH,SN")
        p.add_argument("--prefixes", type=_csv, help="postcode prefixes to sweep instead of the configured ones")
        p.add_argument("--max-prefix-length", type=int, help="stop narrowing saturated prefixes at this length")

    p = sub.add_parser("discover", help="build the title catalogue and print a summary")
    scope_args(p)

    p = sub.add_parser("run", help="harvest notices and load them")
    scope_args(p)
    p.add_argument("--no-db", action="store_true", help="write JSONL and the report only")
    p.add_argument("--titles", type=_csv, help="only these title ids (must be in scope)")
    p.add_argument("--max-titles-per-state", type=int)
    p.add_argument("--issues-per-title", type=int, help="override initial_issues and max_issues_per_title")
    p.add_argument("--refresh-catalogue", action="store_true", help="ignore a cached catalogue")
    p.add_argument("--out", type=Path, help="output directory (default: data/runs/<run_id>)")

    sub.add_parser("validate", help="run sql/validation_queries.sql; non-zero exit on violations")

    p = sub.add_parser("export", help="write records (without body text) to CSV, from the database or a run's JSONL")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--jsonl", type=Path, help="read a run's signals.jsonl instead of the database")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2


class ConfigError(Exception):
    pass


# -- helpers ------------------------------------------------------------------


def _settings(args) -> Settings:
    settings = load_settings(
        args.config,
        states=getattr(args, "states", None),
        overlay=OFFLINE_OVERLAY if getattr(args, "offline", False) else None,
    )
    disc = settings.discovery
    if getattr(args, "max_prefix_length", None):
        disc = dataclasses.replace(disc, max_prefix_length=args.max_prefix_length)
    harvest = settings.harvest
    if getattr(args, "issues_per_title", None):
        n = args.issues_per_title
        harvest = dataclasses.replace(harvest, initial_issues=n, max_issues_per_title=max(n, harvest.max_issues_per_title))
    return dataclasses.replace(settings, discovery=disc, harvest=harvest)


def _client(settings: Settings, offline: bool) -> PoliteClient:
    if offline:
        return PoliteClient(settings.http, FixtureTransport.from_manifest(OFFLINE_ROUTES), sleep=lambda _s: None)
    if not contact_configured():
        log.warning("WITTICH_CONTACT is not set; the User-Agent carries no operator contact")
    return PoliteClient(settings.http, RequestsTransport())


def _store(settings: Settings):
    if not settings.database_url:
        raise ConfigError("DATABASE_URL is not set (see .env.example), or pass --no-db")
    from wittich_harvester.load import Store

    return Store(settings.database_url)


def _catalogue(args, settings: Settings, client: PoliteClient) -> Catalogue:
    cache = DATA_DIR / ("catalogue.offline.json" if args.offline else "catalogue.json")
    custom = bool(args.prefixes or args.max_prefix_length or args.states)
    if not args.offline and not custom and not getattr(args, "refresh_catalogue", False) and cache.exists():
        catalogue = Catalogue.load(cache)
        if catalogue.age_days(datetime.now(timezone.utc)) <= settings.discovery.catalogue_max_age_days:
            log.info("using cached catalogue from %s (%s)", catalogue.created_at, cache)
            return catalogue
    catalogue = discover(client, settings, prefixes=args.prefixes)
    if not custom:
        catalogue.save(cache)
    return catalogue


def _select(catalogue: Catalogue, args, settings: Settings) -> list[CatalogueTitle]:
    titles = catalogue.in_scope()
    if args.titles or settings.harvest.titles:
        wanted = set(args.titles or settings.harvest.titles)
        known = {t.title_id for t in titles}
        missing = wanted - known
        if missing:
            log.warning("titles not in scope or not discovered, skipped: %s", ", ".join(sorted(missing)))
        titles = [t for t in titles if t.title_id in wanted]
    if args.max_titles_per_state:
        per_state: Counter = Counter()
        chosen = []
        for title in titles:
            if per_state[title.state_code] < args.max_titles_per_state:
                per_state[title.state_code] += 1
                chosen.append(title)
        titles = chosen
    return titles


# -- commands -----------------------------------------------------------------


def cmd_init_db(args) -> int:
    store = _store(load_settings(args.config))
    try:
        store.init_schema()
    finally:
        store.close()
    log.info("schema iris_staging is up to date")
    return 0


def cmd_discover(args) -> int:
    settings = _settings(args)
    client = _client(settings, args.offline)
    args.refresh_catalogue = True
    catalogue = _catalogue(args, settings, client)
    in_scope = catalogue.in_scope()
    print(f"titles found: {len(catalogue.titles)}, in scope: {len(in_scope)} ({dict(Counter(t.state_code for t in in_scope))})")
    print(f"excluded: {dict(Counter(t.reason.split(':')[0] for t in catalogue.titles if not t.in_scope))}")
    print(f"postcode queries: {len(catalogue.prefixes_queried)}, saturated: {catalogue.saturated_prefixes}")
    print(f"overview cross-check: {catalogue.overview_found_by_sweep}/{catalogue.overview_in_scope}")
    print(f"errors: {len(catalogue.errors)}, HTTP requests: {client.request_count}")
    return 0


def cmd_run(args) -> int:
    from wittich_harvester.pipeline import Harvester
    from wittich_harvester.report import build_report, render_markdown, write_outputs

    settings = _settings(args)
    client = _client(settings, args.offline)
    store = None if args.no_db else _store(settings)
    gazetteer = Gazetteer.load(settings.geo.gazetteer_path)
    run_id = uuid.uuid4()
    mode = "offline" if args.offline else "live"
    try:
        catalogue = _catalogue(args, settings, client)
        titles = _select(catalogue, args, settings)
        log.info("harvesting %d titles", len(titles))
        cursors = {}
        if store is not None:
            cursors = store.cursors(settings.source_system)
            store.start_run(
                run_id,
                settings.source_system,
                {
                    "mode": mode,
                    "states": list(settings.states),
                    "titles": [t.title_id for t in titles],
                    "harvest": dataclasses.asdict(settings.harvest),
                },
            )
            if catalogue.errors:
                store.record_errors(run_id, settings.source_system, catalogue.errors)
        harvester = Harvester(settings, client, GeoResolver(gazetteer, settings.geo), store)
        result = harvester.run(titles, cursors, run_id=run_id)
        report = build_report(result, catalogue, mode)
        out_dir = args.out or DATA_DIR / "runs" / str(run_id)
        write_outputs(result, report, out_dir)
        if store is not None:
            store.finish_run(run_id, result.status, {k: v for k, v in report.items() if k != "titles"} | {"titles": report["titles"]["by_status"]})
    except BaseException:
        if store is not None:
            try:
                store.finish_run(run_id, "failed", {"error": "aborted"})
            except Exception:  # the run row may not exist yet
                pass
        raise
    finally:
        if store is not None:
            store.close()

    print(render_markdown(report).split("\n## Titles")[0])
    print(f"outputs: {out_dir}")
    return 1 if result.status == "failed" else 0


def cmd_validate(args) -> int:
    settings = load_settings(args.config)
    store = _store(settings)
    failed = 0
    try:
        for name, sql in _queries(VALIDATION_SQL.read_text(encoding="utf-8")):
            rows = store.conn.execute(sql).fetchall()
            status = "ok" if not rows else f"{len(rows)} VIOLATION(S)"
            print(f"{name:45} {status}")
            for row in rows[:5]:
                print(f"    {row}")
            failed += bool(rows)
    finally:
        store.close()
    return 1 if failed else 0


def _queries(text: str) -> list[tuple[str, str]]:
    """Split on '-- check: <name>' markers; each query returns violating rows."""
    parts = re.split(r"^-- check: (\S+).*$", text, flags=re.MULTILINE)
    return [(parts[i], parts[i + 1].strip().rstrip(";")) for i in range(1, len(parts) - 1, 2)]


def cmd_export(args) -> int:
    if args.jsonl:
        records = [json.loads(line) for line in args.jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
        records.sort(key=lambda r: (r["state_code"], r["publication_id"], r["issue_year"], r["issue_number"], r["source_record_id"]))
        columns = [*EXPORT_COLUMNS, "x_25832", "y_25832"]
        rows = [[r.get(c) for c in EXPORT_COLUMNS] + [r["x"], r["y"]] for r in records[: args.limit]]
        return _write_csv(args.out, columns, rows)
    settings = load_settings(args.config)
    store = _store(settings)
    try:
        cur = store.conn.execute(
            f"SELECT {', '.join(EXPORT_COLUMNS)}, round(ST_X(geom)::numeric, 1) AS x_25832, "
            "round(ST_Y(geom)::numeric, 1) AS y_25832 FROM iris_staging.signal_ingest "
            "ORDER BY state_code, publication_id, issue_year, issue_number, source_record_id LIMIT %s",
            (args.limit,),
        )
        rows = cur.fetchall()
        columns = [d.name for d in cur.description]
    finally:
        store.close()
    return _write_csv(args.out, columns, rows)


def _write_csv(path: Path, columns: list[str], rows: list) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for v in row])
    print(f"wrote {len(rows)} rows to {path}")
    return 0


COMMANDS = {
    "init-db": cmd_init_db,
    "discover": cmd_discover,
    "run": cmd_run,
    "validate": cmd_validate,
    "export": cmd_export,
}


if __name__ == "__main__":
    sys.exit(main())
