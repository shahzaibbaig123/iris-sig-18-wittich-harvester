"""Place resolution to EPSG:25832 points via the VG250 gazetteer.

Order of evidence, most specific first:
  1. the article's section when it names a place ("Ortsgemeinde Hatzenbühl"),
  2. the place label the table of contents shows for the article,
  3. the publication area itself (see Gazetteer.publication_area), as an
     explicit fallback.
Names are only matched inside the publication's own area (its Verband or
district), so a "Lehesten" in one district never resolves to another.
Ambiguous names get no point; points outside Germany's UTM32 envelope or the
state's extent are rejected as invalid, never stored.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from wittich_harvester.config import STATE_ARS_PREFIX, GeoSettings
from wittich_harvester.text import clean, fold

LEVEL_BY_ARS_LENGTH = {12: "municipality", 9: "municipal_association", 5: "district"}
MUNICIPALITY_PREFIXES = ("ortsgemeinde", "gemeinde", "stadt", "markt", "ortsteil", "ortsbezirk", "ortschaft")
ASSOCIATION_PREFIXES = ("verbandsgemeinde", "vg", "verwaltungsgemeinschaft", "amt")
_PREFIX = re.compile(r"^(?P<prefix>" + "|".join(MUNICIPALITY_PREFIXES + ASSOCIATION_PREFIXES) + r")\.?\s+(?P<name>.+)$", re.IGNORECASE)
_SPLIT = re.compile(r"\s*,\s*|\s+und\s+|\s*&\s*")


@dataclass(frozen=True)
class Place:
    level: str
    ars: str
    ags: str
    name: str
    bez: str
    state_code: str
    x: float
    y: float


@dataclass
class GeoResult:
    status: str  # resolved | fallback_publication_area | ambiguous | unresolved | invalid
    x: float | None = None
    y: float | None = None
    precision: str | None = None
    ars: str | None = None
    municipality_name: str | None = None
    place_text: str | None = None
    note: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def has_point(self) -> bool:
        return self.x is not None and self.y is not None


def _keys(name: str) -> set[str]:
    """Exact key plus a base key without "(Pfalz)"-style or "/Saale" suffixes."""
    exact = fold(name)
    base = re.sub(r"\s*\(.*?\)\s*$", "", exact)
    base = re.sub(r"/.*$", "", base).strip()
    return {exact, base} - {""}


class Gazetteer:
    def __init__(self, places: list[Place]):
        self.places = places
        self.by_ars: dict[tuple[str, str], Place] = {(p.level, p.ars): p for p in places}
        self._members: dict[str, list[Place]] = {}
        for place in places:
            if place.level == "municipality":
                self._members.setdefault(place.ars[:9], []).append(place)
        self.by_key: dict[str, list[Place]] = {}
        for place in places:
            for key in _keys(place.name):
                self.by_key.setdefault(key, []).append(place)
        self.state_bbox: dict[str, tuple[float, float, float, float]] = {}
        for place in places:
            minx, miny, maxx, maxy = self.state_bbox.get(place.state_code, (place.x, place.y, place.x, place.y))
            self.state_bbox[place.state_code] = (
                min(minx, place.x), min(miny, place.y), max(maxx, place.x), max(maxy, place.y)
            )

    @classmethod
    def load(cls, path: Path) -> "Gazetteer":
        with path.open(encoding="utf-8", newline="") as handle:
            return cls(
                [
                    Place(r["level"], r["ars"], r["ags"], r["name"], r["bez"], r["state_code"], float(r["x"]), float(r["y"]))
                    for r in csv.DictReader(handle)
                ]
            )

    def area(self, ars: str | None) -> Place | None:
        level = LEVEL_BY_ARS_LENGTH.get(len(ars or ""))
        return self.by_ars.get((level, ars)) if level else None

    def members(self, association_ars: str) -> list[Place]:
        return [p for p in self._members.get(association_ars, ())]

    def publication_area(self, title_ars: str | None) -> Place | None:
        """The area a gazette covers, from the ARS Wittich gives the title.

        A 12-digit code names the seat municipality; the gazette serves the
        whole Verband around it when that Verband has several members (e.g.
        title 1740, Amtsblatt der VG Schiefergebirge, is coded as Probstzella).
        A Verband with a single member (an Einheitsgemeinde) is that
        municipality, which is the finer description.
        """
        if not title_ars:
            return None
        if len(title_ars) in (9, 12):
            association = self.by_ars.get(("municipal_association", title_ars[:9]))
            members = self.members(title_ars[:9])
            if association is not None and len(members) > 1:
                return association
            if len(members) == 1:
                return members[0]
            return self.area(title_ars) or association
        return self.area(title_ars)

    def lookup(self, name: str, scope: str, levels: tuple[str, ...]) -> list[Place]:
        """Places called ``name`` at one of ``levels`` whose ARS starts with ``scope``."""
        found: dict[tuple[str, str], Place] = {}
        for key in _keys(name):
            for place in self.by_key.get(key, ()):
                if place.level in levels and place.ars.startswith(scope):
                    found[(place.level, place.ars)] = place
        # Prefer the finest level that matched.
        for level in levels:
            hits = [p for p in found.values() if p.level == level]
            if hits:
                return hits
        return []


class GeoResolver:
    def __init__(self, gazetteer: Gazetteer, settings: GeoSettings):
        self.gazetteer = gazetteer
        self.settings = settings

    # -- validation -----------------------------------------------------------

    def validate(self, x: float | None, y: float | None, state_code: str) -> str | None:
        """Return why a point is invalid, or None when it is acceptable."""
        if x is None or y is None:
            return "missing coordinate"
        if x != x or y != y:  # NaN
            return "coordinate is NaN"
        minx, miny, maxx, maxy = self.settings.envelope
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return f"({x:.1f}, {y:.1f}) outside the EPSG:25832 envelope of Germany"
        bbox = self.gazetteer.state_bbox.get(state_code)
        if bbox:
            buf = self.settings.state_bbox_buffer_m
            if not (bbox[0] - buf <= x <= bbox[2] + buf and bbox[1] - buf <= y <= bbox[3] + buf):
                return f"({x:.1f}, {y:.1f}) outside the extent of {state_code}"
        return None

    # -- resolution -----------------------------------------------------------

    def resolve(self, state_code: str, title_ars: str | None, section: str, place_label: str) -> GeoResult:
        area = self.gazetteer.publication_area(title_ars)
        if title_ars and len(title_ars) >= 9:
            scope = title_ars[:9]  # the Verband (or Einheitsgemeinde) containing the title area
        elif title_ars and len(title_ars) == 5:
            scope = title_ars
        else:
            scope = STATE_ARS_PREFIX[state_code]

        notes: list[str] = []
        for source, text in (("section", section), ("label", place_label)):
            text = clean(text)
            if not text:
                continue
            result = self._from_text(source, text, scope, area, notes)
            if result is not None:
                result.note = "; ".join([*notes, result.note] if result.note else notes) or None
                return self._checked(result, state_code)

        if area is not None:
            notes.append(f"located at publication area {area.bez} {area.name} ({area.ars})")
            return self._checked(
                self._point(area, "fallback_publication_area", place_label or None, "; ".join(notes)), state_code
            )
        notes.append("publication has no gazetteer area" if title_ars else "publication has no ARS")
        return GeoResult(status="unresolved", place_text=clean(place_label) or None, note="; ".join(notes))

    def _from_text(self, source: str, text: str, scope: str, area: Place | None, notes: list[str]) -> GeoResult | None:
        match = _PREFIX.match(text)
        if source == "section" and not match:
            return None  # sections like "Amtlicher Teil" carry no place
        prefix = match["prefix"].lower() if match else ""
        name = match["name"] if match else text
        if prefix in ASSOCIATION_PREFIXES:
            levels: tuple[str, ...] = ("municipal_association",)
        elif prefix in MUNICIPALITY_PREFIXES:
            levels = ("municipality",)
        else:
            levels = ("municipality", "municipal_association")

        # A label that just repeats the publication area's name, or is only an
        # association word ("Verbandsgemeinde"), means "the whole area".
        if area is not None and not prefix and fold(name) in _keys(area.name):
            return self._point(area, "resolved", text, f"{source} names the publication area")
        if area is not None and area.level == "municipal_association" and fold(text).rstrip(".") in ASSOCIATION_PREFIXES:
            return self._point(area, "resolved", text, f"{source} '{text}' denotes the publication area")

        hits = self.gazetteer.lookup(name, scope, levels)
        if len(hits) == 1:
            return self._point(hits[0], "resolved", text, f"matched {source}")
        if len(hits) > 1:
            return GeoResult(
                status="ambiguous",
                place_text=text,
                note=f"{source} '{name}' matches {len(hits)} places in scope {scope}",
                candidates=[p.ars for p in hits],
            )

        parts = [p for p in _SPLIT.split(name) if p]
        if len(parts) > 1:
            resolved = [self.gazetteer.lookup(p, scope, ("municipality",)) for p in parts]
            if all(len(r) == 1 for r in resolved):
                # Several municipalities: use the Verband they share, if any.
                shared = {r[0].ars[:9] for r in resolved}
                container = self.gazetteer.by_ars.get(("municipal_association", shared.pop())) if len(shared) == 1 else None
                target = container or area
                if target is not None:
                    names = ", ".join(r[0].name for r in resolved)
                    return self._point(
                        target, "fallback_publication_area", text, f"{source} lists {len(parts)} municipalities ({names})"
                    )
        notes.append(f"{source} '{name}' not found in gazetteer scope {scope}")
        return None

    def _point(self, place: Place, status: str, place_text: str | None, note: str) -> GeoResult:
        return GeoResult(
            status=status,
            x=place.x,
            y=place.y,
            precision=place.level,
            ars=place.ars,
            municipality_name=place.name,
            place_text=place_text,
            note=note,
        )

    def _checked(self, result: GeoResult, state_code: str) -> GeoResult:
        if not result.has_point:
            return result
        reason = self.validate(result.x, result.y, state_code)
        if reason is None and result.ars and not result.ars.startswith(STATE_ARS_PREFIX[state_code]):
            reason = f"ARS {result.ars} is not in {state_code}"
        if reason is None:
            return result
        return GeoResult(
            status="invalid",
            ars=result.ars,
            municipality_name=result.municipality_name,
            place_text=result.place_text,
            note=f"rejected point: {reason}",
        )
