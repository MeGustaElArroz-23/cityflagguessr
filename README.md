# CityFlagGuessr

CityFlagGuessr is a static, browser-based geography game inspired by GeoGuessr. Each round presents a municipal flag, and the player places a pin on a detailed world map. The result view reveals the municipality, compares the two locations, draws the route between them, and awards up to 5,000 points based on distance.

The project is designed to be copied into an existing website. It has no build step, no server-side component, no API keys, and no paid services. Flag metadata and flag assets are stored locally; the map and Leaflet library are loaded from public internet services at runtime.

## Features

- Choose 1–15 rounds and a 5–60 second limit in five-second increments.
- Daily Challenge runs 8 rounds at 30 seconds each. It uses a deterministic local-date seed, so players using the same catalog get the same flags on the same local calendar day.
- Click the map to place a pin, then accept it; an unaccepted pin is automatically used if time expires.
- Deep-zoom world map with country and city labels.
- Distance-based scoring: guesses within 50 km get 5,000 points; larger distances follow a smooth decay curve.
- Round summary with the real location, selected location, distance, score, route line, and flag-source link.
- Final scorecard with every round's municipality, country, distance, and score.
- Local flag catalog and assets, so gameplay does not depend on Wikimedia once the catalog is built.

## Run the game

The simplest option is to open `index.html` in a browser. For the most reliable local development experience, serve the folder with a small static server:

```bash
cd /home/dario/Web/cityflagguessr
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

An internet connection is still needed for:

- Leaflet, loaded from the unpkg CDN.
- CARTO Voyager map tiles, which use OpenStreetMap data.
- Google Fonts, if those fonts are not already cached.

The municipal catalog is in `data/municipalities.js`, and its local images are in `assets/flags/`.

## Project layout

```text
index.html                    Page structure and screens
styles.css                    Original base styling
overrides.css                 Targeted visual refinements
app.js                        Game state, timer, map, scoring, and results
data/municipalities.js        Active local catalog used by the game
assets/flags/                 Local flag SVG/PNG/GIF assets
scripts/build_dataset.py      Resumable catalog generator
```

## Data sources and attribution

| Resource | Used for | Notes |
| --- | --- | --- |
| [Wikidata](https://www.wikidata.org/) | Municipality names, coordinates, country labels, and flag-file references | Queried only while preparing a new catalog manifest. |
| [Wikimedia Commons](https://commons.wikimedia.org/) | Municipal flag files and their source attribution | Downloaded once into `assets/flags/`; every record retains its Commons file-page link. |
| [Leaflet](https://leafletjs.com/) | Interactive map interface | Loaded from the unpkg CDN. |
| [CARTO basemaps](https://carto.com/basemaps/) | Voyager map tiles | Map data includes OpenStreetMap contributors. |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Geographic map data and labels | Credited in the live map attribution. |

Individual files on Wikimedia Commons can have different licenses and attribution requirements. The result screen links to the precise Commons file page for each displayed flag; retain those links and check file-specific licensing before republishing assets elsewhere.

## Expand the flag catalog

`scripts/build_dataset.py` is deliberately split into three resumable stages. It uses only Wikidata and Wikimedia Commons, makes serialized requests, supplies `maxlag` to the Commons API, waits at least two seconds between requests, and retries transient failures with exponential backoff plus jitter. Do not run multiple downloader instances at once.

Discovery logs show the active endpoint, page number, row count, retry attempt, wait time, HTTP status, and any short response message. Discovery makes up to two attempts per endpoint before moving to the next one, while Commons downloads retain the full retry budget. If Wikidata Query Service is having an outage, use `--query-endpoint qlever` to query the QLever Wikidata mirror directly; `--query-endpoint auto` (the default) tries Wikidata first and then QLever.

During downloading, the terminal shows the current file number, percentage, completed/failed/pending totals, and a per-run ETA. The same totals are persisted in the `progress` section of `data/flag-download-state.json` after every file, so progress survives an interruption.

Every invocation needs a descriptive `--user-agent` containing a real contact email or URL. Replace the example below with your own contact information.

```bash
cd /home/dario/Web/cityflagguessr

# Discover, download, and automatically publish every successfully completed flag.
# If any download fails, the existing playable catalog is preserved.
python3 scripts/build_dataset.py \
  --prepare-manifest \
  --candidate-limit 10000 \
  --download-all \
  --user-agent 'CityFlagGuessr/1.0 (mailto:you@example.com)'

# Resume any pending files after an interruption or a transient failure.
# This may take hours for a large manifest; do not run another downloader in parallel.
python3 scripts/build_dataset.py \
  --download-all \
  --user-agent 'CityFlagGuessr/1.0 (mailto:you@example.com)'

# Optional: manually publish exactly 2,000 completed assets as the playable catalog.
# --clean-assets removes local flag files that are not in the newly published catalog.
python3 scripts/build_dataset.py \
  --build-catalog \
  --limit 2000 \
  --clean-assets \
  --user-agent 'CityFlagGuessr/1.0 (mailto:you@example.com)'
```

For a smaller catalog or a short scheduled run, use `--download-batch 100` instead of `--download-all`. For example, the current catalog was produced with 25 completed records and then built with `--limit 25 --clean-assets`.

The generator writes two resumability files:

- `data/flag-manifest.json`: eligible candidate metadata, including file title, MIME type, source dimensions, Commons download URL, source page, and Wikidata ID.
- `data/flag-download-state.json`: per-file download status, SHA-1, ETag, timestamp, and error details. Completed files with a matching SHA-1 are skipped in later runs.

When `--prepare-manifest` and `--download-all` are combined, all successfully completed files are automatically published only when there are no failed or pending files. Separate staged runs require `--build-catalog`; if fewer than `--limit` files are complete, the active `data/municipalities.js` catalog is preserved unchanged.

### Generator options

| Option | Meaning |
| --- | --- |
| `--prepare-manifest` | Query Wikidata and Commons, then save eligible candidates to the manifest. |
| `--download-batch N` | Download at most `N` pending candidates and save progress after every file. |
| `--download-all` | Download all pending candidates in one serial, resumable run. It can run for hours on a large manifest. |
| `--build-catalog` | Build the playable JavaScript catalog from completed local assets. |
| `--candidate-limit N` | Maximum Wikidata records inspected during manifest preparation. Default: `1000`. |
| `--query-endpoint NAME` | Discovery endpoint: `auto` (default), `wikidata`, or `qlever`. |
| `--limit N` | Exact number of completed records required before publishing. Default: `2000`. |
| `--manifest PATH` | Manifest location. Default: `data/flag-manifest.json`. |
| `--state-file PATH` | Download-state location. Default: `data/flag-download-state.json`. |
| `--output PATH` | Generated playable catalog path. Default: `data/municipalities.js`. |
| `--clean-assets` | After a successful build, delete local flag assets not referenced by the new catalog. Use carefully. |
| `--min-raster-height N` | Minimum original height accepted for PNG/GIF/raster assets. Default: `300`; SVG files are accepted independently of this threshold. |
| `--request-delay SECONDS` | Minimum delay between requests. Must be at least `1`; default: `2`. |
| `--max-retries N` | Retry attempts for transient HTTP/network failures. Default: `6`. |
| `--user-agent TEXT` | Required descriptive User-Agent with a real contact email or URL. |

### Selection and quality rules

The manifest builder accepts only items that Wikidata identifies as a city, town, municipality, or human settlement and that have both coordinates and a flag property. It rejects country/state/region-style file titles, unusable coordinates, inaccessible upload URLs, known dissolved-country labels, and raster flags below the configured quality threshold. The build process also applies a small set of present-day country-label corrections where Wikidata contains historical or disputed statements.

The extraction is best-effort. Before publishing a large catalog, review a sample of local assets and their Commons links; municipal flag data can be incomplete or inconsistent across sources.

## Troubleshooting

- **Map is blank:** confirm the browser has internet access and that your network permits unpkg and CARTO tile requests. The game itself and local flags can still load without a map connection.
- **The builder is throttled or fails temporarily:** wait and rerun `--download-all` or a small `--download-batch`; its state file makes it resume instead of starting over. Do not lower the delay below one second or run parallel download processes.
- **Discovery reports zero eligible candidates:** read the `Eligibility check` line. The script now preserves the prior manifest rather than replacing it with an empty one; the summary identifies whether coordinates, file titles, missing image metadata, or raster quality caused the exclusions.
- **A catalog build refuses to run:** download more files, or lower `--limit`; the builder intentionally will not replace a working catalog with a partial one.
- **A flag is wrong or has poor quality:** remove or correct that candidate before publishing, then rerun the build stage. Keep the Commons source link for attribution.
