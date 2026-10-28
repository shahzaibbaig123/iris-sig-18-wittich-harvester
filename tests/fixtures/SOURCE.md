# Test fixtures: provenance

Captured from the public LINUS WITTICH online edition on **2026-10-28** with
plain GET requests, spaced 1.5 s apart, on paths that the host's robots.txt
does not block (see the README, "Source verification"). No logins, cookies or
query strings were used.

| Fixture | Source URL (ol.wittich.de unless noted) | Notes |
|---|---|---|
| `robots/ol_robots.txt` | `/robots.txt` | 8 of 1028 groups kept, including `Harvest`, `es`, `Python-urllib`, `Scrapy`. The commented-out `User-agent: *` block is kept. |
| `robots/www_robots.txt` | `https://www.wittich.de/robots.txt` | 9 of 1029 groups kept, including `User-agent: *` with `Disallow: /*?*` |
| `search/zip_76.html` | `/internal/typ/ZIP/begriff/76` | RP; contains title 130 (Jockgrim) |
| `search/zip_07.html` | `/internal/typ/ZIP/begriff/07` | TH; 49 titles, i.e. a saturated result; contains title 1740 |
| `search/zip_01809.html` | `/internal/typ/ZIP/begriff/01809` | SN; contains title 2701 (Heidenau) |
| `search/zip_34233.html` | `/internal/typ/ZIP/begriff/34233` | HE; contains title 1329 (Fuldatal) |
| `search/uebersicht.html` | `/uebersicht` | only the "by state" list kept |
| `pages/toc_130_43_2026_p0..p4.html` | `/titel/130/ausgabe/43/2026/rubrik/alle/seite/{n}/sortiert/inhaltsverzeichnis` | 79 articles over pages 0–3; page 4 is the empty page after the end |
| `pages/toc_1740_11_2026_p0..p1.html` | same pattern, title 1740, issue 11/2026 | lists one article twice: real source behaviour |
| `pages/toc_2701_20_2026_p0..p1.html` | same pattern, title 2701, issue 20/2026 | |
| `pages/toc_1329_empty.html` | `/titel/1329` | redirects to `/ausgabe/0/1900`: no online edition |
| `pages/art_<articleId>.html` | `/titel/{id}/ausgabe/{nr}/{year}/artikel/{articleId}` | every notice of the three issues, plus the first article of 2701 for its manifest |
| `routes.json` (`/internal/neuste/ausgabe/*`) | `/internal/neuste/ausgabe/{id}` | the four JSON answers, stored verbatim |

`routes.json` maps each URL path to its file for `FixtureTransport`.
`offline.toml` limits discovery and harvesting to those routes.

## Changes made to the captured pages

1. **Trimmed.** Navigation, footer, sidebar, stylesheets, images and all
   scripts are removed, except the `article_list` manifest. The TOC issue
   selector keeps its six newest options. Whitespace is collapsed. Markup the
   parsers rely on is unchanged.
2. **Teasers of non-notices removed.** The TOC teaser of every article not
   classified as a notice is replaced with `[gekürzt]`, and so is the body of
   2701's first article, which is kept only for its manifest. Club, church and
   personal news (birthdays, obituaries) are copyrighted and/or personal.
3. **Notice bodies shortened.** Each notice body is cut after about 800
   characters and ends with `[… für die Testdaten gekürzt]`.
4. **Pseudonymized.** Every remaining personal name is replaced with
   `Person A` … `Person Q`. That covers officials signing in office, staff
   contacts, private individuals and politicians named in headlines. Private
   building-application addresses and parcel numbers become `Musterstraße 1`
   and `000/1`. The court file number and parcel of the Amtsgericht notice are
   replaced as well. The replacement list is not part of this repository,
   because it contains the real names.

Headlines, sections, dates, place labels and article ids are unchanged, so
classification, dating and geocoding behave as on the live pages.
