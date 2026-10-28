# Gazetteer: `vg250_points_rp_he_th_sn.csv`

Derived from **VG250 Verwaltungsgebiete 1:250 000 (Ebenen), as of 01.01.2026**,
published by the Bundesamt für Kartographie und Geodäsie (BKG):

- Download: `https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_0101/aktuell/vg250_01-01.utm32s.gpkg.ebenen.zip`
  (GeoPackage, UTM32s = EPSG:25832; fetched 2026-10-28, Last-Modified 2026-07-21)
- Licence: Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)
- Attribution: © GeoBasis-DE / BKG (2026)

**Changes.** Only land areas (`GF = 4`) of Rheinland-Pfalz (07), Hessen (06),
Thüringen (16) and Sachsen (14) are kept, from three layers:

| level | VG250 layer | rows |
|---|---|---|
| `municipality` | `vg250_gem` (Gemeinden) | 3,744 |
| `municipal_association` | `vg250_vwg` (Verbandsgemeinden, Verwaltungsgemeinschaften, Einheitsgemeinden) | 1,093 |
| `district` | `vg250_krs` (Kreise) | 98 |

Each polygon is reduced to its representative point (a point guaranteed to lie
inside the polygon, unlike a centroid), rounded to 0.1 m in EPSG:25832.
Columns: `level, ars, ags, name, bez, state_code, x, y`.

Rebuild:

```sh
curl -L -o data/raw/vg250.zip <download URL above>
unzip -o data/raw/vg250.zip -d data/raw
uv run --group gazetteer python scripts/build_gazetteer.py data/raw/vg250_ebenen_0101/DE_VG250.gpkg
```
