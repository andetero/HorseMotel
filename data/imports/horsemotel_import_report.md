# HorseMotel.com Import Report

Generated: 2026-05-05T05:34:16.365001+00:00
Listings written: 838

## Inputs
- Authorized public HorseMotel.com listing pages: https://www.horsemotel.com/
- data/imports/horsemotel_map.kml
- https://www.google.com/maps/d/kml?mid=1qrjPl4O3jErNdqkjkci9NcMi1AU&forcekml=1

## Notes
- Partner/source: HorseMotel.com
- Attribution: Listing provided by HorseMotel.com
- HorseMotel.com remains the source of truth.
- Rows without coordinates are skipped until latitude/longitude are provided.
- Street addresses are captured as the preferred external map/search location when available.
- KML / Google My Maps coordinates are treated as fallback or approximate pin coordinates, not authoritative street-address validation.
- Hookups are inferred from free-text descriptions, with negative phrases such as "no dump station" or "no sewer" excluded.
- Listing image URLs are captured from HorseMotel.com listing blocks when image files are present.
- The importer can download the authorized Google My Maps KML into data/imports/horsemotel_map.kml and use it to improve fallback coordinates.
- Website-derived imports read public HorseMotel.com listing pages with permission from HorseMotel.com.
- KML-only placemarks are included so Canada and international HorseMotel.com listings are not lost when they are not exposed through U.S. state pages.
