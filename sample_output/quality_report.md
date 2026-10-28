# IRIS-SIG-18 harvest quality report

- Run `7332c0c0-0ff2-42ac-8285-b8ce2ab06b65` (live), status **succeeded**
- 2026-10-28T15:20:54+00:00 to 2026-10-28T15:22:22+00:00, 86 HTTP requests
- Database: 49 inserted, 0 updated, 0 unchanged

## Catalogue

1490 titles found via 279 postcode queries; 741 in scope (HE 264, SN 167, RP 155, TH 155).
Excluded: out_of_state_scope 365, excluded_title 335, excluded_frequency 49.
Overview cross-check: 117 of 119 TH/SN overview titles were also found by the postcode sweep.
Discovery errors and warnings: validate:search_saturated 1.

## Titles

Status: ok 9, download_disabled 2.

| Title | State | Route | Status | Newest | Issues read | Articles/blocks | Notices | Records | Errors | Warnings |
|---|---|---|---|---|---|---|---|---|---|---|
| 104 Bellheim | RP | online | ok | 43/2026 | 43/2026 | 61 | 5 | 5 | 0 | 1 |
| 130 Jockgrim | RP | online | ok | 43/2026 | 43/2026 | 77 | 4 | 4 | 0 | 1 |
| 183 Wörth | RP | epaper | ok | 40/2026 | 40/2026 | 20 | 0 | 0 | 0 | 0 |
| 1329 Fuldatal | HE | epaper | download_disabled | 43/2026 | - | 0 | 0 | 0 | 0 | 1 |
| 1353 Kaufungen | HE | epaper | download_disabled | 44/2026 | - | 0 | 0 | 0 | 0 | 1 |
| 1391 Vellmarer Blättchen | HE | epaper | ok | 22/2026 | 22/2026 | 99 | 1 | 1 | 0 | 0 |
| 1691 Gemeinde-Nachrichten | TH | online | ok | 10/2026 | 10/2026 | 36 | 2 | 2 | 0 | 0 |
| 1740 Amtsblatt der Verwaltungsgemeinschaft Schiefergebirge | TH | online | ok | 11/2026 | 11/2026 | 28 | 7 | 7 | 0 | 1 |
| 1803 Wurzbacher Stadtkurier / Amts- und Mitteilungsblatt der Stadt Wurzbach | TH | online | ok | 3/2026 | 3/2026 | 16 | 4 | 4 | 0 | 0 |
| 2600 Lokal-Anzeiger | Amtl. Mitteilungsblatt Stadt Dohna und der Gem. Müglitztal | SN | online | ok | 10/2026 | 10/2026 | 55 | 17 | 17 | 0 | 0 |
| 2701 Heidenauer Journal - Amtsblatt und Stadtzeitung der Stadt Heidenau | SN | online | ok | 20/2026 | 20/2026 | 21 | 9 | 9 | 0 | 0 |

## Records

413 articles listed, 49 classified as notices, 49 records.

- By state: SN 26, TH 13, RP 9, HE 1; by source: wittich_ol 48, wittich_epaper 1
- By category: sitzung 28, general 10, satzung_haushalt 4, umwelt_sicherheit 2, vergabe_stelle 2, bauleitplanung 1, verkehr 1, zustellung_justiz 1
- Geo status: resolved 48, fallback_publication_area 1; precision: municipality 43, municipal_association 6
- Missing fields: body_text 0, geometry 0, published_on 0, section 0
- Quality flags: geo_fallback_publication_area 1, issue_level_date 1
- Publication dates: 2026-09-06 to 2026-10-30

## Errors and warnings

5 in total (warning 5).

- `validate:toc_duplicate_entries`: 3
- `validate:epaper_download_disabled`: 2

## Locations needing review

| Record | Status | Place text | Point used | Note |
|---|---|---|---|---|
| 1391-2026-22-p016-8e3e35890f | fallback_publication_area | - | Vellmar | located at publication area Stadt Vellmar (066330026026) |
