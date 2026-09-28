# Quote map data

Natural Earth public-domain cartography, downloaded 2026-09-22.
Source and terms: https://www.naturalearthdata.com/about/terms-of-use/

Inputs from the Natural Earth GeoJSON distribution:

- https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson
  SHA256: `6866c877d39cba9c357620878839b336d569f8c662d3cfab4cb1dbe2d39c977f`
- https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places.geojson
  SHA256: `9b8e3de09048ef00dfc70357dbb9fa324493f214b5e0ae4daf1aa79a8d10116b`

`world.json` retains country polygons at three decimal places, ISO identifiers,
and label centers. City catalogs retain coordinates at four decimal places,
Spanish names where available and source aliases. Population only orders the
suggestion list; it never resolves ambiguous names. Country-only centers for
small territories absent from the 110m geometry use their largest listed city.

This snapshot contains 7,330 cities in 225 separate country files. It is an
illustration, not a complete address directory. Matching requires one exact
normalized name or alias within the selected country. Unknown or ambiguous
names show a clearly labeled country approximation. No postal-code validation,
carrier coverage, transit-time calculation or actual flight routing is inferred.

The browser loads the world geometry and only the selected countries' catalogs.
No external geocoding service receives the quote inputs.

## Argentina provinces

`argentina.json` is a simplified local snapshot of IGN boundaries delivered by
Georef, downloaded 2026-09-22. All 24 jurisdiction codes match the portal's
canonical province catalog. Source:
https://apis.datos.gob.ar/georef/api/v2.0/provincias.geojson

Official download documentation:
https://www.argentina.gob.ar/georef/descarga-de-la-base-completa

Source SHA256: `b086852064c26813d9f607c82c91b4e74e18ea83fd21993ddedd473cf8495af5`.

Rebuild with `python3 scripts/maps/build_argentina_provinces.py SOURCE OUTPUT`.
The script simplifies boundaries at 0.025 degrees, rounds to three decimals,
normalizes spherical winding, and omits tiny rings that collapse after rounding.
The visible reference for Tierra del Fuego is its capital, because the full
jurisdiction centroid supplied by IGN is in Antarctica. All national markers
are explicitly labeled provincial references, not locality geocodes or carrier
coverage. Province polygons load only for the national view.
