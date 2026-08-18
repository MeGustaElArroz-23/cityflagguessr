#!/usr/bin/env python3
"""Build a local municipal-flag catalog without overloading Wikimedia services.

Use this as a resumable three-stage pipeline:
  1. --prepare-manifest: fetch candidate metadata in Commons API batches.
  2. --download-batch N or --download-all: download pending files serially.
  3. --build-catalog: write the playable data file from completed downloads.

Combining --prepare-manifest and --download-all automatically publishes a new
catalog when every candidate completes successfully.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPARQL_ENDPOINTS = (
    ("Wikidata Query Service", "https://query.wikidata.org/sparql"),
    ("QLever Wikidata mirror", "https://qlever.dev/api/wikidata"),
)
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
DEFAULT_MIN_RASTER_HEIGHT = 300
DEFAULT_REQUEST_DELAY = 2.0
DEFAULT_MAX_RETRIES = 6
COMMONS_BATCH_SIZE = 50
WIKIDATA_PAGE_SIZE = 500

# Wikidata's P17 may include a historical or disputed-country statement alongside
# a contemporary one. Keep the playable location labels current and unambiguous.
COUNTRY_OVERRIDES = {
    "Q11739": "Pakistan",  # Lahore
    "Q25270": "Kosovo",  # Pristina
    "Q2734": "Poland",  # Skierniewice
}
HISTORICAL_COUNTRY_LABELS = {
    "Austro-Hungarian Empire", "Czechoslovakia", "German Democratic Republic",
    "Kingdom of Yugoslavia", "Polish–Lithuanian Commonwealth", "Soviet Union",
    "Russian Socialist Federative Soviet Republic", "Yugoslavia",
}

USER_AGENT = ""
REQUEST_DELAY = DEFAULT_REQUEST_DELAY
MAX_RETRIES = DEFAULT_MAX_RETRIES


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def progress_counts(candidates, state) -> tuple[int, int, int]:
    completed = sum(
        state["files"].get(candidate["wikidata"], {}).get("status") == "complete"
        and (ROOT / candidate["flag"]).exists()
        for candidate in candidates
    )
    failed = sum(
        state["files"].get(candidate["wikidata"], {}).get("status") == "failed"
        for candidate in candidates
    )
    return completed, failed, len(candidates) - completed - failed


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def update_progress(state, candidates, started_at: float) -> tuple[int, int, int]:
    completed, failed, pending = progress_counts(candidates, state)
    state["progress"] = {
        "total": len(candidates), "completed": completed, "failed": failed, "pending": pending,
        "updatedAt": now(), "elapsedSecondsThisRun": round(time.monotonic() - started_at),
    }
    return completed, failed, pending


def retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, urllib.error.HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
    return min(300.0, max(REQUEST_DELAY, 2 ** attempt) + random.uniform(0, 1))


def error_summary(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        detail = getattr(error, "cityflag_detail", "")
        if not detail:
            try:
                detail = re.sub(r"\s+", " ", error.read(600).decode("utf-8", "replace").strip())
            except OSError:
                detail = ""
            error.cityflag_detail = detail
        summary = f"HTTP {error.code} {error.reason}"
        return f"{summary} — {detail[:300]}" if detail else summary
    return str(error)


def request(
    url: str,
    data: bytes | None = None,
    content_type: str | None = None,
    label: str = "Request",
    max_attempts: int | None = None,
):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json, application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    attempts = max_attempts or MAX_RETRIES
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            retryable = not isinstance(error, urllib.error.HTTPError) or error.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == attempts - 1:
                raise
            delay = retry_delay(error, attempt)
            print(
                f"{label}: attempt {attempt + 1}/{attempts} failed ({error_summary(error)}). "
                f"Retrying in {delay:.1f}s…",
                file=sys.stderr,
            )
            time.sleep(delay)


def get_json(
    url: str,
    data: bytes | None = None,
    content_type: str | None = None,
    label: str = "Request",
    max_attempts: int | None = None,
):
    attempts = max_attempts or MAX_RETRIES
    for attempt in range(attempts):
        try:
            with request(url, data=data, content_type=content_type, label=label, max_attempts=attempts) as response:
                return json.load(response)
        except json.JSONDecodeError as error:
            if attempt == attempts - 1:
                raise urllib.error.URLError(f"Malformed JSON response after {attempts} attempts: {error}") from error
            delay = retry_delay(error, attempt)
            print(
                f"{label}: attempt {attempt + 1}/{attempts} returned malformed JSON ({error.msg} at character {error.pos}). "
                f"Retrying in {delay:.1f}s…",
                file=sys.stderr,
            )
            time.sleep(delay)


def commons_images(file_titles: list[str]):
    """Return image metadata for up to 50 files in one Commons API request."""
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "maxlag": "5", "prop": "imageinfo",
        "iiprop": "url|mime|size|sha1", "iiurlwidth": "900",
        "titles": "|".join(f"File:{title}" for title in file_titles),
    })
    data = get_json(f"{COMMONS_API}?{params}", label="Commons metadata")
    images = {}
    for page in data.get("query", {}).get("pages", {}).values():
        title = page.get("title", "").removeprefix("File:")
        info = (page.get("imageinfo") or [{}])[0]
        mime = info.get("mime", "")
        if mime == "image/svg+xml":
            image_url, extension = info.get("url"), "svg"
        elif mime == "image/gif":
            image_url, extension = info.get("thumburl") or info.get("url"), "gif"
        else:
            image_url, extension = info.get("thumburl") or info.get("url"), "png"
        images[title] = {
            "imageUrl": image_url, "mime": mime, "extension": extension,
            "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
            "sha1": info.get("sha1", ""),
        }
    return images


def wikidata_rows(candidate_limit: int, endpoint_choice: str):
    query = """PREFIX wd: <http://www.wikidata.org/entity/>
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    PREFIX p: <http://www.wikidata.org/prop/>
    PREFIX ps: <http://www.wikidata.org/prop/statement/>
    PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT DISTINCT ?municipality ?municipalityLabel ?country ?countryLabel ?coord ?flag WHERE {
      VALUES ?settlementType { wd:Q515 wd:Q532 wd:Q3957 wd:Q5084 }
      ?municipality wdt:P31 ?settlementType; wdt:P625 ?coord; wdt:P41 ?flag.
      OPTIONAL {
        ?municipality p:P17 ?countryStatement.
        ?countryStatement ps:P17 ?country.
        FILTER NOT EXISTS { ?countryStatement pq:P582 ?countryEndDate. }
        ?country rdfs:label ?countryLabel. FILTER(LANG(?countryLabel) = "en")
      }
      ?municipality rdfs:label ?municipalityLabel. FILTER(LANG(?municipalityLabel) = "en")
    } ORDER BY STR(?municipality)"""
    endpoints = SPARQL_ENDPOINTS if endpoint_choice == "auto" else [
        endpoint for endpoint in SPARQL_ENDPOINTS if endpoint[0].lower().startswith(endpoint_choice)
    ]
    page_count = (candidate_limit + WIKIDATA_PAGE_SIZE - 1) // WIKIDATA_PAGE_SIZE
    print(
        f"Discovering up to {candidate_limit:,} rows in {page_count} page(s) of up to {WIKIDATA_PAGE_SIZE} from "
        f"{', '.join(name for name, _ in endpoints)}.",
        file=sys.stderr,
    )
    for endpoint_name, endpoint_url in endpoints:
        try:
            print(f"Trying {endpoint_name}…", file=sys.stderr)
            rows = []
            for offset in range(0, candidate_limit, WIKIDATA_PAGE_SIZE):
                page_limit = min(WIKIDATA_PAGE_SIZE, candidate_limit - offset)
                page_query = f"{query} LIMIT {page_limit} OFFSET {offset}"
                page_number = offset // WIKIDATA_PAGE_SIZE + 1
                page_label = f"{endpoint_name} page {page_number}/{page_count}"
                print(f"{page_label}: requesting up to {page_limit} rows…", file=sys.stderr)
                payload = get_json(
                    endpoint_url,
                    data=page_query.encode("utf-8"),
                    content_type="application/sparql-query",
                    label=page_label,
                    max_attempts=min(2, MAX_RETRIES),
                )
                page_rows = payload.get("results", {}).get("bindings", [])
                rows.extend(page_rows)
                print(f"{page_label}: received {len(page_rows)} row(s), {len(rows):,} total.", file=sys.stderr)
                if len(page_rows) < page_limit:
                    break
                if offset + page_limit < candidate_limit:
                    time.sleep(REQUEST_DELAY)
            if rows:
                print(f"{endpoint_name}: discovery completed with {len(rows):,} row(s).", file=sys.stderr)
                return rows
            raise urllib.error.URLError("The query returned no usable SPARQL result rows")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            print(
                f"{endpoint_name} failed on page {page_number}/{page_count}: {error_summary(error)}. "
                "Trying the next configured endpoint…",
                file=sys.stderr,
            )
    raise RuntimeError("All Wikidata query endpoints failed")


def prepare_manifest(args) -> None:
    rows = wikidata_rows(args.candidate_limit, args.query_endpoint)
    normalized = []
    titles = []
    for row in rows:
        title = urllib.parse.unquote(row["flag"]["value"]).rsplit("/", 1)[-1].replace("_", " ")
        normalized.append((row, title))
        if title not in titles:
            titles.append(title)
    images = {}
    for start in range(0, len(titles), COMMONS_BATCH_SIZE):
        print(f"Resolving Commons metadata {start + 1}-{min(start + COMMONS_BATCH_SIZE, len(titles))}/{len(titles)}…", file=sys.stderr)
        images.update(commons_images(titles[start:start + COMMONS_BATCH_SIZE]))
        time.sleep(REQUEST_DELAY)

    candidates, seen, rejected = [], set(), Counter()
    excluded_titles = re.compile(r"\b(country|state|territory|national|province|governorate|oblast|emirate|region)\b", re.IGNORECASE)
    for row, title in normalized:
        qid = row["municipality"]["value"].rsplit("/", 1)[-1]
        if qid in seen or row.get("country", {}).get("value", "").rsplit("/", 1)[-1] == qid:
            rejected["duplicate or self-referencing municipality"] += 1
            continue
        info = images.get(title, {})
        coordinate = re.search(r"point\(([-0-9.]+) ([-0-9.]+)\)", row["coord"]["value"], re.IGNORECASE)
        if not coordinate:
            rejected["unreadable coordinates"] += 1
            continue
        if excluded_titles.search(title):
            rejected["country/state-style flag title"] += 1
            continue
        if not info.get("imageUrl", "").startswith("https://upload.wikimedia.org/"):
            rejected["missing usable Commons image metadata"] += 1
            continue
        if info["mime"] != "image/svg+xml" and info["height"] < args.min_raster_height:
            rejected[f"raster below {args.min_raster_height}px"] += 1
            continue
        lon, lat = map(float, coordinate.groups())
        name = row["municipalityLabel"]["value"]
        country = COUNTRY_OVERRIDES.get(qid, row.get("countryLabel", {}).get("value", "Unknown"))
        if country in HISTORICAL_COUNTRY_LABELS:
            continue
        candidates.append({
            "id": f"{safe_id(name)}-{qid.lower()}", "name": name,
            "country": country, "lat": lat, "lon": lon,
            "flag": f"assets/flags/{safe_id(name)}-{qid.lower()}.{info['extension']}",
            "source": f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(title)}",
            "sourceLabel": "Wikimedia Commons", "wikidata": qid, "fileTitle": title,
            "imageUrl": info["imageUrl"], "mime": info["mime"], "sourceWidth": info["width"],
            "sourceHeight": info["height"], "sha1": info["sha1"],
        })
        seen.add(qid)
    summary = "; ".join(f"{count:,} {reason}" for reason, count in rejected.most_common()) or "no rejections"
    print(f"Eligibility check: {len(candidates):,} eligible candidate(s); rejected: {summary}.", file=sys.stderr)
    if not candidates:
        raise RuntimeError(
            "Discovery produced no eligible flags, so the existing manifest was left unchanged. "
            "See the eligibility summary above for the rejection reason."
        )
    write_json(args.manifest, {"version": 1, "generatedAt": now(), "minRasterHeight": args.min_raster_height, "candidates": candidates})
    print(f"Saved {len(candidates)} eligible candidates to {args.manifest}")


def download_candidate(candidate, state, args) -> bool:
    key = candidate["wikidata"]
    destination = ROOT / candidate["flag"]
    previous = state["files"].get(key, {})
    if previous.get("status") == "complete" and previous.get("sha1") == candidate["sha1"] and destination.exists():
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        time.sleep(REQUEST_DELAY)
        with request(candidate["imageUrl"], label=f"Download {candidate['name']}") as response:
            temporary = destination.with_name(f"{destination.name}.{os.getpid()}.part")
            with temporary.open("wb") as output:
                output.write(response.read())
            temporary.replace(destination)
            state["files"][key] = {"status": "complete", "sha1": candidate["sha1"], "etag": response.headers.get("ETag", ""), "updatedAt": now()}
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        state["files"][key] = {"status": "failed", "sha1": candidate["sha1"], "error": str(error), "updatedAt": now()}
        print(f"Deferred {candidate['name']}: {error}", file=sys.stderr)
        return False


def download_batch(args) -> None:
    manifest = read_json(args.manifest, {"candidates": []})
    if not manifest.get("candidates"):
        raise RuntimeError("Manifest is missing or empty; run --prepare-manifest first")
    state = read_json(args.state_file, {"version": 1, "files": {}})
    attempted = completed = 0
    pending = [
        candidate for candidate in manifest["candidates"]
        if state["files"].get(candidate["wikidata"], {}).get("status") != "complete"
        or not (ROOT / candidate["flag"]).exists()
    ]
    requested = len(pending) if args.download_all else min(args.download_batch, len(pending))
    mode = "all" if args.download_all else str(args.download_batch)
    print(f"Downloading up to {requested} pending files ({mode} mode)…", file=sys.stderr)
    started_at = time.monotonic()
    for candidate in manifest["candidates"]:
        status = state["files"].get(candidate["wikidata"], {}).get("status")
        if status == "complete" and (ROOT / candidate["flag"]).exists():
            continue
        if not args.download_all and attempted >= args.download_batch:
            break
        attempted += 1
        completed += int(download_candidate(candidate, state, args))
        total_complete, total_failed, total_pending = update_progress(state, manifest["candidates"], started_at)
        write_json(args.state_file, state)
        percent = (attempted / requested * 100) if requested else 100
        elapsed = time.monotonic() - started_at
        eta = (elapsed / attempted) * (requested - attempted) if attempted else 0
        print(
            f"[{attempted}/{requested} · {percent:5.1f}%] {candidate['name']} | "
            f"catalog progress: {total_complete} complete, {total_failed} failed, {total_pending} pending | "
            f"ETA {format_duration(eta)}",
            file=sys.stderr,
        )
    total_complete, total_failed, total_pending = update_progress(state, manifest["candidates"], started_at)
    write_json(args.state_file, state)
    print(
        f"Downloaded {completed}/{attempted} requested files. Catalog progress: "
        f"{total_complete} complete, {total_failed} failed, {total_pending} pending. "
        f"Progress is stored in {args.state_file}",
    )


def build_catalog(args) -> None:
    manifest = read_json(args.manifest, {"candidates": []})
    state = read_json(args.state_file, {"files": {}})
    records = []
    for candidate in manifest.get("candidates", []):
        completed = state["files"].get(candidate["wikidata"], {})
        if completed.get("status") != "complete" or not (ROOT / candidate["flag"]).exists():
            continue
        record = {key: value for key, value in candidate.items() if key not in {"fileTitle", "imageUrl", "mime", "sha1"}}
        record["country"] = COUNTRY_OVERRIDES.get(candidate["wikidata"], record["country"])
        records.append(record)
        if len(records) >= args.limit:
            break
    if len(records) < args.limit:
        raise RuntimeError(f"Only {len(records)} completed files are available; catalog was preserved")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("window.MUNICIPALITIES = " + json.dumps(records, ensure_ascii=False, indent=2) + ";\n", encoding="utf-8")
    if args.clean_assets:
        keep = {ROOT / record["flag"] for record in records}
        for asset in (ROOT / "assets" / "flags").iterdir():
            if asset.is_file() and asset not in keep:
                asset.unlink()
    print(f"Wrote {len(records)} records to {args.output}")


def publish_download_all(args) -> None:
    manifest = read_json(args.manifest, {"candidates": []})
    state = read_json(args.state_file, {"files": {}})
    completed, failed, pending = progress_counts(manifest.get("candidates", []), state)
    if failed or pending:
        print(
            f"Automatic publish skipped: {completed} complete, {failed} failed, {pending} pending. "
            "The active catalog was preserved; rerun --download-all to finish pending files.",
            file=sys.stderr,
        )
        return
    if not completed:
        raise RuntimeError("Automatic publish found no completed files; the active catalog was preserved")
    args.limit = completed
    print(f"All {completed} candidates completed. Automatically publishing the active catalog…", file=sys.stderr)
    build_catalog(args)


def main() -> None:
    global USER_AGENT, REQUEST_DELAY, MAX_RETRIES
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-manifest", action="store_true", help="Discover and save eligible candidate metadata only")
    parser.add_argument("--download-batch", type=int, metavar="N", help="Download at most N pending manifest files")
    parser.add_argument("--download-all", action="store_true", help="Download every pending manifest file serially (may run for a long time)")
    parser.add_argument("--build-catalog", action="store_true", help="Build the game catalog from completed downloads")
    parser.add_argument("--candidate-limit", type=int, default=1000, help="Wikidata candidates to inspect when preparing a manifest")
    parser.add_argument("--query-endpoint", choices=("auto", "wikidata", "qlever"), default="auto", help="Discovery endpoint: auto tries Wikidata then QLever")
    parser.add_argument("--limit", type=int, default=2000, help="Completed records required when building the game catalog")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data" / "flag-manifest.json")
    parser.add_argument("--state-file", type=Path, default=ROOT / "data" / "flag-download-state.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "municipalities.js")
    parser.add_argument("--clean-assets", action="store_true", help="Remove local flag files not referenced by the built catalog")
    parser.add_argument("--min-raster-height", type=int, default=DEFAULT_MIN_RASTER_HEIGHT)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY, help="Minimum seconds between network requests (default: 2)")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--user-agent", required=True, help="Descriptive agent with a contact URL or email")
    args = parser.parse_args()
    if not (args.prepare_manifest or args.download_batch is not None or args.download_all or args.build_catalog):
        parser.error("select at least one stage: --prepare-manifest, --download-batch N, --download-all, or --build-catalog")
    if args.download_batch is not None and args.download_all:
        parser.error("choose either --download-batch N or --download-all, not both")
    if args.download_batch is not None and args.download_batch < 1:
        parser.error("--download-batch must be at least 1")
    if args.candidate_limit < 1:
        parser.error("--candidate-limit must be at least 1")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.min_raster_height < 1:
        parser.error("--min-raster-height must be at least 1")
    if args.request_delay < 1:
        parser.error("--request-delay must be at least one second")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1")
    if "@" not in args.user_agent and "http" not in args.user_agent:
        parser.error("--user-agent must include a contact URL or email")
    USER_AGENT, REQUEST_DELAY, MAX_RETRIES = args.user_agent, args.request_delay, args.max_retries
    auto_publish = args.prepare_manifest and args.download_all and not args.build_catalog
    if args.prepare_manifest:
        prepare_manifest(args)
    if args.download_batch is not None:
        download_batch(args)
    if args.download_all:
        download_batch(args)
    if args.build_catalog:
        build_catalog(args)
    elif auto_publish:
        publish_download_all(args)


if __name__ == "__main__":
    main()
