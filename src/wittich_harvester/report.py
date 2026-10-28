"""Run outputs: signals.jsonl, errors.jsonl and a quality report (JSON + Markdown)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from wittich_harvester.catalogue import Catalogue
from wittich_harvester.pipeline import RunResult

LOCATION_SAMPLE = 25
ERROR_SAMPLE = 25


def build_report(result: RunResult, catalogue: Catalogue | None, mode: str) -> dict:
    records = result.records
    errors = result.errors
    published = sorted(r.published_on for r in records if r.published_on)
    report = {
        "run": {
            "run_id": str(result.run_id),
            "mode": mode,
            "status": result.status,
            "started_at": result.started_at.isoformat(timespec="seconds"),
            "finished_at": result.finished_at.isoformat(timespec="seconds") if result.finished_at else None,
            "http_requests": result.requests,
            "loaded_to_database": result.loaded,
        },
        "titles": {
            "harvested": len(result.titles),
            "by_status": dict(Counter(t.status for t in result.titles)),
            "stale": [t.title.title_id for t in result.titles if t.stale],
            "detail": [t.summary() for t in result.titles],
        },
        "classification": {
            "articles_seen": sum(t.articles_seen for t in result.titles),
            "notices": sum(t.notices for t in result.titles),
            "by_tier": dict(result.tiers),
            "top_reasons": dict(result.decisions.most_common(15)),
        },
        "records": {
            "total": len(records),
            "by_state": dict(Counter(r.state_code for r in records)),
            "by_category": dict(Counter(r.notice_category for r in records)),
            "by_geo_status": dict(Counter(r.geo_status for r in records)),
            "by_geo_precision": dict(Counter(r.geo_precision or "none" for r in records)),
            "quality_flags": dict(Counter(f for r in records for f in r.quality_flags)),
            "missing": {
                "published_on": sum(1 for r in records if r.published_on is None),
                "body_text": sum(1 for r in records if not r.body_text),
                "section": sum(1 for r in records if not r.section),
                "geometry": sum(1 for r in records if r.x is None),
            },
            "published_on_range": [published[0].isoformat(), published[-1].isoformat()] if published else None,
        },
        "load": {
            "inserted": result.counts.inserted,
            "updated": result.counts.updated,
            "unchanged": result.counts.unchanged,
        }
        if result.loaded
        else None,
        "errors": {
            "total": len(errors),
            "by_severity": dict(Counter(e.severity for e in errors)),
            "by_stage_code": dict(Counter(f"{e.stage}:{e.code}" for e in errors)),
            "sample": [e.to_json() for e in errors if e.severity == "error"][:ERROR_SAMPLE],
        },
        "locations_needing_review": [
            {
                "source_record_id": r.source_record_id,
                "title": r.title,
                "geo_status": r.geo_status,
                "place_text": r.place_text,
                "fallback_area": r.municipality_name,
                "note": r.geo_note,
            }
            for r in records
            if r.geo_status != "resolved"
        ][:LOCATION_SAMPLE],
    }
    if catalogue is not None:
        in_scope = catalogue.in_scope()
        report["catalogue"] = {
            "created_at": catalogue.created_at,
            "titles_found": len(catalogue.titles),
            "in_scope": len(in_scope),
            "in_scope_by_state": dict(Counter(t.state_code for t in in_scope)),
            "excluded_reasons": dict(Counter(t.reason.split(":")[0] for t in catalogue.titles if not t.in_scope)),
            "prefixes_queried": len(catalogue.prefixes_queried),
            "saturated_prefixes": catalogue.saturated_prefixes,
            "overview_titles_in_scope": catalogue.overview_in_scope,
            "overview_titles_found_by_sweep": catalogue.overview_found_by_sweep,
            "errors": dict(Counter(f"{e.stage}:{e.code}" for e in catalogue.errors)),
        }
    return report


def render_markdown(report: dict) -> str:
    run = report["run"]
    lines = [
        "# IRIS-SIG-18 harvest quality report",
        "",
        f"- Run `{run['run_id']}` ({run['mode']}), status **{run['status']}**",
        f"- {run['started_at']} to {run['finished_at']}, {run['http_requests']} HTTP requests",
    ]
    if report.get("load"):
        load = report["load"]
        lines.append(f"- Database: {load['inserted']} inserted, {load['updated']} updated, {load['unchanged']} unchanged")
    else:
        lines.append("- Database: not loaded (`--no-db`)")

    if cat := report.get("catalogue"):
        lines += [
            "",
            "## Catalogue",
            "",
            f"{cat['titles_found']} titles found via {cat['prefixes_queried']} postcode queries; "
            f"{cat['in_scope']} in scope ({_kv(cat['in_scope_by_state'])}).",
            f"Excluded: {_kv(cat['excluded_reasons'])}.",
            f"Overview cross-check: {cat['overview_titles_found_by_sweep']} of {cat['overview_titles_in_scope']} "
            "TH/SN overview titles were also found by the postcode sweep.",
            f"Discovery errors and warnings: {_kv(cat['errors']) or 'none'}.",
        ]

    titles = report["titles"]
    lines += [
        "",
        "## Titles",
        "",
        f"Status: {_kv(titles['by_status'])}." + (f" Stale (no recent issue): {', '.join(titles['stale'])}." if titles["stale"] else ""),
        "",
        "| Title | State | Status | Newest | Issues read | Articles | Notices | Records | Errors | Warnings |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in titles["detail"]:
        lines.append(
            f"| {t['title_id']} {t['name']} | {t['state_code']} | {t['status']} | {t['newest_issue'] or '-'} | "
            f"{', '.join(t['issues_processed']) or '-'} | {t['articles_seen']} | {t['notices']} | {t['records']} | "
            f"{t['errors']} | {t['warnings']} |"
        )

    cls = report["classification"]
    rec = report["records"]
    lines += [
        "",
        "## Records",
        "",
        f"{cls['articles_seen']} articles listed, {cls['notices']} classified as notices, {rec['total']} records.",
        "",
        f"- By state: {_kv(rec['by_state'])}",
        f"- By category: {_kv(rec['by_category'])}",
        f"- Geo status: {_kv(rec['by_geo_status'])}; precision: {_kv(rec['by_geo_precision'])}",
        f"- Missing fields: {_kv(rec['missing'])}",
        f"- Quality flags: {_kv(rec['quality_flags']) or 'none'}",
        f"- Publication dates: {' to '.join(rec['published_on_range']) if rec['published_on_range'] else 'none'}",
        "",
        "## Errors and warnings",
        "",
        f"{report['errors']['total']} in total ({_kv(report['errors']['by_severity']) or 'none'}).",
        "",
    ]
    for key, count in sorted(report["errors"]["by_stage_code"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{key}`: {count}")
    if report["errors"]["sample"]:
        lines += ["", "Errors (sample):", ""]
        for e in report["errors"]["sample"]:
            lines.append(f"- `{e['stage']}:{e['code']}` {e['message']} ({e['source_url'] or '-'})")

    lines += ["", "## Locations needing review", ""]
    if report["locations_needing_review"]:
        lines += ["| Record | Status | Place text | Point used | Note |", "|---|---|---|---|---|"]
        for loc in report["locations_needing_review"]:
            lines.append(
                f"| {loc['source_record_id']} | {loc['geo_status']} | {loc['place_text'] or '-'} | "
                f"{loc['fallback_area'] or 'none'} | {loc['note'] or '-'} |"
            )
    else:
        lines.append("None: every record resolved from its section or place label.")
    return "\n".join(lines) + "\n"


def _kv(mapping: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(mapping.items(), key=lambda kv: (-kv[1], str(kv[0]))))


def write_outputs(result: RunResult, report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "signals.jsonl").open("w", encoding="utf-8") as handle:
        for record in result.records:
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")
    with (out_dir / "errors.jsonl").open("w", encoding="utf-8") as handle:
        for error in result.errors:
            handle.write(json.dumps(error.to_json(), ensure_ascii=False) + "\n")
    (out_dir / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "quality_report.md").write_text(render_markdown(report), encoding="utf-8")
