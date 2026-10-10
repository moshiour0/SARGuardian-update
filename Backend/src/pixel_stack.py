"""
pixel_stack.py
--------------
Per-pixel displacement from the exported AOI rasters.

Why not just the AOI median
===========================
Every number in this project so far is a median over the whole AOI. That is
the right statistic for asking whether a valley is moving, and the wrong one
for finding a landslide inside it: a 200 m scar creeping at 3 mm/day inside a
2 km box is a few hundred pixels among twelve thousand, and the median never
notices. Only a per-pixel fit can see it.

Removing the atmosphere without a weather model
===============================================
Two separate AOIs in the same scene were measured independently and their
winter displacements correlate at +0.974 - 82% of the variance is shared.
Different ground kilometres apart cannot move identically, so that shared part
is the troposphere, and the residual after removing it is 5.3 mm against
12.6 mm before: a 2.4-fold improvement from arithmetic alone.

The same logic applies spatially. Subtracting the scene median epoch by epoch
removes whatever is common to the whole AOI and keeps whatever is local. It
cannot remove a turbulent screen with structure at landslide scale, and it
cannot see motion that fills the AOI uniformly - both limits are stated in the
output rather than hidden.

What "significant" means here
=============================
A per-pixel velocity is compared with the scatter of its own fit, not with a
global threshold, and then against the distribution of every other pixel. A
handful of significant pixels scattered at random is noise finding its tail;
a significant CLUSTER is a candidate. The tool reports both counts and refuses
to call either one a detection on its own.

Usage
-----
    python src/pixel_stack.py --dir outputs/export --season winter
    python src/pixel_stack.py --dir outputs/export --season winter --geotiff out.tif
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("stack")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import resolve  # noqa: E402

WINTER_MONTHS = (11, 12, 1, 2, 3)


def parse_export(path: Path):
    """
    Dates and processing type from the name; orbit geometry from the tags.

    The exported filename carries no direction, so nothing in the name
    distinguishes ascending path 98 from descending path 48. Chaining on dates
    alone would happily build a series that alternates between two geometries
    measuring different projections of the same motion. The exporter writes the
    source granule into the GeoTIFF tags, and that does carry it.
    """
    m = re.match(r"GUNW_(\d{8})_(\d{8})(?:_([A-Z]{2}))?\.tif$", path.name)
    if not m:
        return None
    geom = "?"
    try:
        import rasterio
        with rasterio.open(path) as src:
            src_name = src.tags().get("source", "")
        g = re.search(r"_GUNW_\d+_(\d+)_([AD])_", src_name)
        if g:
            geom = f"{g.group(2)}{int(g.group(1)):03d}"
    except Exception:
        pass
    return {"path": path, "ref": datetime.strptime(m.group(1), "%Y%m%d").date(),
            "sec": datetime.strptime(m.group(2), "%Y%m%d").date(),
            "proc": m.group(3) or "PR", "geom": geom}


def align(rasters: list[dict]) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Put every raster on one grid using its geotransform, not its shape.

    The exporter crops to each pair's own valid bounding box, so two rasters of
    the same AOI can differ by a column. Stacking them by array index silently
    shears the scene by one pixel per epoch - a displacement gradient that is
    pure bookkeeping. Aligning on coordinates makes that impossible.
    """
    import rasterio

    metas = []
    for r in rasters:
        with rasterio.open(r["path"]) as src:
            metas.append({"t": src.transform, "h": src.height, "w": src.width,
                          "crs": src.crs, "disp": src.read(1),
                          "coh": src.read(2) if src.count > 1 else None})

    res = {(round(m["t"].a, 9), round(m["t"].e, 9)) for m in metas}
    if len(res) != 1:
        raise SystemExit(f"Rasters have different resolutions: {res}")
    if len({str(m["crs"]) for m in metas}) != 1:
        raise SystemExit("Rasters are in different CRSs")

    px, py = metas[0]["t"].a, metas[0]["t"].e
    x0 = min(m["t"].c for m in metas)
    y0 = max(m["t"].f for m in metas)
    x1 = max(m["t"].c + m["w"] * px for m in metas)
    y1 = min(m["t"].f + m["h"] * py for m in metas)
    W = int(round((x1 - x0) / px))
    H = int(round((y1 - y0) / py))

    cube = np.full((len(metas), H, W), np.nan, dtype="float32")
    cohs = np.full((len(metas), H, W), np.nan, dtype="float32")
    for k, m in enumerate(metas):
        j = int(round((m["t"].c - x0) / px))
        i = int(round((m["t"].f - y0) / py))
        cube[k, i:i + m["h"], j:j + m["w"]] = m["disp"]
        if m["coh"] is not None:
            cohs[k, i:i + m["h"], j:j + m["w"]] = m["coh"]

    import rasterio.transform as rt
    return cube, cohs, {"transform": rt.Affine(px, 0, x0, 0, py, y0),
                        "crs": metas[0]["crs"], "shape": (H, W)}


def chain(rasters: list[dict]) -> list[list[dict]]:
    """
    Contiguous runs within ONE geometry.

    Ascending and descending measure different components of the same 3-D
    motion and can never be chained, however neatly their dates happen to line
    up. Grouping first makes that impossible rather than merely unlikely.
    """
    runs = []
    for geom in sorted({r["geom"] for r in rasters}):
        group = sorted((r for r in rasters if r["geom"] == geom),
                       key=lambda r: r["ref"])
        cur = [group[0]]
        for a, b in zip(group, group[1:]):
            if a["sec"] == b["ref"]:
                cur.append(b)
            else:
                runs.append(cur); cur = [b]
        runs.append(cur)
    return runs


def stack(run: list[dict], remove_common_mode: bool = True):
    cube, cohs, geo = align(run)
    n_ep = len(run) + 1
    H, W = geo["shape"]

    if remove_common_mode:
        # One number per epoch: the scene median. Whatever is common to the
        # whole AOI is atmosphere or an unresolved reference offset; whatever
        # survives is local, which is the only thing a landslide can be.
        for k in range(cube.shape[0]):
            med = np.nanmedian(cube[k])
            if np.isfinite(med):
                cube[k] -= med

    cum = np.full((n_ep, H, W), np.nan, dtype="float32")
    cum[0] = 0.0
    for k in range(cube.shape[0]):
        cum[k + 1] = cum[k] + cube[k]

    days = np.array([0.0] + list(np.cumsum([(r["sec"] - r["ref"]).days for r in run])))
    ok = np.isfinite(cum).all(axis=0)

    vel = np.full((H, W), np.nan, dtype="float32")
    sig = np.full((H, W), np.nan, dtype="float32")
    if ok.any():
        y = cum[:, ok]                                   # (n_ep, n_px)
        A = np.vstack([days, np.ones_like(days)]).T
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = A @ coef - y
        dof = max(n_ep - 2, 1)
        s = np.sqrt((resid ** 2).sum(axis=0) / dof)
        se = s * np.sqrt(np.linalg.inv(A.T @ A)[0, 0])
        vel[ok] = coef[0]
        sig[ok] = se
    return {"velocity": vel, "sigma": sig, "cumulative": cum, "coherence": cohs,
            "valid": ok, "days": days, "geo": geo, "run": run}


def report(res: dict, label: str) -> None:
    v, s, ok = res["velocity"], res["sigma"], res["valid"]
    run = res["run"]
    print(f"\n{'='*72}\n{label}   {run[0]['ref']} .. {run[-1]['sec']}  "
          f"({len(run)} pairs, {int(res['days'][-1])} d)\n{'='*72}")
    n = int(ok.sum())
    print(f"  pixels with a complete series : {n:,} of {ok.size:,} "
          f"({100*n/ok.size:.0f}%)")
    if n == 0:
        print("  nothing to fit"); return

    vv, ss = v[ok], s[ok]
    rob = 1.4826 * np.median(np.abs(vv - np.median(vv)))
    print(f"  velocity, robust scatter      : {rob:.3f} mm/day "
          f"({rob*365:.0f} mm/yr)")
    print(f"  median per-pixel fit sigma    : {np.median(ss):.3f} mm/day")

    # Significant against its own fit, and against the population.
    own = np.zeros_like(v, bool); own[ok] = np.abs(vv) > 3 * ss
    pop = np.zeros_like(v, bool); pop[ok] = np.abs(vv - np.median(vv)) > 3 * rob
    both = own & pop
    print(f"\n  3-sigma against its own fit   : {int(own.sum()):,} px "
          f"({100*own.sum()/max(n,1):.2f}%)")
    print(f"  3-sigma against the population: {int(pop.sum()):,} px "
          f"({100*pop.sum()/max(n,1):.2f}%)")
    print(f"  both                          : {int(both.sum()):,} px")
    print(f"  expected by chance at 3-sigma : {0.0027*n:.0f} px")

    lab, count = _largest_cluster(both)
    print(f"\n  largest connected cluster     : {count} px", end="")
    if count:
        ii, jj = np.nonzero(lab)
        print(f"  (rows {ii.min()}-{ii.max()}, cols {jj.min()}-{jj.max()})")
        print(f"  cluster mean velocity         : "
              f"{float(np.nanmean(v[lab])):+.3f} mm/day")
        pos = float(np.nanmean(v[lab] > 0))
        print(f"  cluster sign consistency      : {100*max(pos, 1-pos):.0f}% "
              f"one direction")
    else:
        print()

    # --- the null the earlier version of this tool did not have --------------
    # Comparing against a 0.27% chance rate assumes pixels are independent.
    # Tropospheric noise is correlated over kilometres, so it CLUSTERS on its
    # own, and cluster size proves nothing until compared with what this
    # scene's own correlation length produces from pure noise.
    corr_km = _correlation_length_km(v, ok, res["geo"])
    null = _null_cluster_sizes(v.shape, corr_km, res["geo"], trials=20)
    p95 = int(np.percentile(null, 95))
    print(f"\n  measured correlation length   : {corr_km:.1f} km")
    print(f"  clusters from noise ALONE     : median {int(np.median(null))} px, "
          f"95th pct {p95} px, max {int(max(null))} px")

    dof = len(res["days"]) - 2
    if dof <= 2:
        print(f"\n  NOTE: {len(res['days'])} epochs leaves dof={dof}. Each pixel's own")
        print("  sigma is itself uncertain by roughly half, so the per-fit")
        print("  significance count above is inflated and means little.")

    # A cluster faster than lambda/4 per pair is not a fast measurement, it is
    # an unmeasurable one. Above that the phase has wrapped and the unwrapper
    # has guessed; the value is as likely to be its mistake as the ground's.
    from gunw_reader import NISAR_LAMBDA_M
    ceiling = NISAR_LAMBDA_M / 4 * 1000 / 12.0
    if count and abs(float(np.nanmean(v[lab]))) > ceiling:
        print(f"\n  WARNING: cluster mean exceeds lambda/4 per 12-day pair "
              f"({ceiling:.2f} mm/day).")
        print("  Phase cannot represent motion this fast. These pixels are at least")
        print("  as likely to be an unwrapping error as a measurement, and cannot")
        print("  support a detection on their own.")

    if count <= p95:
        print("\n  VERDICT: NO DETECTION. The largest cluster is within what this")
        print("  scene's own correlation length produces from noise with no")
        print("  signal present. Cluster size is not evidence here.")
        return False
    else:
        print("\n  VERDICT: cluster exceeds the noise-only 95th percentile. That is")
        print("  necessary, not sufficient. Before calling it anything: check it")
        print("  appears in the other geometry, in a different time window, and")
        print("  moves consistently in one direction.")
        return True


def _correlation_length_km(v: np.ndarray, ok: np.ndarray, geo: dict) -> float:
    """
    Distance at which the velocity field decorrelates to 1/e.

    This is the number that decides whether a cluster is remarkable. Assuming
    independent pixels puts the chance rate at 0.27% and makes any blob look
    extraordinary; the truth is that a troposphere correlated over kilometres
    manufactures blobs for free.
    """
    a = np.where(ok, v, np.nan)
    a = a - np.nanmean(a)
    px_km = abs(geo["transform"].a) / 1000.0
    ref = np.nanmean(a * a)
    if not np.isfinite(ref) or ref <= 0:
        return px_km

    # Both directions, and keep the LARGER. Residual atmosphere over a valley
    # is anisotropic - it streaks along the wind - and measuring across the
    # streaks only makes noise look more local than it is, which is precisely
    # the direction that turns an ordinary blob into a false candidate.
    def decay(axis: int) -> float:
        n = a.shape[axis]
        for lag in range(1, min(60, n // 2)):
            if axis == 1:
                c = np.nanmean(a[:, :-lag] * a[:, lag:]) / ref
            else:
                c = np.nanmean(a[:-lag, :] * a[lag:, :]) / ref
            if not np.isfinite(c) or c < np.e ** -1:
                return lag * px_km
        return 60 * px_km

    return max(decay(0), decay(1))


def _null_cluster_sizes(shape, corr_km: float, geo: dict, trials: int = 20):
    """Largest 3-sigma cluster from pure noise at this scene's own scale."""
    from numpy.fft import irfft2, rfft2
    px_km = abs(geo["transform"].a) / 1000.0
    k = max(3, int(round(corr_km / max(px_km, 1e-6))))
    k = min(k, min(shape) // 2 or 3)
    ker = np.ones((k, k)) / (k * k)
    rng = np.random.default_rng(0)
    out = []
    for _ in range(trials):
        f = rng.normal(0.0, 1.0, shape)
        sm = irfft2(rfft2(f) * rfft2(ker, shape), shape)
        s = sm.std()
        if s <= 0:
            out.append(0); continue
        _, n = _largest_cluster(np.abs(sm / s) > 3.0)
        out.append(n)
    return out


def _largest_cluster(mask: np.ndarray):
    """Largest 4-connected blob, by flood fill. No scipy for one call."""
    seen = np.zeros_like(mask, bool)
    best = np.zeros_like(mask, bool); best_n = 0
    H, W = mask.shape
    for si in range(H):
        for sj in range(W):
            if not mask[si, sj] or seen[si, sj]:
                continue
            stack_, cells = [(si, sj)], []
            seen[si, sj] = True
            while stack_:
                i, j = stack_.pop(); cells.append((i, j))
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    a, b = i + di, j + dj
                    if 0 <= a < H and 0 <= b < W and mask[a, b] and not seen[a, b]:
                        seen[a, b] = True; stack_.append((a, b))
            if len(cells) > best_n:
                best_n = len(cells)
                best = np.zeros_like(mask, bool)
                for i, j in cells:
                    best[i, j] = True
    return best, best_n


def cluster_footprint(res: dict):
    """Largest significant cluster, in longitude and latitude."""
    v, s, ok = res["velocity"], res["sigma"], res["valid"]
    if not ok.any():
        return None
    vv, ss = v[ok], s[ok]
    rob = 1.4826 * np.median(np.abs(vv - np.median(vv)))
    own = np.zeros_like(v, bool); own[ok] = np.abs(vv) > 3 * ss
    pop = np.zeros_like(v, bool); pop[ok] = np.abs(vv - np.median(vv)) > 3 * rob
    lab, n = _largest_cluster(own & pop)
    if not n:
        return None
    ii, jj = np.nonzero(lab)
    T = res["geo"]["transform"]
    xs = T.c + (jj + 0.5) * T.a
    ys = T.f + (ii + 0.5) * T.e
    try:
        from pyproj import Transformer
        lon, lat = Transformer.from_crs(res["geo"]["crs"], 4326,
                                        always_xy=True).transform(xs, ys)
    except ImportError:
        logger.warning("pyproj missing - cannot compare geometries on the ground.")
        return None
    return {"n": n, "lon": (float(lon.min()), float(lon.max())),
            "lat": (float(lat.min()), float(lat.max())),
            "velocity": float(np.nanmean(v[lab]))}


def cross_geometry_check(found: dict) -> None:
    """
    Do the geometries see it in the same PLACE?

    This is the test that decides. Ascending and descending fly at different
    times through different weather, so a residual atmospheric blob has no
    reason to land twice on the same ground - while a moving slope has no
    choice. Comparing indices would be meaningless because each geometry has
    its own grid; the comparison has to happen in longitude and latitude.
    """
    if len(found) < 2:
        print("\n  Only one geometry available - the strongest check on a candidate")
        print("  cannot be run. Treat any cluster as unconfirmed.")
        return

    print(f"\n{'=' * 72}")
    print("CROSS-GEOMETRY CHECK")
    print("=" * 72)
    for g, c in sorted(found.items()):
        print(f"  {g}  {c['n']:>4} px  {c['velocity']:+7.2f} mm/day   "
              f"lon {c['lon'][0]:.4f}..{c['lon'][1]:.4f}  "
              f"lat {c['lat'][0]:.4f}..{c['lat'][1]:.4f}")

    geoms = sorted(found)
    for a, b in [(geoms[i], geoms[j])
                 for i in range(len(geoms)) for j in range(i + 1, len(geoms))]:
        ca, cb = found[a], found[b]
        ov_lon = min(ca["lon"][1], cb["lon"][1]) - max(ca["lon"][0], cb["lon"][0])
        ov_lat = min(ca["lat"][1], cb["lat"][1]) - max(ca["lat"][0], cb["lat"][0])
        print(f"\n  {a} vs {b}: overlap {ov_lon:+.4f} deg lon, "
              f"{ov_lat:+.4f} deg lat")
        if ov_lon > 0 and ov_lat > 0:
            print("  SAME GROUND. Two geometries, two weather systems, one location -")
            print("  that is a real candidate. Check the sign against the slope")
            print("  aspect before interpreting direction.")
        else:
            km = abs(min(ov_lon, ov_lat)) * 111.0
            print(f"  DISJOINT - about {km:.1f} km apart, sharing no ground.")
            print("  Independent geometries throwing up unrelated blobs is exactly")
            print("  what residual atmosphere does. NOT a detection.")


def write_geotiff(res: dict, out: Path) -> None:
    import rasterio
    v, s = res["velocity"], res["sigma"]
    with rasterio.open(out, "w", driver="GTiff", height=v.shape[0], width=v.shape[1],
                       count=2, dtype="float32", crs=res["geo"]["crs"],
                       transform=res["geo"]["transform"], nodata=np.nan,
                       compress="deflate", predictor=3) as dst:
        dst.write(v, 1); dst.set_band_description(1, "LOS velocity (mm/day)")
        dst.write(s, 2); dst.set_band_description(2, "1-sigma (mm/day)")
    logger.info("Wrote %s", out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-pixel velocity from exported AOI rasters")
    ap.add_argument("--dir", default="outputs/export")
    ap.add_argument("--season", choices=("winter", "summer", "all"), default="winter")
    ap.add_argument("--keep-common-mode", action="store_true",
                    help="do NOT remove the per-epoch scene median")
    ap.add_argument("--geotiff", metavar="OUT.tif")
    args = ap.parse_args()

    d = resolve(args.dir)
    rasters = [r for r in (parse_export(p) for p in sorted(d.glob("*.tif"))) if r]
    rasters = [r for r in rasters if r["proc"] == "PR"]
    if args.season != "all":
        want = args.season == "winter"
        rasters = [r for r in rasters if (r["ref"].month in WINTER_MONTHS) == want]
    if not rasters:
        logger.error("No %s rasters in %s", args.season, d)
        return 1

    logger.info("%d %s pair(s)", len(rasters), args.season)
    if args.keep_common_mode:
        logger.warning("Common mode kept - atmosphere will dominate every pixel.")

    runs = [r for r in chain(rasters) if len(r) >= 2]
    if not runs:
        logger.error("No contiguous chain of 2+ pairs; nothing to stack.")
        return 1

    found = {}
    for k, run in enumerate(runs, 1):
        res = stack(run, remove_common_mode=not args.keep_common_mode)
        survived = report(res, f"{run[0]['geom']}  chain {k}")
        fp = cluster_footprint(res)
        # Only a cluster that beat its OWN noise null is worth cross-checking.
        # Confirming one blob against another that its own test already called
        # noise is not corroboration - it is two random blobs, and with enough
        # of them some pair will always overlap.
        if fp and survived:
            found.setdefault(run[0]["geom"], fp)
        elif fp:
            logger.info("%s cluster did not beat its own noise null - excluded "
                        "from the cross-geometry check.", run[0]["geom"])
        if args.geotiff and len(runs) == 1:
            write_geotiff(res, Path(args.geotiff))

    cross_geometry_check(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
