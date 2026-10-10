"""
troposphere.py
--------------
Measure and remove the elevation-dependent (stratified) tropospheric delay from
exported LOS displacement rasters.

Why this exists
===============
The pipeline removes an ionosphere screen, references to stable ground, and
subtracts a scene median epoch by epoch. None of that touches the term that
matters most in high relief: tropospheric water vapour is stratified with
height, so it perturbs interferometric phase in proportion to elevation. A
median removes the mean, not the gradient.

Measured over the source zone, on the fixed 116x155 export lattice, with a
3-MAD clipped fit (see fit_elevation_trend for why it is not plain least
squares):

    GUNW phase     15 pairs  |r| median 0.46, 21.1% of variance, 13/15 p<0.001
    GOFF offsets   18 pairs  |r| median 0.12,  1.5% of variance, 15/18 p<0.001

A fourteenfold difference in explained variance, which is what the physics
predicts. Phase measures optical path length and is perturbed directly; offset
tracking measures a geometric pixel shift and is affected only at second order.
So the bounded non-detection, which rests on offsets, is largely immune - and
any phase result is not.

Read the variance column, not the slope column. The GOFF slopes are the LARGER
of the two in absolute terms - median 19.1 mm/km against 8.8 for phase, max 90.4
against 21.7 - and that is not a contradiction. Offset fields are one to two
orders of magnitude noisier (MAD 34-470 mm against 8-49 mm for phase), so a
steeper line accounts for a smaller share of what is there. A slope quoted
without the scatter it sits in says nothing.

The decisive detail is not the magnitude either. It is that the fitted slope
ALTERNATES SIGN between consecutive 12-day pairs - 6 reversals in 15 phase
pairs, 10 in 18 offset pairs. Ground does not reverse direction every twelve
days. A water-vapour field does.

What "removing" it means, and what it does not
==============================================
This fits displacement against elevation over the valid pixels of one raster
and subtracts the fitted line. That removes the part of the delay that is
linear in height, which is the stratified component. It does NOT remove:

  - turbulent water vapour, which has no elevation signature,
  - any real deformation that happens to correlate with height, and there is no
    way to tell the two apart from one interferogram.

The second point is the honest limit of the method. A slope that creeps faster
at altitude looks exactly like a stratified delay to this fit. Where that is a
live possibility, report the slope as an error bar instead of removing it -
`--report-only` does that.

There is a third limit, and over this AOI it binds. The pixels that survive
coherence and correlation masking are not spread evenly over the relief: they
sit in a band roughly 1.1 km wide inside a 4.0 km range, so the line is
extrapolated across about 3.4 times the interquartile range on a typical pair.
A fit that reaches that far past its own support is set by a minority of pixels
and applied to all of them. The tool computes range/IQR per pair, flags it in
the table with `!`, and says so in the summary. Ten of fifteen phase pairs and
ten of eighteen offset pairs are flagged here, which is why the source-zone
numbers above are quoted as an uncertainty and NOT removed from the published
time series.

Elevation source
================
SRTM 30 m via OpenTopoData, fetched once per AOI grid and cached to
outputs/dem_cache/. The public endpoint allows one request per second, so a
full 116x155 grid would be 180 requests. Fetching every pixel is unnecessary
for a trend that is smooth by construction, so a decimated grid is fetched and
bilinearly resampled; --step controls it.

Usage
-----
    python src/troposphere.py --dir outputs/export_src --aoi source
    python src/troposphere.py --dir outputs/export_src --aoi source --band 1 \
        --out outputs/export_src_detrended
    python src/troposphere.py --dir outputs/export_goff_src --aoi source --report-only
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("troposphere")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import resolve  # noqa: E402

MIN_SAMPLES = 30          # below this a fit is not worth reporting
LEVERAGE_WARN = 3.0       # elevation range / IQR above which the fit is
                          # extrapolated rather than interpolated
CACHE = Path("outputs/dem_cache")


# ---------------------------------------------------------------------------
def fit_elevation_trend(elev_m: np.ndarray, disp_mm: np.ndarray,
                        robust: bool = True, clip_sigma: float = 3.0,
                        passes: int = 2) -> dict:
    """
    Displacement against elevation, in mm per km.

    Why the fit is robust by default
    --------------------------------
    Plain least squares gave the wrong answer here, and the way it failed is
    worth recording. On a winter ascending pair the displacement field has a
    tight core and heavy tails - MAD scatter 13.8 mm against a standard
    deviation of 39.4, a ratio of 2.9. Least squares minimises the squared
    residual, so the tails set the slope. Removing that slope reduced the
    standard deviation from 39.4 to 34.3, exactly as least squares promises,
    and simultaneously pushed the MAD scatter of the core UP from 13.8 to 22.2.

    The fit was optimising a statistic nobody in this project uses. Every other
    noise figure here is MAD-based, because unculled SAR products are full of
    gross outliers. So this clips residuals at `clip_sigma` MADs and refits,
    twice, which is the same discipline applied to the trend.

    Set robust=False to recover the plain least-squares behaviour - useful only
    for demonstrating the above.

    Pure: no I/O, no raster, so the decision this drives can be tested.
    """
    m = np.isfinite(elev_m) & np.isfinite(disp_mm)
    n = int(m.sum())
    out = {"n": n, "n_used": 0, "slope_mm_per_km": float("nan"),
           "intercept_mm": float("nan"), "r": float("nan"),
           "variance_explained": float("nan"), "p_value": float("nan"),
           "elev_range_m": float("nan"), "elev_iqr_m": float("nan"),
           "leverage": float("nan"), "usable": False}
    if n < MIN_SAMPLES:
        return out

    e_all = elev_m[m] / 1000.0      # km, so the slope reads in mm/km
    d_all = disp_mm[m]
    if np.ptp(e_all) <= 0:
        return out                  # no relief, nothing to regress against

    keep = np.ones(e_all.shape, dtype=bool)
    slope = intercept = 0.0
    for _ in range(max(1, passes) if robust else 1):
        e, d = e_all[keep], d_all[keep]
        if e.size < MIN_SAMPLES or np.ptp(e) <= 0:
            keep = np.ones(e_all.shape, dtype=bool)
            e, d = e_all, d_all
            break
        slope, intercept = np.polyfit(e, d, 1)
        if not robust:
            break
        res = d_all - (slope * e_all + intercept)
        s = 1.4826 * np.median(np.abs(res - np.median(res)))
        if not np.isfinite(s) or s <= 0:
            break
        keep = np.abs(res - np.median(res)) <= clip_sigma * s

    e, d = e_all[keep], d_all[keep]
    if e.size < MIN_SAMPLES:
        e, d, keep = e_all, d_all, np.ones(e_all.shape, dtype=bool)
    slope, intercept = np.polyfit(e, d, 1)
    r = float(np.corrcoef(e, d)[0, 1]) if np.ptp(e) > 0 else float("nan")

    # How far the line is asked to reach beyond the elevations that constrain
    # it. Most valid pixels sit in a narrow band; a handful of high ones set the
    # slope and the correction is then extrapolated across the rest. Range over
    # interquartile range says by how much - see LEVERAGE_WARN below.
    q25, q75 = np.percentile(e * 1000.0, [25, 75])
    rng = float(np.ptp(e) * 1000.0)
    iqr = float(q75 - q25)
    out.update(n_used=int(keep.sum()), slope_mm_per_km=float(slope),
               intercept_mm=float(intercept), r=r,
               variance_explained=r * r if np.isfinite(r) else float("nan"),
               elev_range_m=rng, elev_iqr_m=iqr,
               leverage=(rng / iqr) if iqr > 0 else float("inf"),
               usable=True)

    nn = int(keep.sum())
    if np.isfinite(r) and abs(r) < 1.0 and nn > 2:
        t = abs(r) * math.sqrt((nn - 2) / (1.0 - r * r))
        # Normal tail on a t statistic, which is fine at these sample sizes -
        # and beside the point. The samples are neighbouring 80 m pixels in one
        # interferogram, so they are not independent and the effective n is
        # tens, not thousands. Read this as "a trend exists at all", never as a
        # strength of evidence; the caller prints that caveat.
        out["p_value"] = math.erfc(t / math.sqrt(2.0))
    else:
        # An undefined correlation is not a significant one. This used to
        # return 0.0, so a NaN r came out as maximally significant and would
        # have been starred *** in the table.
        out["p_value"] = float("nan")
    return out


def remove_trend(elev_m: np.ndarray, disp_mm: np.ndarray, fit: dict) -> np.ndarray:
    """
    Subtract the fitted elevation line.

    The intercept is deliberately NOT subtracted. It is an arbitrary constant
    for a relative measurement - the same constant the reference point already
    removes - and taking it out here would shift every value by the mean and
    make before/after scatter comparisons meaningless.
    """
    if not fit.get("usable"):
        return disp_mm
    return disp_mm - fit["slope_mm_per_km"] * (elev_m / 1000.0)


def robust_sigma(a: np.ndarray) -> float:
    """MAD-based scatter, as used everywhere else in this project."""
    v = a[np.isfinite(a)]
    if v.size == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def scatter(a: np.ndarray) -> tuple:
    """
    Both scatter statistics, because they disagree and the disagreement is
    information.

    Standard deviation is what a least-squares fit minimises, so quoting only
    it flatters the correction. MAD describes the core of the distribution and
    ignores the tails, so quoting only it can make a correct fit look harmful
    when the tails are heavy. Report the pair and let the reader see the shape:
    std/MAD near 1.48 is Gaussian, and much above that is outlier-dominated.
    """
    v = a[np.isfinite(a)]
    if v.size == 0:
        return float("nan"), float("nan")
    return float(np.std(v)), robust_sigma(v)



DATE_PAIR = re.compile(r"(\d{8})_(\d{8})")


def sign_reversals(rows: list[dict]) -> tuple[int, int, dict]:
    """
    How often the fitted slope changes sign between CONSECUTIVE PAIRS ON ONE
    TRACK, in date order.

    Why this is not simply zip(signs, signs[1:])
    -------------------------------------------
    It used to be, over rows in whatever order `sorted(glob("*.tif"))` produced.
    That order is by date across ALL tracks, so the "consecutive 12-day pairs"
    being compared ran descending, ascending, descending, ascending - two
    geometries that project the same delay field differently, read as one time
    sequence. It also counted the routine and urgent products of the same
    acquisitions as two observations.

    The claim being tested is that the slope reverses between successive passes
    of the same satellite over the same ground, which is a statement about one
    track. So: group by track, drop duplicate processings of a pair, order by
    reference date, and count transitions within each group.

    Returns (reversals, transitions, {track: sign string}). Compare reversals
    against transitions/2, which is what random signs give.
    """
    by_track: dict[str, dict[str, dict]] = {}
    for r in rows:
        # One observation per acquisition pair. PR and UR are the same two
        # images; keeping both inflates the count and, since they usually agree
        # in sign, biases it toward "no reversal".
        by_track.setdefault(r.get("track", "?"), {}).setdefault(
            r.get("reference", r["file"]), r)

    flips = transitions = 0
    seqs: dict[str, str] = {}
    for track, pairs in by_track.items():
        ordered = [pairs[k] for k in sorted(pairs)]
        seq = "".join("+" if p["slope_mm_per_km"] > 0 else "-" for p in ordered)
        seqs[track] = seq
        flips += sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        transitions += max(0, len(seq) - 1)
    return flips, transitions, seqs


# ---------------------------------------------------------------------------
def dem_for_grid(transform, width: int, height: int, epsg: int, step: int) -> np.ndarray:
    """
    Elevation on a raster's own grid, decimated by `step` and resampled back.

    Cached per (epsg, origin, size, step) so a stack of twenty pairs over one
    AOI costs one fetch, not twenty.
    """
    import rasterio
    from pyproj import Transformer
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from geometry_merge import elevation_grid

    key = (f"{epsg}_{transform.c:.0f}_{transform.f:.0f}_"
           f"{width}x{height}_s{step}.npy")
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / key
    if cached.exists():
        logger.info("DEM from cache: %s", cached.name)
        return np.load(cached)

    rows = np.arange(0, height, step)
    cols = np.arange(0, width, step)
    xs, ys = rasterio.transform.xy(transform, [0] * len(cols), cols.tolist())
    _, ys_col = rasterio.transform.xy(transform, rows.tolist(), [0] * len(rows))
    tf = Transformer.from_crs(epsg, 4326, always_xy=True)
    lons = [tf.transform(x, ys[0])[0] for x in xs]
    lats = [tf.transform(xs[0], y)[1] for y in ys_col]

    logger.info("Fetching %d x %d elevation samples (step %d)", len(lats), len(lons), step)
    coarse = elevation_grid(lats, lons)

    # Bilinear back onto the full grid. The trend is smooth by construction, so
    # this costs far less than it saves in requests.
    ri = np.linspace(0, coarse.shape[0] - 1, height)
    ci = np.linspace(0, coarse.shape[1] - 1, width)
    r0 = np.clip(np.floor(ri).astype(int), 0, coarse.shape[0] - 1)
    r1 = np.clip(r0 + 1, 0, coarse.shape[0] - 1)
    c0 = np.clip(np.floor(ci).astype(int), 0, coarse.shape[1] - 1)
    c1 = np.clip(c0 + 1, 0, coarse.shape[1] - 1)
    wr = (ri - r0)[:, None]
    wc = (ci - c0)[None, :]
    top = coarse[np.ix_(r0, c0)] * (1 - wc) + coarse[np.ix_(r0, c1)] * wc
    bot = coarse[np.ix_(r1, c0)] * (1 - wc) + coarse[np.ix_(r1, c1)] * wc
    full = top * (1 - wr) + bot * wr

    np.save(cached, full)
    logger.info("Cached %s", cached.name)
    return full


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure and remove the stratified tropospheric delay")
    ap.add_argument("--dir", required=True, help="directory of exported GeoTIFFs")
    ap.add_argument("--aoi", default="source", help="only used to label output")
    ap.add_argument("--band", type=int, default=1,
                    help="band holding displacement (1 = LOS mm for GUNW, "
                         "1 = slant-range mm for GOFF)")
    ap.add_argument("--step", type=int, default=4,
                    help="decimation for the DEM fetch; 4 keeps a 116x155 grid "
                         "to about 12 requests")
    ap.add_argument("--report-only", action="store_true",
                    help="measure and report, write nothing. Use where real "
                         "deformation could plausibly correlate with height")
    ap.add_argument("--out", metavar="DIR", help="write detrended rasters here")
    ap.add_argument("--match", metavar="SUBSTR",
                    help="only files whose name contains this, e.g. layer2. "
                         "GOFF exports carry three layers per pair and mixing "
                         "them in one summary is meaningless")
    ap.add_argument("--csv", metavar="OUT.csv")
    args = ap.parse_args()

    try:
        import rasterio
    except ImportError:
        logger.error("rasterio required:  pip install rasterio")
        return 1

    d = resolve(args.dir)
    files = sorted(p for p in Path(d).glob("*.tif"))
    if args.match:
        files = [p for p in files if args.match in p.name]
    if not files:
        logger.error("No .tif under %s%s", d,
                     f" matching {args.match!r}" if args.match else "")
        return 1

    outdir = Path(resolve(args.out)) if args.out else None
    if outdir and not args.report_only:
        outdir.mkdir(parents=True, exist_ok=True)

    rows, dem, grid_key = [], None, None
    print(f"\n{'='*86}")
    print(f"STRATIFIED TROPOSPHERE over {args.aoi}: displacement against elevation")
    print("=" * 86)
    print("A real deformation field has no reason to be linear in elevation.")
    print("A stratified delay is linear in elevation by construction, and its")
    print("sign changes between passes as the atmosphere changes.\n")
    print(f"  {'PAIR':<26}{'n':>6}{'mm/km':>9}{'r':>7}{'var%':>7}{'p':>10}"
          f"{'  std b>a':>16}{'  MAD b>a':>16}{'lev':>7}")
    print("  " + "-" * 104)

    for f in files:
        with rasterio.open(f) as s:
            arr = s.read(args.band).astype(float)
            if s.nodata is not None:
                arr[arr == s.nodata] = np.nan
            key = (s.crs.to_epsg(), round(s.transform.c), round(s.transform.f),
                   s.width, s.height)
            if key != grid_key:
                dem = dem_for_grid(s.transform, s.width, s.height,
                                   s.crs.to_epsg(), args.step)
                grid_key = key
            prof = s.profile

        # Which track and which pair this raster came from. The exporter writes
        # the source granule into the tags, and the granule name is the only
        # place the orbit direction survives - the export filename carries
        # dates and processing type but not A/D.
        tags = {}
        try:
            with rasterio.open(f) as _s:
                tags = _s.tags()
        except Exception:
            pass
        gran = tags.get("source", "")
        m = re.search(r"_(\d{3})_([AD])_", gran)
        track = f"{m.group(2)}{m.group(1)}" if m else "?"
        proc = re.search(r"NISAR_L2_([A-Z]{2})_", gran)
        proc = proc.group(1) if proc else "??"
        dm = DATE_PAIR.search(f.name)
        ref = dm.group(1) if dm else f.name

        fit = fit_elevation_trend(dem, arr)
        sd_b, mad_b = scatter(arr)
        corrected = remove_trend(dem, arr, fit)
        sd_a, mad_a = scatter(corrected)
        name = f.stem[:26]

        if not fit["usable"]:
            print(f"  {name:<26}{fit['n']:>6}   too few valid samples")
            continue

        pv = fit["p_value"]
        star = "" if not np.isfinite(pv) else (
            "***" if pv < 0.001 else "**" if pv < 0.01 else "*" if pv < 0.05 else "")
        flag = "!" if fit["leverage"] > LEVERAGE_WARN else " "
        print(f"  {name:<26}{fit['n']:>6}{fit['slope_mm_per_km']:>9.1f}"
              f"{fit['r']:>7.2f}{100*fit['variance_explained']:>6.1f}%"
              f"{fit['p_value']:>10.1e}{star:<3}"
              f"{sd_b:>7.1f}>{sd_a:<7.1f}{mad_b:>7.1f}>{mad_a:<7.1f}"
              f"{fit['leverage']:>6.1f}{flag}")

        rows.append({"file": f.name, "track": track, "proc": proc,
                     "reference": ref, **fit,
                     "std_before_mm": sd_b, "std_after_mm": sd_a,
                     "mad_before_mm": mad_b, "mad_after_mm": mad_a})

        if outdir and not args.report_only:
            prof.update(count=1, dtype="float32", nodata=np.nan)
            with rasterio.open(outdir / f.name, "w", **prof) as dst:
                dst.write(corrected.astype("float32"), 1)
                dst.set_band_description(1, "LOS displacement, elevation trend removed (mm)")
                dst.update_tags(elevation_slope_mm_per_km=f"{fit['slope_mm_per_km']:.3f}",
                                elevation_r=f"{fit['r']:.4f}")

    if not rows:
        print("\n  Nothing fitted.")
        return 1

    sl = np.array([r["slope_mm_per_km"] for r in rows])
    rr = np.array([abs(r["r"]) for r in rows])
    sig = sum(1 for r in rows if np.isfinite(r["p_value"]) and r["p_value"] < 0.001)
    flips, transitions, seqs = sign_reversals(rows)

    print(f"\n  {len(rows)} pairs fitted")
    print(f"  |slope| median {np.median(np.abs(sl)):.1f} mm/km, max {np.abs(sl).max():.1f}")
    print(f"  |r| median {np.median(rr):.2f}, variance explained {100*np.median(rr**2):.1f}%")
    print(f"  significant at p<0.001: {sig} of {len(rows)}  "
          f"(see the note on effective sample size below)")
    for t, seq in sorted(seqs.items()):
        print(f"  sign sequence, track {t}: {seq}")
    expected = transitions / 2.0
    print(f"  reversals: {flips} of {transitions} within-track transitions; "
          f"{expected:.1f} expected if the signs were random")

    worse_std = sum(1 for r in rows if r["std_after_mm"] > r["std_before_mm"])
    worse_mad = sum(1 for r in rows if r["mad_after_mm"] > r["mad_before_mm"])
    lev = np.array([r["leverage"] for r in rows])
    print(f"  scatter went UP after removal: {worse_std} of {len(rows)} by std, "
          f"{worse_mad} by MAD")
    print(f"  leverage (elevation range / IQR) median {np.median(lev):.1f}, "
          f"max {lev.max():.1f}; {(lev > LEVERAGE_WARN).sum()} above "
          f"{LEVERAGE_WARN:.0f}")

    if (lev > LEVERAGE_WARN).any():
        print("\n  ! LEVERAGE. On the flagged pairs the valid pixels sit in a")
        print("    narrow elevation band and the line is extrapolated well past")
        print("    it. The slope is then set by a minority of pixels and applied")
        print("    to all of them. Treat those slopes as an error bar, not a")
        print("    correction: run with --report-only.")

    if worse_mad > worse_std:
        print("\n  Note: the two statistics disagree on those pairs. Least")
        print("    squares can only reduce the standard deviation, so a rise in")
        print("    MAD means the core of the distribution was moved apart to")
        print("    pull in the tails. That is a sign the fit is serving the")
        print("    outliers, not the field.")

    # A coin gives half the transitions. The old test fired at len(rows)//3,
    # which is BELOW chance - it announced alternation on evidence consistent
    # with no alternation at all, and it counted transitions across an
    # alphabetical file list that interleaves ascending and descending pairs.
    if transitions >= 6 and flips >= 0.75 * transitions:
        print("\n  THE SIGN REVERSES between consecutive pairs on the same track,")
        print(f"  {flips} of {transitions} transitions against {expected:.1f} expected by")
        print("  chance. Ground does not change direction that often; a")
        print("  water-vapour field does. This is atmosphere, not deformation.")
    elif transitions >= 6 and flips > expected:
        print(f"\n  The sign reverses more often than chance ({flips} of "
              f"{transitions}, {expected:.1f} expected)")
        print("  but not decisively. Do not lead with this; the variance split")
        print("  between phase and offsets is the stronger evidence.")
    else:
        print("\n  The sign is largely stable. A persistent elevation-correlated")
        print("  signal can be stratified delay OR real motion that happens to")
        print("  scale with height - one interferogram cannot separate them.")
        print("  Prefer --report-only here and quote the slope as an error bar.")

    print("\n  ON THE p COLUMN. It treats every valid pixel as an independent")
    print("  sample. Interferogram pixels at 80 m posting are strongly")
    print("  correlated in space, so the effective sample size is tens, not")
    print("  thousands, and these p-values are far smaller than the evidence")
    print("  supports. Read the correlation and the variance columns; the p")
    print("  column only separates 'a trend exists' from 'nothing at all'.")

    if args.report_only:
        print("\n  --report-only: nothing written. Quote the slope as an")
        print("  uncertainty on every displacement in this stack.")

    if args.csv:
        import csv as _csv
        p = Path(resolve(args.csv))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=sorted(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        logger.info("Wrote %s", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
