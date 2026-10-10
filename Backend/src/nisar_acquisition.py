"""
nisar_acquisition.py
--------------------
SARGuardian data acquisition for the Langtang / Lhende Khola AOIs (Rasuwa, Nepal).

Approach
========
NASA publishes NISAR *Level-2* products that are already coregistered, unwrapped
and geocoded:

    GUNW  Geocoded UNWrapped interferogram   -> LOS displacement, ready to use
    GOFF  Geocoded pixel OFFsets             -> large/fast displacement, no coherence needed
    GCOV  Geocoded covariance (backscatter)  -> amplitude change detection

So no SNAP, no ISCE2, no DEM, no orbit files, no burst handling for a first
displacement time series. Drop to RSLC + ISCE2 only for custom pair networks.

Search is stdlib-only and needs no credentials, so recon runs anywhere.
Download needs asf_search plus an Earthdata Login (env vars or ~/.netrc).

Usage
-----
    python nisar_acquisition.py --recon
    python nisar_acquisition.py --recon --sentinel1
    python nisar_acquisition.py --download GUNW GOFF
    python nisar_acquisition.py --watch --new-since 2026-08-27   # cron mode

Exit codes (for cron)
---------------------
    0   ran fine, nothing new
    10  new product(s) found since --new-since   <- alert on this
    1   error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nisar-acq")

# ---------------------------------------------------------------------------
# AOI. Imported, never redefined.
#
# This module used to carry its own copy of the rings, with no SOURCE_RING at
# all and no way to choose one but editing the file. Two consequences: the
# analysis AOI could not be searched or downloaded with the tool the
# reproduction page tells you to use, and the retracted claim that Lhende was
# "the reported source zone ~9 km north" survived here after it had been
# corrected everywhere else.
#
# One definition, in gunw_reader, for every module.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gunw_reader import AOIS  # noqa: E402

AOI_NAME = "source"
AOI_RING = AOIS[AOI_NAME]


def set_aoi(name: str) -> None:
    """Switch the AOI and rebuild everything derived from it."""
    global AOI_NAME, AOI_RING, AOI_WKT, BBOX, SEARCH_WKT
    if name not in AOIS:
        raise SystemExit(f"Unknown AOI '{name}'. Choose from {sorted(AOIS)}")
    AOI_NAME = name
    AOI_RING = AOIS[name]
    AOI_WKT = "POLYGON ((" + ", ".join(f"{x} {y}" for x, y in AOI_RING + [AOI_RING[0]]) + "))"
    lons = [c[0] for c in AOI_RING]
    lats = [c[1] for c in AOI_RING]
    BBOX = (min(lons), min(lats), max(lons), max(lats))
    SEARCH_WKT = (
        f"POLYGON(({BBOX[0]:.4f} {BBOX[1]:.4f},{BBOX[2]:.4f} {BBOX[1]:.4f},"
        f"{BBOX[2]:.4f} {BBOX[3]:.4f},{BBOX[0]:.4f} {BBOX[3]:.4f},"
        f"{BBOX[0]:.4f} {BBOX[1]:.4f}))"
    )


AOI_WKT = "POLYGON ((" + ", ".join(f"{x} {y}" for x, y in AOI_RING + [AOI_RING[0]]) + "))"
_lons = [c[0] for c in AOI_RING]
_lats = [c[1] for c in AOI_RING]
BBOX = (min(_lons), min(_lats), max(_lons), max(_lats))
SEARCH_WKT = (
    f"POLYGON(({BBOX[0]:.4f} {BBOX[1]:.4f},{BBOX[2]:.4f} {BBOX[1]:.4f},"
    f"{BBOX[2]:.4f} {BBOX[3]:.4f},{BBOX[0]:.4f} {BBOX[3]:.4f},{BBOX[0]:.4f} {BBOX[1]:.4f}))"
)

EVENT_DATE = date(2026, 8, 26)
NISAR_REPEAT_DAYS = 12

ASF_API = "https://api.daac.asf.alaska.edu/services/search/param"
RESULT_CAP = 2000          # ASF silently truncates here

# Default under data/ so downloads never land inside src/. Override with
# WORKSPACE_DIR, and run src/organise.py afterwards to sort by product.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA, resolve  # noqa: E402

# Anchored to the repository root, so downloads land in <repo>/data no matter
# which directory the command was run from.
WORKSPACE = resolve(os.getenv("WORKSPACE_DIR", "")) if os.getenv("WORKSPACE_DIR") else DATA

# NISAR product groups. Filtering server-side is essential: an unfiltered NISAR
# query over this AOI returns >2000 rows, of which ~1900 are ECMWF_SMST weather
# ancillaries that crowd out the products we actually want.
INTERFEROMETRIC = ["GUNW", "GOFF", "RUNW", "ROFF", "RIFG"]
ACQUISITION = ["RSLC", "GSLC", "GCOV"]


# ---------------------------------------------------------------------------
# Catalogue search - stdlib only, no credentials
# ---------------------------------------------------------------------------
def _get(url: str, retries: int = 4) -> dict | list:
    """GET with exponential backoff, so an unattended cron survives a blip."""
    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt < retries - 1:
                logger.warning("Query failed (%s), retrying in %.0fs...", exc, delay)
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"ASF query failed after {retries} attempts: {last}")


def asf_query(warn_on_cap: bool = True, **params) -> list[dict]:
    """
    Query the public ASF search API.

    The API silently truncates at RESULT_CAP with no indication in the payload,
    so we detect it and say so. Prefer narrowing with processingLevel over
    raising the cap.
    """
    payload = {
        "intersectsWith": SEARCH_WKT,
        "output": "jsonlite",
        "maxResults": str(RESULT_CAP),
    }
    payload.update({k: v for k, v in params.items() if v is not None})
    body = _get(f"{ASF_API}?{urllib.parse.urlencode(payload)}")
    rows = body.get("results", body if isinstance(body, list) else [])

    if warn_on_cap and len(rows) >= RESULT_CAP:
        logger.error(
            "RESULTS TRUNCATED at %d - this query is incomplete. "
            "Narrow it with processingLevel= or a shorter date range.",
            RESULT_CAP,
        )
    return rows


def query_by_year(start: str, end: str, **params) -> list[dict]:
    """
    Chunk a long date range into calendar years to stay under the cap.
    Deduplicates on granuleName across chunks.
    """
    first = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    seen: dict[str, dict] = {}
    year = first.year
    while year <= last.year:
        lo = max(first, date(year, 1, 1))
        hi = min(last, date(year, 12, 31))
        for row in asf_query(start=f"{lo}T00:00:00Z", end=f"{hi}T23:59:59Z", **params):
            seen[row["granuleName"]] = row
        year += 1
    return list(seen.values())


def parse_pair_dates(granule: str) -> tuple[date, date] | None:
    """NISAR L2 names carry reference start/stop then secondary start/stop."""
    stamps = re.findall(r"_(\d{8})T\d{6}", granule)
    if len(stamps) < 4:
        return None
    return (
        datetime.strptime(stamps[0], "%Y%m%d").date(),
        datetime.strptime(stamps[2], "%Y%m%d").date(),
    )


def filename_from_url(url: str) -> str | None:
    """
    Take the on-disk name from the URL rather than assuming an extension.

    Today ASF serves NISAR L2 as '<granuleName>.h5', so this matches what
    asf_search writes. Deriving it costs nothing and keeps the resume check
    correct if the DAAC ever changes packaging or we add other products.
    """
    if not url:
        return None
    name = Path(urllib.parse.urlparse(url).path).name
    return name or None


# ---------------------------------------------------------------------------
# Reconnaissance
# ---------------------------------------------------------------------------
def recon_nisar(start: str = "2025-01-01", end: str | None = None) -> list[dict]:
    end = end or date.today().isoformat()
    logger.info("Querying NISAR interferometric products (%s to %s)...", start, end)

    rows = query_by_year(start, end, platform="NISAR", processingLevel=",".join(INTERFEROMETRIC))
    if not rows:
        logger.warning("No NISAR interferometric products over this AOI yet.")
        return []

    records = []
    for row in rows:
        pair = parse_pair_dates(row["granuleName"])
        if not pair:
            continue
        reference, secondary = pair
        records.append(
            {
                "productType": row.get("productType"),
                "direction": (row.get("flightDirection") or "?")[0],
                "path": row.get("path"),
                "reference": reference,
                "secondary": secondary,
                "span_days": (secondary - reference).days,
                "days_before_event": (EVENT_DATE - secondary).days,
                "granule": row["granuleName"],
                "url": row.get("downloadUrl"),
                "size_mb": row.get("sizeMB"),
            }
        )
    records.sort(key=lambda r: (r["secondary"], r["productType"]))

    gunw = [r for r in records if r["productType"] == "GUNW"]
    print(f"\nGUNW pairs: {len(gunw)}   all products: {len(records)}   "
          f"paths: {sorted({r['path'] for r in records if r['path'] is not None})}")
    print(f"\n{'PROD':<6}{'DIR':>4}{'PATH':>6}  {'REFERENCE':<12}{'SECONDARY':<12}{'SPAN':>7}{'PRE-EVENT':>11}")
    print("-" * 62)
    for r in gunw:
        marker = "  <-- precursory" if 0 <= r["days_before_event"] <= 20 else ""
        print(
            f"{r['productType']:<6}{r['direction']:>4}{r['path']:>6}  "
            f"{str(r['reference']):<12}{str(r['secondary']):<12}"
            f"{r['span_days']:>5} d{r['days_before_event']:>9} d{marker}"
        )

    acq_rows = query_by_year(start, end, platform="NISAR", processingLevel=",".join(ACQUISITION))
    acq = sorted({r["startTime"][:10] for r in acq_rows if r.get("startTime")})
    if acq:
        last = datetime.strptime(acq[-1], "%Y-%m-%d").date()
        logger.info("Latest NISAR acquisition: %s (%d dates on file)", last, len(acq))
        if last < EVENT_DATE:
            logger.warning(
                "CO-EVENT PAIR NOT YET PUBLISHED. Next repeat ~%s - that "
                "interferogram spans the collapse. Keep polling.",
                last + timedelta(days=NISAR_REPEAT_DAYS),
            )
    return records


def recon_sentinel1(start: str = "2025-01-01", end: str | None = None) -> None:
    """Track coverage for BOTH orbit directions - never search one only."""
    end = end or date.today().isoformat()
    logger.info("Querying Sentinel-1 SLC coverage (%s to %s)...", start, end)

    rows = query_by_year(start, end, platform="SENTINEL-1", processingLevel="SLC", beamMode="IW")
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        buckets[(row["flightDirection"], row["path"])].append(row)

    print(f"\n{'DIRECTION':<12}{'TRACK':>6}{'FRAMES':>9}{'DATES':>7}  {'MISSIONS':<14}{'MED GAP':>9}{'LATEST':>13}")
    print("-" * 74)
    for (direction, path), scenes in sorted(buckets.items()):
        days = sorted({s["startTime"][:10] for s in scenes})
        parsed = [datetime.strptime(d, "%Y-%m-%d") for d in days]
        gaps = sorted((parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1))
        median = gaps[len(gaps) // 2] if gaps else 0
        frames = ",".join(str(f) for f in sorted({s["frame"] for s in scenes}))
        missions = ",".join(sorted({s["dataset"].replace("Sentinel-", "S") for s in scenes}))
        print(f"{direction:<12}{path:>6}{frames:>9}{len(days):>7}  {missions:<14}{median:>7} d{days[-1]:>13}")

    total_gb = sum(s.get("sizeMB") or 0 for s in rows) / 1024
    print(f"\n{len(rows)} SLC scenes, ~{total_gb:.0f} GB if all downloaded - pick one track.")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def build_session():
    """
    Earthdata session.

    Uses EARTHDATA_USERNAME / EARTHDATA_PASSWORD when present, otherwise falls
    back to ~/.netrc, which is what GDAL, earthaccess and most geospatial tools
    already expect. Never hardcode credentials.
    """
    import asf_search as asf

    username = os.getenv("EARTHDATA_USERNAME")
    password = os.getenv("EARTHDATA_PASSWORD")

    if username and password:
        logger.info("Authenticating with Earthdata via environment variables...")
        return asf.ASFSession().auth_with_creds(username, password)

    netrc_path = Path.home() / ("_netrc" if os.name == "nt" else ".netrc")
    if netrc_path.exists():
        logger.info("No env credentials; falling back to %s", netrc_path)
    else:
        logger.warning(
            "No EARTHDATA_USERNAME/PASSWORD and no %s found. "
            "Download will fail unless the DAAC allows anonymous access.",
            netrc_path,
        )
    return asf.ASFSession()


def download(product_types: list[str], records: list[dict]) -> None:
    try:
        import asf_search as asf
    except ImportError:
        logger.error("asf_search is required to download.  pip install asf_search")
        raise SystemExit(1)

    wanted = [r for r in records if r["productType"] in product_types]
    if not wanted:
        logger.warning("Nothing matches %s", product_types)
        return

    outdir = WORKSPACE / "nisar_l2" / "_incoming"
    outdir.mkdir(parents=True, exist_ok=True)
    session = build_session()

    total_mb = sum(r.get("size_mb") or 0 for r in wanted)
    logger.info("Downloading %d products (~%.0f MB) to %s", len(wanted), total_mb, outdir)

    for record in wanted:
        name = filename_from_url(record["url"])
        if not name:
            logger.warning("No download URL for %s", record["granule"])
            continue
        target = outdir / name
        if target.exists() and target.stat().st_size > 0:
            logger.info("Present, skipping: %s", name)
            continue
        logger.info(
            "Fetching %s %s path %s  %s -> %s",
            record["productType"], record["direction"], record["path"],
            record["reference"], record["secondary"],
        )
        asf.download_url(url=record["url"], path=str(outdir), session=session)


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="NISAR acquisition for the Rasuwa AOIs")
    parser.add_argument("--aoi", choices=sorted(AOIS), default="source",
                        help="which AOI to search. 'source' is the confirmed "
                             "detachment zone; 'lhende' is the runout corridor "
                             "the avalanche dammed")
    parser.add_argument("--recon", action="store_true", help="inventory available products")
    parser.add_argument("--sentinel1", action="store_true", help="also show Sentinel-1 track coverage")
    parser.add_argument("--download", nargs="+", metavar="TYPE", help="download product types, e.g. GUNW GOFF")
    parser.add_argument("--watch", action="store_true", help="cron mode: exit 10 if new products appeared")
    parser.add_argument("--new-since", metavar="YYYY-MM-DD", help="treat secondary dates after this as new")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--span", type=int, metavar="DAYS",
                        help="only pairs with this temporal baseline, e.g. 24. "
                             "A 24-day pair spanning two existing 12-day steps "
                             "closes a loop and gives the network redundancy.")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what --download would fetch, and its size")
    args = parser.parse_args()
    set_aoi(args.aoi)

    if not (args.recon or args.download or args.watch):
        parser.print_help()
        return 0

    print(f"\nAOI bbox : {BBOX[0]:.4f}, {BBOX[1]:.4f}, {BBOX[2]:.4f}, {BBOX[3]:.4f}")
    print(f"Event    : {EVENT_DATE}")

    try:
        records = recon_nisar(start=args.start)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    if args.sentinel1:
        recon_sentinel1(start=args.start)

    if args.span:
        before = len(records)
        records = [r for r in records if r["span_days"] == args.span]
        print(f"\n--span {args.span}: {len(records)} of {before} products have a "
              f"{args.span}-day baseline")
        if not records:
            print("  Nothing at that baseline. Run --recon to see what exists.")
            return 1

    if args.download:
        wanted = [r for r in records
                  if r["productType"] in [t.upper() for t in args.download]]
        if args.dry_run:
            mb = sum(r.get("size_mb") or 0 for r in wanted)
            print(f"\nDRY RUN - {len(wanted)} product(s), ~{mb/1024:.1f} GB\n")
            print(f"  {'PRODUCT':<8}{'DIR':>4}{'PATH':>6}  {'REFERENCE':<12}"
                  f"{'SECONDARY':<12}{'SPAN':>6}{'MB':>8}")
            print("  " + "-" * 58)
            for r in sorted(wanted, key=lambda r: (r["reference"], r["path"])):
                print(f"  {r['productType']:<8}{r['direction'][0]:>4}{r['path']:>6}  "
                      f"{str(r['reference']):<12}{str(r['secondary']):<12}"
                      f"{r['span_days']:>4} d{r.get('size_mb') or 0:>8.0f}")
            print("\nRe-run without --dry-run to fetch these.")
            return 0
        download([t.upper() for t in args.download], records)

    if args.watch and args.new_since:
        cutoff = datetime.strptime(args.new_since, "%Y-%m-%d").date()
        fresh = [r for r in records if r["secondary"] > cutoff]
        if fresh:
            logger.warning("%d NEW product(s) since %s:", len(fresh), cutoff)
            for r in fresh:
                logger.warning("  %s %s path %s  %s -> %s",
                               r["productType"], r["direction"], r["path"],
                               r["reference"], r["secondary"])
            return 10
        logger.info("No new products since %s.", cutoff)

    return 0


if __name__ == "__main__":
    sys.exit(main())
