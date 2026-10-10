#!/usr/bin/env python3
"""
build_data.py
-------------
Everything the interactive site needs, extracted from the same measurements the
command-line tools produce.

    python docs/site/build_data.py --out docs/site/site_data.json

Why this exists
===============
The site lets a visitor run the analysis themselves, in the browser. For that
to be worth anything the browser has to do the arithmetic - not replay a
recording of it - so this ships the INPUTS to each computation, never the
answers, plus a small block of reference values the page checks itself against.

Two kinds of step, and the site labels which is which:

    LIVE          the browser computes it: noise floors from real offset
                  samples, connected components on the real change grid, look
                  geometry, the inverse-velocity fit, the forecast cutoff.
    PRE-COMPUTED  anything that had to read the 51 GB archive or fetch
                  Sentinel-2. Those results are carried here as data.

The change grid travels as ONE RGB PNG rather than three arrays, because it is
three spatially-correlated 602x457 planes and PNG is very good at exactly that:

    R   delta NDSI, mapped from [-1, +1] onto 0..255
    G   pre-event NDSI, same mapping
    B   255 where the pixel is usable in BOTH epochs, else 0

The browser reads it back with getImageData and thresholds it. That is the same
operation scar_map.py performs, on the same numbers, at the same 20 m posting.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import logging
import math
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("build_data")

EVENT = datetime(2026, 8, 26).date()
ASSUMED = (28.28771, 85.52809)
GRID = (602, 457)

PRE_SCENE = "S2C_45RUM_20260812_0_L2A"
POST_SCENES = ["S2C_45RUM_20260908_0_L2A", "S2B_45RUM_20260906_0_L2A",
               "S2B_45RUM_20260903_0_L2A", "S2C_45RUM_20260901_0_L2A",
               "S2A_45RUM_20260831_0_L2A", "S2C_45RUM_20260829_0_L2A",
               "S2B_45RUM_20260827_0_L2A"]

# The eight ascending path 098 acquisitions. Export filenames carry no track
# field, which is the trap local_floor.py documents, so the dates are the key.
ASC_DATES = ["20251128", "20251210", "20251222", "20260103",
             "20260702", "20260714", "20260726", "20260819"]


def q(a: np.ndarray) -> np.ndarray:
    """[-1, +1] -> 0..255, with anything non-finite pinned to the midpoint."""
    x = np.where(np.isfinite(a), a, 0.0)
    return np.clip(np.round((x + 1.0) * 127.5), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
def scenes_block(cache: Path) -> list[dict]:
    """Every Sentinel-2 scene with cloud measured over the AOI, not the tile."""
    if cache.exists():
        logger.info("scenes from cache %s", cache.name)
        return json.loads(cache.read_text())
    from scar_map import search, aoi_cloud
    out = []
    for f in search("2026-05-01", "2026-09-30"):
        c = aoi_cloud(f)
        out.append({"id": c["id"], "date": c["date"],
                    "tile_cloud": round(float(c["scene_cloud"]), 1),
                    "aoi_cloud": round(float(c["aoi_cloud_pct"]), 1),
                    "aoi_clear": round(float(c["aoi_clear_pct"]), 1)})
        logger.info("  %s tile %5.1f%%  AOI %5.1f%%", c["date"],
                    c["scene_cloud"], c["aoi_cloud_pct"])
    cache.write_text(json.dumps(out))
    return out


def grid_block(cache: Path) -> tuple[str, dict]:
    """The change grid as one RGB PNG, plus the numbers needed to read it back."""
    from PIL import Image
    if cache.exists():
        logger.info("change grid from cache %s", cache.name)
        z = np.load(cache)
        d, pre, both = z["d"], z["pre"], z["both"]
    else:
        from scar_map import by_id, ndsi, composite
        F = by_id([PRE_SCENE] + POST_SCENES)
        pre, pre_ok = ndsi(F[PRE_SCENE])
        post, post_ok = composite([F[i] for i in POST_SCENES])
        both = pre_ok & post_ok & np.isfinite(pre) & np.isfinite(post)
        d = np.where(both, post - pre, np.nan)
        np.savez_compressed(cache, d=d, pre=pre, both=both)

    rgb = np.dstack([q(d), q(pre), np.where(both, 255, 0).astype(np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    png = base64.b64encode(buf.getvalue()).decode()
    logger.info("change grid PNG %.0f KB", len(png) / 1024)
    return png, {
        "w": GRID[1], "h": GRID[0], "scale": 127.5, "offset": -1.0,
        "usable_px": int(both.sum()),
        "usable_pct": round(100 * float(both.mean()), 1),
        "aoi": [85.4645, 28.2453, 85.5562, 28.3529],
    }


def offsets_block(sample_n: int = 2400, seed: int = 20260910) -> list[dict]:
    """
    Real range-offset pixels, so the browser can form a MAD and a floor itself.

    A sample rather than the whole raster: the floor is a robust scatter
    statistic and a few thousand pixels reproduce it closely, which is what the
    reference block below is there to demonstrate rather than assert.
    """
    import rasterio
    rng = np.random.default_rng(seed)
    rows = []
    stats = {r["file"]: r for r in csv.DictReader(
        open(ROOT / "outputs" / "goff_stats_source.csv"))}
    for path in sorted((ROOT / "outputs" / "export_goff_src").glob("*layer2*.tif")):
        name = path.name
        if "_UR_" in name:
            continue
        ref, sec = name[5:13], name[14:22]
        if ref not in ASC_DATES:
            continue
        span = (datetime.strptime(sec, "%Y%m%d") - datetime.strptime(ref, "%Y%m%d")).days
        with rasterio.open(path) as s:
            a = s.read(1).astype(float)
            if s.nodata is not None:
                a[a == s.nodata] = np.nan
        # The AOI exports are written in MILLIMETRES, not metres - goff_reader
        # converts on the way out. Scaling again here put the floor at 21,209
        # mm/day instead of 21.2, which the reference block caught immediately.
        v = a[np.isfinite(a)]
        if v.size < 200:
            continue
        take = v if v.size <= sample_n else rng.choice(v, sample_n, replace=False)
        rows.append({
            "pair": f"{ref}_{sec}", "ref": ref, "sec": sec, "span_days": span,
            "pre_event": datetime.strptime(sec, "%Y%m%d").date() < EVENT,
            "n_total": int(v.size),
            "sample_mm": [int(round(x)) for x in take],
        })
        logger.info("  %s  %d px sampled of %d", rows[-1]["pair"],
                    len(rows[-1]["sample_mm"]), v.size)
    return rows


def series_block() -> dict:
    """Displacement series and per-pair floors, for the detector station."""
    from inverse_velocity import load_floors
    floors = load_floors(ROOT / "outputs" / "goff_stats_source.csv", "layer2")
    series = defaultdict(list)
    with open(ROOT / "outputs" / "ts_goff_source.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            series[f'{r["geometry"]}|{r["component"]}'].append(
                {"epoch": r["epoch"], "cum_mm": round(float(r["cumulative_mm"]), 3)})
    for v in series.values():
        v.sort(key=lambda r: r["epoch"])
    return {"blocks": dict(series),
            "floors": {f"{a:%Y%m%d}_{b:%Y%m%d}": round(v, 2)
                       for (a, b), v in floors.items()}}


def clusters_block(cache: Path, grid_cache: Path) -> list[dict]:
    """
    The change clusters and their terrain reading.

    The browser re-derives the clusters itself from the PNG - that is the whole
    point of station 3 - but it cannot query a DEM, so the terrain that
    separates a detachment from a deposit is resolved here and carried.
    """
    if cache.exists():
        logger.info("clusters from cache %s", cache.name)
        return json.loads(cache.read_text())
    from scar_map import enumerate_candidates, classify, grid_transform
    z = np.load(grid_cache)
    d, pre, both = z["d"], z["pre"], z["both"]
    cl = enumerate_candidates(both, pre, d, -0.25, 0.35, 40)
    rows = classify(cl, d, pre, grid_transform(), limit=12)
    for r in rows:
        for k in ("area_km2", "lat", "lon", "d_ndsi_median", "elev_m",
                  "slope_deg", "aspect_deg"):
            r[k] = round(float(r[k]), 5)
    cache.write_text(json.dumps(rows))
    logger.info("classified %d clusters, %d detachment-like", len(rows),
                sum(1 for r in rows if r["reading"] == "DETACHMENT-like"))
    return rows


def probe_block(grid_cache: Path) -> dict:
    """The reported failure point, tested. It is the one clean negative here."""
    from scar_map import probe, grid_transform
    z = np.load(grid_cache)
    r = probe(ASSUMED[0], ASSUMED[1], z["d"], z["both"], z["pre"],
              grid_transform(), radius=5)
    return {k: (round(float(v), 4) if isinstance(v, float) else v)
            for k, v in r.items()}


def scar_floor_block() -> dict:
    """
    The floor measured AT a candidate scar, not over the whole 82 km2.

    Station 5 measures the AOI-wide floor, because that is what the exported
    rasters give and the browser can genuinely recompute it. But a floor
    averaged over 82 km2 is not the floor on one hillside, and the difference
    runs about 1.7x in the direction that flatters the bound. That escalation
    is measured by local_floor.py and carried here, because the browser cannot
    open a GeoTIFF.
    """
    # At the candidate the published elevation supports, from the per-candidate
    # run - not a separate single-point file, which is how one candidate's
    # floor came to be applied to all four.
    p = ROOT / "outputs" / "local_floor_candidates.csv"
    b = ROOT / "outputs" / "bound_source.csv"
    if not p.exists() or not b.exists():
        logger.warning("no per-candidate floors; station 5 will show AOI only")
        return {}
    pick = next(r["id"] for r in csv.DictReader(open(b))
                if r["matches_published_elevation"] == "True")
    rows = [r for r in csv.DictReader(open(p))
            if r["usable"] == "True" and r["target_id"] == pick]
    cov = []
    for r in rows:
        sec = datetime.strptime(r["file"][14:22], "%Y%m%d").date()
        ref = datetime.strptime(r["file"][5:13], "%Y%m%d").date()
        if sec < EVENT and (EVENT - ref).days <= 60:
            cov.append({"pair": r["file"][5:22],
                        "aoi": round(float(r["aoi_floor_mm_day"]), 2),
                        "point": round(float(r["local_floor_mm_day"]), 2),
                        "ci_lo": round(float(r["local_floor_ci_lo"]), 2),
                        "ci_hi": round(float(r["local_floor_ci_hi"]), 2),
                        "block": round(float(r["block_median_floor_mm_day"]), 2),
                        "px": f'{r["window_px"]}/{r["window_total_px"]}'})
    cov.sort(key=lambda x: x["pair"])
    return {"candidate": pick, "covering": cov,
            "worst_point": max((c["point"] for c in cov), default=float("nan")),
            "worst_block": max((c["block"] for c in cov), default=float("nan")),
            "worst_aoi": max((c["aoi"] for c in cov), default=float("nan"))}


def bound_block() -> dict:
    """The like-for-like headline, per candidate, exactly as bound.py wrote it."""
    from bound import PRECURSOR_MM_DAY
    p = ROOT / "outputs" / "bound_source.csv"
    rows = list(csv.DictReader(open(p))) if p.exists() else []
    return {"precursor_mm_day": round(PRECURSOR_MM_DAY, 4), "candidates": rows}


def reference_block(offsets: list[dict], series: dict) -> dict:
    """
    What the browser must reproduce.

    Computed here with the project's own functions, shipped alongside, and
    checked in-page. If the JS drifts from the Python the site says so instead
    of quietly showing a different number to every visitor.
    """
    from local_floor import detection_floor, robust_sigma
    from geometry_merge import sensitivities

    floors = {}
    for r in offsets:
        v = np.array(r["sample_mm"], dtype=float)
        floors[r["pair"]] = {
            "sigma_mm": round(robust_sigma(v), 3),
            "floor_mm_day": round(detection_floor(v, r["span_days"]), 3),
        }

    geom = {}
    for asp in (273.0, 342.0, 0.0):
        geom[f"{asp:.0f}"] = {
            t["track"]: round(t["sensitivity"], 4)
            for t in sensitivities(28.27802, 85.52963, 39.0, asp, 0.3)}

    return {"floors": floors, "sensitivity": geom,
            "note": "computed by local_floor.detection_floor, robust_sigma and "
                    "geometry_merge.sensitivities on the data shipped here"}


def tracks_block(lat: float = 28.28) -> list[dict]:
    """
    The five tracks, with heading resolved at this latitude.

    left_looking travels with every track and has no default, because getting
    it wrong inverted the verdict on three of these five once already.
    """
    from geometry_merge import TRACKS, heading_deg
    return [{"name": t.name, "mission": t.mission, "ascending": bool(t.ascending),
             "heading": round(heading_deg(t.inclination_deg, lat, t.ascending), 2),
             "incidence": t.incidence_deg, "left_looking": bool(t.left_looking),
             "repeat_days": t.repeat_days}
            for t in TRACKS]


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Build the interactive site's data")
    ap.add_argument("--out", default="docs/site/site_data.json")
    ap.add_argument("--no-network", action="store_true",
                    help="use caches only; fail rather than fetch")
    args = ap.parse_args()

    here = ROOT / "docs" / "site"
    here.mkdir(parents=True, exist_ok=True)

    logger.info("scenes")
    scenes = scenes_block(here / "_scenes_cache.json")
    logger.info("change grid")
    png, grid = grid_block(here / "_grid_cache.npz")
    logger.info("offset samples")
    offsets = offsets_block()
    logger.info("series and floors")
    series = series_block()
    logger.info("clusters")
    clusters = clusters_block(here / "_clusters_cache.json", here / "_grid_cache.npz")
    logger.info("probe of the reported point")
    probe = probe_block(here / "_grid_cache.npz")
    logger.info("floor at the scar")
    scar_floor = scar_floor_block()
    logger.info("tracks")
    tracks = tracks_block()
    logger.info("reference values")
    ref = reference_block(offsets, series)

    doc = {
        "meta": {
            "event": EVENT.isoformat(),
            "assumed_point": {"lat": ASSUMED[0], "lon": ASSUMED[1]},
            "built": datetime.now().date().isoformat(),
            "grid": grid,
        },
        "scenes": scenes,
        "change_png": png,
        "offsets": offsets,
        "series": series,
        "clusters": clusters,
        "probe": probe,
        "tracks": tracks,
        "scar_floor": scar_floor,
        "bound": bound_block(),
        "reference": ref,
    }
    out = ROOT / args.out
    out.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    logger.info("wrote %s  (%.2f MB)", out, out.stat().st_size / 1e6)

    print(f"\n  scenes    {len(scenes)}")
    print(f"  grid      {grid['w']}x{grid['h']}, {grid['usable_pct']}% usable in both epochs")
    print(f"  offsets   {len(offsets)} ascending pre/post pairs")
    print(f"  series    {len(series['blocks'])} blocks, {len(series['floors'])} floors")
    print(f"  clusters  {len(clusters)}  ({sum(1 for c in clusters if c['reading']=='DETACHMENT-like')} detachment-like)")
    print(f"  probe     assumed point: {probe.get('scar_like_pct',0):.1f}% scar-like on {probe.get('n_usable',0)} px")
    print(f"  tracks    {len(tracks)}")
    if scar_floor: print(f"  at-scar   worst covering interval {scar_floor['worst_point']} mm/day "
                        f"(AOI {scar_floor['worst_aoi']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
