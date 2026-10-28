"""Build data/gazetteer/vg250_points_rp_he_th_sn.csv from the BKG VG250 GeoPackage.

    curl -L -o data/raw/vg250.zip \\
      https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_0101/aktuell/vg250_01-01.utm32s.gpkg.ebenen.zip
    unzip -o data/raw/vg250.zip -d data/raw
    uv run --group gazetteer python scripts/build_gazetteer.py data/raw/vg250_ebenen_0101/DE_VG250.gpkg

VG250 "utm32s" is already EPSG:25832, so no reprojection happens here. Each
area becomes its representative point (guaranteed inside the polygon, unlike
a centroid). Only land areas (GF = 4) of the four states in scope are kept.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

import shapely

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "gazetteer" / "vg250_points_rp_he_th_sn.csv"
STATES = {"07": "RP", "06": "HE", "16": "TH", "14": "SN"}
LAYERS = (("vg250_gem", "municipality"), ("vg250_vwg", "municipal_association"), ("vg250_krs", "district"))
ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def gpkg_to_wkb(blob: bytes) -> bytes:
    if blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry blob")
    flags = blob[3]
    envelope = ENVELOPE_BYTES[(flags >> 1) & 0x07]
    return blob[8 + envelope :]


def build(gpkg: Path, out: Path) -> int:
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    srs = {row[0]: row[1] for row in con.execute("SELECT table_name, srs_id FROM gpkg_contents")}
    rows = []
    seen: set[tuple[str, str]] = set()
    for table, level in LAYERS:
        if srs.get(table) != 25832:
            raise SystemExit(f"{table}: expected EPSG:25832, found {srs.get(table)}")
        query = f"SELECT ARS, AGS, GEN, BEZ, geom FROM {table} WHERE GF = 4 AND substr(ARS, 1, 2) IN (?, ?, ?, ?)"
        for ars, ags, name, bez, blob in con.execute(query, tuple(STATES)):
            if (level, ars) in seen:
                continue
            seen.add((level, ars))
            point = shapely.from_wkb(gpkg_to_wkb(blob)).representative_point()
            rows.append(
                {
                    "level": level,
                    "ars": ars,
                    "ags": ags,
                    "name": name,
                    "bez": bez,
                    "state_code": STATES[ars[:2]],
                    "x": f"{point.x:.1f}",
                    "y": f"{point.y:.1f}",
                }
            )
    rows.sort(key=lambda r: (r["state_code"], r["level"], r["ars"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("gpkg", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    count = build(args.gpkg, args.out)
    print(f"wrote {count} points to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
