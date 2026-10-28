"""Staging records, change hashes, quality flags and in-batch de-duplication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from wittich_harvester.text import clean

# Fields that define a record's content. Run ids and timestamps are excluded,
# so re-harvesting an unchanged article yields the same hash.
HASH_FIELDS = (
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
    "state_code",
    "ars",
    "municipality_name",
    "place_text",
    "x",
    "y",
    "geo_precision",
    "geo_status",
    "quality_flags",
)

OUTLIER_DAYS = 7


@dataclass
class ErrorRecord:
    stage: str  # robots | fetch | parse | validate | geo | load
    code: str
    message: str
    severity: str = "error"  # error | warning
    source_url: str | None = None
    source_record_id: str | None = None
    payload: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SignalRecord:
    source_system: str
    source_record_id: str
    source_url: str
    signal_type: str
    notice_category: str | None
    title: str
    summary: str | None
    body_text: str | None
    published_on: date | None
    section: str | None
    publication_id: str
    publication_title: str | None
    issue_number: int | None
    issue_year: int | None
    issue_url: str | None
    state_code: str
    ars: str | None
    municipality_name: str | None
    place_text: str | None
    x: float | None
    y: float | None
    geo_precision: str | None
    geo_status: str
    geo_note: str | None
    quality_flags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    def compute_hash(self) -> str:
        payload = {name: _jsonable(getattr(self, name)) for name in HASH_FIELDS}
        payload["quality_flags"] = sorted(self.quality_flags)
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.content_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return self.content_hash

    def to_json(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 1)
    return value


def date_flags(published_on: date | None, modal_date: date | None, issue_year: int | None) -> list[str]:
    """Flag missing dates and dates far from the rest of the issue."""
    if published_on is None:
        return ["missing_published_on"]
    if modal_date is not None and abs((published_on - modal_date).days) > OUTLIER_DAYS:
        return ["published_on_outlier"]
    if issue_year is not None and abs(published_on.year - issue_year) > 1:
        return ["published_on_outlier"]
    return []


def headline_flags(stub_headline: str, manifest_caption: str | None, article_headline: str | None) -> list[str]:
    seen = {clean(h).casefold() for h in (stub_headline, manifest_caption, article_headline) if h}
    return ["headline_mismatch"] if len(seen) > 1 else []


def dedupe(records: list[SignalRecord]) -> tuple[list[SignalRecord], list[ErrorRecord]]:
    """Keep the first record per source key; report repeats, louder when they differ."""
    kept: dict[tuple[str, str], SignalRecord] = {}
    errors: list[ErrorRecord] = []
    for record in records:
        key = (record.source_system, record.source_record_id)
        first = kept.get(key)
        if first is None:
            kept[key] = record
            continue
        identical = first.content_hash == record.content_hash
        errors.append(
            ErrorRecord(
                stage="validate",
                code="duplicate_identical" if identical else "duplicate_conflict",
                severity="warning",
                message=(
                    "same article seen twice in one run"
                    if identical
                    else "same article seen twice with different content; kept the first"
                ),
                source_url=record.source_url,
                source_record_id=record.source_record_id,
            )
        )
    return list(kept.values()), errors
