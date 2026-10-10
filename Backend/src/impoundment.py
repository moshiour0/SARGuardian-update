"""
impoundment.py
--------------
Find places where a landslide could dam a river, and how much water it would
hold.

Why this exists
===============
The 26 August 2026 Nepal cascade was not a single failure - it was ice/rock
detachment, then channel blockage, then breach, then a surge that raised the
Trishuli by metres in half an hour. The blockage step is the multiplier: it
converts a local slope failure into a downstream flood.

Blockage potential is a property of the *terrain*, so it can be mapped in
advance from a DEM alone, with no SAR at all. Cross that map with InSAR
deformation on the flanking slopes and you have a genuine two-factor alert:
a site that is both dammable and moving.

Method
======
1. Fill depressions (priority-flood) for routing purposes only.
2. D8 flow directions and flow accumulation.
3. Channel = cells whose upstream contributing area exceeds a threshold.
4. For each channel cell, treat it as a dam site. For a range of dam heights,
   flood the *upstream contributing area* to that water level and integrate
   the depth.
5. Rank by impounded volume; flag sites whose pool touches the AOI edge
   (truncated, so the volume is a lower bound).

DEM sources
===========
--dem FILE.tif    a local GeoTIFF (NASADEM, SRTM, Copernicus GLO-30). Proper.
--api             OpenTopoData SRTM 30 m over the AOI bbox. No download, no
                  credentials, rate-limited to ~100 points/second, so keep the
                  grid modest. Good enough to prototype; not production.

Limits worth stating in a paper
===============================
- A pool is only closed if it does not spill over a col into a neighbouring
  catchment. This tool does not test for cols. Volumes are therefore upper
  bounds for any site near a drainage divide.
- Dam height is imposed, not predicted. Whether a slope can actually deliver
  that volume of debris is a separate question - couple this to a runout model
  before calling a number a forecast.
- SRTM dates from 2000. In terrain reshaped since (the 2015 Gorkha avalanche
  through Langtang, for instance) the geometry is out of date.

Usage
-----
    python src/impoundment.py --api --aoi langtang --heights 50 100 150
    python src/impoundment.py --dem nasadem.tif --top 15 --geojson outputs/dams.geojson
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("impoundment")

AOIS = {
    # The confirmed 26 Aug 2026 source zone. Bounding box of SOURCE_RING in
    # gunw_reader; the failure point is at 28.28771 N, 85.52809 E.
    "source": (85.46452, 28.24529, 85.55619, 28.35290),
    "langtang": (85.45911, 28.24474, 85.56485, 28.32969),
    "lhende": (85.44, 28.34, 85.62, 28.47),
    # A second range, a second continent, and an outcome already on the record.
    # The Birch Glacier collapsed into the Loetschental on 28 May 2025, and the
    # deposit obstructed the Lonza and impounded a lake - a documented landslide
    # dam. Everything else here is calibrated against that event, so running the
    # susceptibility map over it is the one available test of whether this
    # travels beyond the Himalaya. Blatten village sits at 46.42028 N 7.81972 E.
    "blatten": (7.75, 46.38, 7.90, 46.46),
}

# D8 neighbour offsets (row, col) and their relative distances
NB = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
      (-1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)),
      (1, -1, math.sqrt(2)), (1, 1, math.sqrt(2))]


# ---------------------------------------------------------------------------
# DEM loading
# ---------------------------------------------------------------------------
def dem_from_api(bbox, target_cells: int = 12000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample SRTM 30 m from OpenTopoData over the bbox."""
    lon0, lat0, lon1, lat1 = bbox
    midlat = (lat0 + lat1) / 2
    w_km = (lon1 - lon0) * 111.32 * math.cos(math.radians(midlat))
    h_km = (lat1 - lat0) * 110.57
    aspect = w_km / h_km
    ny = int(round(math.sqrt(target_cells / aspect)))
    nx = int(round(target_cells / ny))

    lats = np.linspace(lat1, lat0, ny)          # north -> south, image order
    lons = np.linspace(lon0, lon1, nx)
    pts = [(la, lo) for la in lats for lo in lons]

    n_calls = (len(pts) + 99) // 100
    logger.info("Sampling %d x %d = %d cells (~%.0f m) via OpenTopoData: %d calls, ~%.0f s",
                ny, nx, len(pts), w_km * 1000 / nx, n_calls, n_calls * 1.15)

    z = []
    for k in range(0, len(pts), 100):
        chunk = pts[k:k + 100]
        loc = "|".join(f"{a:.5f},{b:.5f}" for a, b in chunk)
        url = "https://api.opentopodata.org/v1/srtm30m?locations=" + urllib.parse.quote(loc)
        for attempt in range(4):
            try:
                d = json.loads(urllib.request.urlopen(url, timeout=120).read().decode())
                z += [r.get("elevation") for r in d["results"]]
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                logger.warning("retry (%s)", exc)
                time.sleep(3)
        time.sleep(1.1)
        if (k // 100) % 20 == 0 and k:
            logger.info("  %d/%d", k, len(pts))

    arr = np.array([np.nan if v is None else v for v in z], dtype=float).reshape(ny, nx)
    return arr, lats, lons


def dem_from_file(path: Path, bbox=None):
    try:
        import rasterio
    except ImportError:
        logger.error("rasterio required for --dem:  pip install rasterio")
        raise SystemExit(1)
    with rasterio.open(path) as src:
        arr = src.read(1).astype(float)
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        h, w = arr.shape
        xs = np.array([src.xy(0, c)[0] for c in range(w)])
        ys = np.array([src.xy(r, 0)[1] for r in range(h)])
    return arr, ys, xs


# ---------------------------------------------------------------------------
# Hydrology
# ---------------------------------------------------------------------------
def fill_depressions(z: np.ndarray) -> np.ndarray:
    """Priority-flood depression filling (Barnes et al.). Routing only."""
    ny, nx = z.shape
    filled = np.full_like(z, np.inf)
    closed = np.zeros(z.shape, dtype=bool)
    heap: list[tuple[float, int, int]] = []

    for i in range(ny):
        for j in (0, nx - 1):
            if np.isfinite(z[i, j]):
                heapq.heappush(heap, (z[i, j], i, j)); filled[i, j] = z[i, j]; closed[i, j] = True
    for j in range(nx):
        for i in (0, ny - 1):
            if np.isfinite(z[i, j]) and not closed[i, j]:
                heapq.heappush(heap, (z[i, j], i, j)); filled[i, j] = z[i, j]; closed[i, j] = True

    while heap:
        zc, i, j = heapq.heappop(heap)
        for di, dj, _ in NB:
            ni, nj = i + di, j + dj
            if 0 <= ni < ny and 0 <= nj < nx and not closed[ni, nj] and np.isfinite(z[ni, nj]):
                filled[ni, nj] = max(z[ni, nj], zc)
                closed[ni, nj] = True
                heapq.heappush(heap, (filled[ni, nj], ni, nj))
    filled[~np.isfinite(z)] = np.nan
    return filled


def d8_receivers(filled: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Index of the steepest-descent neighbour for each cell (-1 if none)."""
    ny, nx = filled.shape
    recv = np.full(ny * nx, -1, dtype=np.int64)
    for i in range(ny):
        for j in range(nx):
            if not np.isfinite(filled[i, j]):
                continue
            best, best_idx = 0.0, -1
            for di, dj, dist in NB:
                ni, nj = i + di, j + dj
                if not (0 <= ni < ny and 0 <= nj < nx) or not np.isfinite(filled[ni, nj]):
                    continue
                length = dist * (dx if dj else dy) if di == 0 or dj == 0 else dist * (dx + dy) / 2
                slope = (filled[i, j] - filled[ni, nj]) / length
                if slope > best:
                    best, best_idx = slope, ni * nx + nj
            recv[i * nx + j] = best_idx
    return recv


def flow_accumulation(filled: np.ndarray, recv: np.ndarray) -> np.ndarray:
    """Number of upstream cells draining through each cell (including itself)."""
    ny, nx = filled.shape
    acc = np.ones(ny * nx, dtype=np.float64)
    acc[~np.isfinite(filled).ravel()] = 0.0
    order = np.argsort(-np.nan_to_num(filled.ravel(), nan=-np.inf))
    for idx in order:
        r = recv[idx]
        if r >= 0:
            acc[r] += acc[idx]
    return acc.reshape(ny, nx)


def upstream_of(idx: int, recv: np.ndarray, children: dict[int, list[int]]) -> np.ndarray:
    """All cell indices draining through idx, by BFS up the receiver tree."""
    out, stack = [idx], [idx]
    while stack:
        c = stack.pop()
        for ch in children.get(c, ()):
            out.append(ch); stack.append(ch)
    return np.array(out, dtype=np.int64)


# ---------------------------------------------------------------------------
def analyse(z, lats, lons, heights, min_accum_cells, top_n,
            min_separation_km=1.5, rank_by="volume"):
    ny, nx = z.shape
    midlat = float(np.mean(lats))
    dy = abs(float(lats[1] - lats[0])) * 110570.0
    dx = abs(float(lons[1] - lons[0])) * 111320.0 * math.cos(math.radians(midlat))
    cell_area = dx * dy
    logger.info("Grid %d x %d, cell %.0f x %.0f m (%.4f km2)", ny, nx, dx, dy, cell_area / 1e6)

    logger.info("Filling depressions...")
    filled = fill_depressions(z)
    logger.info("D8 flow directions...")
    recv = d8_receivers(filled, dx, dy)
    logger.info("Flow accumulation...")
    acc = flow_accumulation(filled, recv)

    children: dict[int, list[int]] = {}
    for idx, r in enumerate(recv):
        if r >= 0:
            children.setdefault(int(r), []).append(idx)

    channel = np.argwhere(acc >= min_accum_cells)
    logger.info("Channel cells (>= %d upstream cells): %d", min_accum_cells, len(channel))
    if len(channel) == 0:
        logger.error("No channel found - lower --min-accum.")
        return []

    zf = z.ravel()
    results = []
    for (i, j) in channel:
        idx = int(i) * nx + int(j)
        sill = z[i, j]
        if not np.isfinite(sill):
            continue
        up = upstream_of(idx, recv, children)
        zup = zf[up]
        good = np.isfinite(zup)
        up, zup = up[good], zup[good]
        if len(up) < 3:
            continue
        for H in heights:
            wl = sill + H
            flooded = zup < wl
            if flooded.sum() < 2:
                continue
            vol = float(np.sum(wl - zup[flooded]) * cell_area)
            area = float(flooded.sum() * cell_area)
            fi = up[flooded] // nx
            fj = up[flooded] % nx
            edge = bool((fi.min() == 0) or (fj.min() == 0) or
                        (fi.max() == ny - 1) or (fj.max() == nx - 1))
            back = float(math.hypot((fi.max() - fi.min()) * dy,
                                    (fj.max() - fj.min()) * dx) / 1000.0)
            results.append({
                "lat": float(lats[i]), "lon": float(lons[j]),
                "sill_m": float(sill), "dam_height_m": float(H),
                "volume_Mm3": vol / 1e6, "area_km2": area / 1e6,
                "backwater_km": back,
                "upstream_cells": int(len(up)),
                "upstream_area_km2": float(len(up) * cell_area / 1e6),
                "truncated": edge,
            })

    # Group by site so each location carries its full height response, not
    # just the biggest number.
    sites: dict[tuple, dict] = {}
    for r in results:
        key = (r["lat"], r["lon"])
        s_ = sites.setdefault(key, {k: v for k, v in r.items()
                                    if k not in ("dam_height_m", "volume_Mm3",
                                                 "area_km2", "backwater_km", "truncated")})
        s_.setdefault("by_height", {})[r["dam_height_m"]] = {
            "volume_Mm3": r["volume_Mm3"], "area_km2": r["area_km2"],
            "backwater_km": r["backwater_km"], "truncated": r["truncated"]}

    hs = sorted(heights)
    for s_ in sites.values():
        bh = s_["by_height"]
        s_["max_volume_Mm3"] = max(v["volume_Mm3"] for v in bh.values())
        # The smallest blockage this site responds to at all. A reach that needs
        # 100 m of debris before it holds anything is a different proposition
        # from one that starts filling at 10 m, and this is the number that
        # separates them.
        lo = min(bh)
        s_["threshold_height_m"] = lo
        s_["efficiency_Mm3_per_m"] = bh[lo]["volume_Mm3"] / lo
        s_["volume_at_min_height_Mm3"] = bh[lo]["volume_Mm3"]
        s_["truncated"] = any(v["truncated"] for v in bh.values())

    return rank_sites(list(sites.values()), rank_by, top_n, min_separation_km)


def rank_sites(sites: list[dict], rank_by: str, top_n: int,
               min_separation_km: float) -> list[dict]:
    """
    Order the sites and suppress near-duplicates.

    Pure: takes dicts, returns dicts, touches no DEM. The ranking is the part
    of this module that makes a claim - "favours sites a small landslide could
    dam" - so it has to be checkable without several thousand DEM queries.
    """
    if rank_by == "efficiency":
        # Threshold height FIRST, then volume per metre within it.
        #
        # Ranking on volume-per-metre alone did the opposite of what it claims.
        # `lo` differs between sites and impounded volume grows faster than
        # linearly with height, so dividing by a LARGER lo can still give a
        # bigger ratio - and a site that holds nothing until 100 m outranks
        # sites that start filling at 10 m. In the committed source-zone run
        # that is exactly what happened: site 2 needs a 100 m blockage and came
        # second of twelve, above four reaches that respond at 10 m.
        #
        # "Favours sites a small landslide could dam" has to mean the small
        # blockage comes first. Efficiency then orders the sites that share a
        # threshold, which is a like-for-like comparison because lo is equal.
        ranked = sorted(sites,
                        key=lambda r: (r["threshold_height_m"],
                                       -r["efficiency_Mm3_per_m"]))
    else:
        ranked = sorted(sites, key=lambda r: -r["max_volume_Mm3"])

    # Non-maximum suppression. Consecutive channel cells describe the SAME pool
    # with the dam nudged a few tens of metres, so without this one valley reach
    # fills the entire table with near-identical rows.
    keep: list[dict] = []
    for r in ranked:
        if any(math.hypot((r["lat"] - k["lat"]) * 110.57,
                          (r["lon"] - k["lon"]) * 111.32 * math.cos(math.radians(r["lat"])))
               < min_separation_km for k in keep):
            continue
        keep.append(r)
        if len(keep) >= top_n:
            break
    return keep


def report(rows, heights, rank_by):
    if not rows:
        print("\nNo impoundment sites found.")
        return
    hs = sorted(heights)
    basis = "volume per metre of blockage" if rank_by == "efficiency" else "maximum impounded volume"
    print("\nRanked by " + basis + "\n")

    head = f"{'#':>3}  {'LAT':>9}{'LON':>10}{'SILL m':>8}{'UPSTR km2':>10}"
    head += "".join(f"{int(h):>8}m" for h in hs)
    head += f"{'THRESH':>8}{'Mm3/m':>9}  FLAG"
    print(head)
    print("-" * len(head))

    for k, r in enumerate(rows, 1):
        line = (f"{k:>3}  {r['lat']:>9.4f}{r['lon']:>10.4f}"
                f"{r['sill_m']:>8.0f}{r['upstream_area_km2']:>10.1f}")
        for h in hs:
            v = r["by_height"].get(h)
            line += f"{v['volume_Mm3']:>9.1f}" if v else f"{'-':>9}"
        line += f"{r['threshold_height_m']:>7.0f}m{r['efficiency_Mm3_per_m']:>9.2f}  "
        line += "truncated" if r["truncated"] else ""
        print(line)

    print("\n  columns are impounded volume (Mm3) at each dam height")
    print("  Mm3/m = volume at the smallest height THIS SITE responds to, per")
    print("  metre of blockage. The threshold differs between sites, so this")
    print("  column compares like for like only within one threshold - which is")
    print("  why the ranking sorts on threshold first.")
    if rank_by == "efficiency":
        by_thr: dict[float, int] = {}
        for r in rows:
            by_thr[r["threshold_height_m"]] = by_thr.get(r["threshold_height_m"], 0) + 1
        summary = ", ".join(f"{n} at {h:g} m" for h, n in sorted(by_thr.items()))
        print(f"\n  threshold blockage: {summary}")
    print("\nScale reference:")
    print("  2024 Thame GLOF, Nepal            ~  2 Mm3")
    print("  2023 South Lhonak GLOF, Sikkim    ~ 50 Mm3  (~180 dead)")

    small = [r for r in rows if r["volume_at_min_height_Mm3"] >= 2]
    if small:
        print(f"\n  {len(small)} site(s) already impound >2 Mm3 with only a {min(hs):g} m blockage.")
        print("  Those are the realistic ones - a 150 m dam is a rare event, 25 m is not.")
    if any(r["truncated"] for r in rows):
        print("  'truncated' = pool reaches the AOI edge; volume is a LOWER bound.")


def write_geojson(rows, out: Path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")}}
        for r in rows]}
    out.write_text(json.dumps(fc, indent=1))
    logger.info("Wrote %s (%d sites)", out, len(rows))


def main() -> int:
    ap = argparse.ArgumentParser(description="Landslide-dam impoundment susceptibility")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--api", action="store_true", help="sample SRTM via OpenTopoData")
    src.add_argument("--dem", metavar="FILE.tif", help="local DEM GeoTIFF")
    ap.add_argument("--aoi", choices=sorted(AOIS), default="source",
                    help="terrain AOI. 'lhende' is the valley the 26 Aug 2026 avalanche dammed and breached, so its map is a testable prediction")
    ap.add_argument("--cells", type=int, default=12000, help="approx grid cells for --api")
    ap.add_argument("--heights", type=float, nargs="+", default=[25, 50, 100, 150])
    ap.add_argument("--min-accum", type=int, default=40,
                    help="upstream cells needed to count as channel")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-separation", type=float, default=1.5,
                    help="km; suppress sites closer than this to a better one")
    ap.add_argument("--rank-by", choices=("volume", "efficiency"), default="efficiency",
                    help="efficiency = volume per metre of blockage (default; "
                         "favours sites a small landslide could dam)")
    ap.add_argument("--geojson", metavar="OUT.geojson")
    args = ap.parse_args()

    bbox = AOIS[args.aoi]
    print(f"\nAOI {args.aoi}: {bbox}")
    print(f"Dam heights tested: {', '.join(f'{h:g} m' for h in args.heights)}")

    if args.api:
        z, lats, lons = dem_from_api(bbox, args.cells)
    else:
        z, lats, lons = dem_from_file(Path(args.dem))

    rows = analyse(z, lats, lons, args.heights, args.min_accum, args.top,
                   args.min_separation, args.rank_by)
    report(rows, args.heights, args.rank_by)

    if args.geojson and rows:
        write_geojson(rows, Path(args.geojson))

    print("\nCaveats: pools are not tested for spilling over cols, so volumes are")
    print("upper bounds near divides. Dam height is imposed, not predicted -")
    print("couple to a runout model before calling any number a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
