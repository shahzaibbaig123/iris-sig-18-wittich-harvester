"""Settings: config/harvester.toml plus environment overrides.

Secrets (DATABASE_URL) and the operator contact (WITTICH_CONTACT) only ever
come from the environment, never from the TOML file or the source code.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "harvester.toml"

# ARS prefix (Land) for each supported state code.
STATE_ARS_PREFIX = {"RP": "07", "HE": "06", "TH": "16", "SN": "14"}
ARS_PREFIX_STATE = {prefix: code for code, prefix in STATE_ARS_PREFIX.items()}
STATE_NAMES = {
    "RP": "Rheinland-Pfalz",
    "HE": "Hessen",
    "TH": "Thüringen",
    "SN": "Sachsen",
}


@dataclass(frozen=True)
class HttpSettings:
    user_agent: str
    min_interval_seconds: float = 1.0
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    rate_limit_base_seconds: float = 30.0
    respect_robots: bool = True


@dataclass(frozen=True)
class DiscoverySettings:
    zip_prefixes: tuple[str, ...]
    saturation_threshold: int = 40
    max_prefix_length: int = 4
    use_overview: bool = True
    exclude_title_patterns: tuple[str, ...] = ()
    exclude_frequencies: tuple[str, ...] = ()
    catalogue_max_age_days: int = 7


@dataclass(frozen=True)
class HarvestSettings:
    initial_issues: int = 1
    overlap_issues: int = 1
    max_issues_per_title: int = 4
    max_toc_pages: int = 30
    stale_after_years: int = 1
    titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class FilterSettings:
    signal_type: str
    place_section_prefix: re.Pattern[str]
    strong_sections: tuple[re.Pattern[str], ...]
    excluded_sections: tuple[re.Pattern[str], ...]
    excluded_headlines: tuple[re.Pattern[str], ...]
    notice_cues: tuple[re.Pattern[str], ...]
    headline_cues: tuple[re.Pattern[str], ...]
    categories: tuple[Category, ...]


@dataclass(frozen=True)
class GeoSettings:
    gazetteer_path: Path
    envelope: tuple[float, float, float, float]
    state_bbox_buffer_m: float = 5000.0


@dataclass(frozen=True)
class Settings:
    source_system: str
    base_url: str
    epaper_base_url: str
    states: tuple[str, ...]
    http: HttpSettings
    discovery: DiscoverySettings
    harvest: HarvestSettings
    filter: FilterSettings
    geo: GeoSettings
    database_url: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def ars_prefixes(self) -> tuple[str, ...]:
        return tuple(STATE_ARS_PREFIX[s] for s in self.states)


def _patterns(values) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(v, re.IGNORECASE) for v in values)


def _merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def user_agent(template: str, contact: str) -> str:
    """Fill in the operator contact, or drop the "(... {contact} ...)" part."""
    if contact:
        return template.replace("{contact}", contact)
    return re.sub(r"\s*\([^)]*\{contact\}[^)]*\)", "", template).strip()


def load_settings(
    path: Path | str | None = None,
    env: dict[str, str] | None = None,
    states: list[str] | None = None,
    overlay: Path | str | None = None,
) -> Settings:
    """Load the TOML config; ``overlay`` is merged over it (used by --offline)."""
    env = os.environ if env is None else env
    path = Path(path) if path else DEFAULT_CONFIG
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if overlay:
        with Path(overlay).open("rb") as handle:
            data = _merge(data, tomllib.load(handle))

    selected = tuple(s.upper() for s in (states or data["scope"]["states"]))
    unknown = [s for s in selected if s not in STATE_ARS_PREFIX]
    if unknown:
        raise ValueError(f"Unsupported state code(s): {', '.join(unknown)}")

    http = dict(data["http"])
    http["user_agent"] = user_agent(http["user_agent"], env.get("WITTICH_CONTACT", "").strip())

    flt = data["filter"]
    geo = data["geo"]
    gazetteer = Path(geo["gazetteer_path"])
    if not gazetteer.is_absolute():
        gazetteer = PROJECT_ROOT / gazetteer

    return Settings(
        source_system=data["source"]["system"],
        base_url=data["source"]["base_url"].rstrip("/"),
        epaper_base_url=data["source"]["epaper_base_url"].rstrip("/"),
        states=selected,
        http=HttpSettings(**http),
        discovery=DiscoverySettings(
            **{
                **data["discovery"],
                "zip_prefixes": tuple(data["discovery"]["zip_prefixes"]),
                "exclude_title_patterns": tuple(data["discovery"]["exclude_title_patterns"]),
                "exclude_frequencies": tuple(data["discovery"]["exclude_frequencies"]),
            }
        ),
        harvest=HarvestSettings(**{**data["harvest"], "titles": tuple(str(t) for t in data["harvest"].get("titles", ()))}),
        filter=FilterSettings(
            signal_type=flt["signal_type"],
            place_section_prefix=re.compile(flt["place_section_prefix"], re.IGNORECASE),
            strong_sections=_patterns(flt["strong_sections"]),
            excluded_sections=_patterns(flt["excluded_sections"]),
            excluded_headlines=_patterns(flt["excluded_headlines"]),
            notice_cues=_patterns(flt["notice_cues"]),
            headline_cues=_patterns(flt.get("headline_cues", ())),
            categories=tuple(
                Category(c["name"], re.compile(c["pattern"], re.IGNORECASE))
                for c in flt["category"]
            ),
        ),
        geo=GeoSettings(
            gazetteer_path=gazetteer,
            envelope=tuple(float(v) for v in geo["envelope"]),
            state_bbox_buffer_m=float(geo["state_bbox_buffer_m"]),
        ),
        database_url=env.get("DATABASE_URL") or None,
        raw=data,
    )


def contact_configured(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("WITTICH_CONTACT", "").strip())
