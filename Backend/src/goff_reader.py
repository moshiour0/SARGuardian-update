"""
goff_reader.py
--------------
Read NISAR L2 GOFF (geocoded pixel offsets) into ground displacement.

Why GOFF and not GUNW
=====================
Interferometric phase is ambiguous once displacement between passes exceeds
lambda/4 - about 59.6 mm per 12-day pair at NISAR's 0.2384 m wavelength, i.e.
5 mm/day. Blatten was moving at 650 mm/day six days before it failed. Phase
cannot measure a slope that is actually failing; offset tracking can, because
it cross-correlates image patches and never wraps.

The trade is precision. GUNW resolves millimetres, GOFF resolves a fraction of
a resolution cell. Between the phase ceiling and the offset noise floor there
is a band of velocities neither product sees - measure that floor on the winter
pairs with --noise-floor rather than assuming it.

What is in the product
======================
    slantRangeOffset        metres, along the line of sight
    alongTrackOffset        metres, along the flight direction (~N-S)
    slantRangeOffsetVariance, alongTrackOffsetVariance   metres^2
    correlationSurfacePeak  0-1, the matching quality
    snr                     offset signal-to-noise

Two layers at different correlation window sizes. layer1 is the finer window
(sharper, noisier); layer2 the coarser (smoother, more robust). Compare them:
a signal present in both is far more credible than one in either alone.

NASA labels these "raw (unculled, unfiltered)", so gating and outlier rejection
are the caller's job. This module does both and says how much it removed.

Usage
-----
    python src/goff_reader.py --inspect FILE.h5
    python src/goff_reader.py --read FILE.h5 --auto-ref --quicklook out.png
    python src/goff_reader.py --batch data/nisar_l2/GOFF --csv outputs/goff.csv
    python src/goff_reader.py --noise-floor data/nisar_l2/GOFF/2025-12_winter

Requires: h5py, numpy.  Optional: pyproj (AOI clip on projected grids),
matplotlib (quicklooks).
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

import numpy as np

try:
    import h5py
except ImportError:
    print("h5py is required:  pip install h5py", file=sys.stderr)
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gunw_reader as _g  # noqa: E402
from gunw_reader import build_aoi_mask, walk, read_pair_dates, set_aoi  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("goff")

from gunw_reader import crid_of, product_maturity, track_of  # noqa: E402
from paths import NISAR, resolve  # noqa: E402

DEFAULT_CORRELATION = 0.3
DEFAULT_SNR = 3.0
MAD_SIGMA = 1.4826          # MAD -> Gaussian sigma


# ---------------------------------------------------------------------------
def layer_groups(datasets: dict) -> list[str]:
    """Every .../pixelOffsets/<pol>/<layerN> group present, in order."""
    groups = set()
    for path in datasets:
        m = re.match(r"(.*/pixelOffsets/[^/]+/layer\d+)/", path)
        if m:
            groups.add(m.group(1))
    return sorted(groups)


def robust_sigma(a: np.ndarray) -> float:
    """MAD-based scatter - immune to the gross outliers in unculled offsets."""
    a = a[np.isfinite(a)]
    if a.size == 0:
        return float("nan")
    return float(MAD_SIGMA * np.median(np.abs(a - np.median(a))))


def read_goff(
    path: Path,
    layer: str | int = "best",
    correlation_min: float = DEFAULT_CORRELATION,
    snr_min: float = DEFAULT_SNR,
    cull_sigma: float = 4.0,
    clip_aoi: bool = True,
    auto_ref: bool = True,
    deramp: bool = True,
) -> dict:
    with h5py.File(path, "r") as f:
        datasets = walk(f)
        groups = layer_groups(datasets)
        if not groups:
            raise RuntimeError("No pixelOffsets layer groups found. Run --inspect.")

        if layer == "best":
            chosen = groups              # read all, pick later
        elif isinstance(layer, int) or str(layer).isdigit():
            want = f"layer{int(layer)}"
            chosen = [g for g in groups if g.endswith("/" + want)] or groups[:1]
        else:
            chosen = [g for g in groups if g.endswith("/" + str(layer))] or groups[:1]

        results = {}
        for grp in chosen:
            def ds(name):
                key = f"{grp}/{name}"
                return np.asarray(f[key][()], dtype=np.float64) if key in datasets else None

            rng = ds("slantRangeOffset")
            azi = ds("alongTrackOffset")
            corr = ds("correlationSurfacePeak")
            snr = ds("snr")
            rng_var = ds("slantRangeOffsetVariance")
            if rng is None:
                continue

            xs = np.asarray(f[f"{grp}/xCoordinates"][()], dtype=np.float64)
            ys = np.asarray(f[f"{grp}/yCoordinates"][()], dtype=np.float64)
            epsg = None
            if f"{grp}/projection" in datasets:
                try:
                    v = f[f"{grp}/projection"]
                    epsg = int(v.attrs.get("epsg_code", np.ravel(v[()])[0]))
                except Exception:
                    pass
            # Key by polarisation AND layer. A quad-pol product carries
            # HH/layer1..3 and VV/layer1..3; keying on the layer name alone
            # lets VV silently overwrite HH and the read collapses to nothing.
            pol_name = grp.split("/")[-2]
            results[f"{pol_name}/{grp.rsplit('/', 1)[-1]}"] = dict(
                rng=rng, azi=azi, corr=corr, snr=snr, rng_var=rng_var,
                xs=xs, ys=ys, epsg=epsg, group=grp,
            )

    if not results:
        raise RuntimeError("No slantRangeOffset found in any layer.")

    out = {}
    for name, L in results.items():
        rng, azi = L["rng"], L["azi"]
        gates = {"finite": int(np.isfinite(rng).sum())}
        valid = np.isfinite(rng)
        if azi is not None:
            valid &= np.isfinite(azi)

        if L["corr"] is not None:
            valid &= L["corr"] >= correlation_min
            gates[f"correlation>={correlation_min}"] = int(valid.sum())
        if L["snr"] is not None:
            valid &= L["snr"] >= snr_min
            gates[f"snr>={snr_min}"] = int(valid.sum())

        quality_valid = valid.copy()

        aoi_mask = None
        if clip_aoi:
            aoi_mask = build_aoi_mask(L["xs"], L["ys"], _g.AOI_RING, L["epsg"])
            if aoi_mask is not None:
                valid &= aoi_mask
                gates["inside AOI"] = int(valid.sum())

        # Reference: offsets carry a bias from orbit and coregistration error,
        # exactly as unwrapped phase carries an arbitrary constant.
        ref_rng = ref_azi = None
        if auto_ref:
            blk = 11
            ny, nx = rng.shape
            by, bx = ny // blk, nx // blk
            if by and bx:
                fullv = quality_valid[:by * blk, :bx * blk].reshape(by, blk, bx, blk).all(axis=(1, 3))
                if aoi_mask is not None:
                    ina = aoi_mask[:by * blk, :bx * blk].reshape(by, blk, bx, blk).any(axis=(1, 3))
                    fullv &= ~ina
                if L["corr"] is not None:
                    sc = L["corr"][:by * blk, :bx * blk].reshape(by, blk, bx, blk).mean(axis=(1, 3))
                    score = np.where(fullv, sc, -1.0)
                else:
                    score = fullv.astype(float) - 1.0 * (~fullv)
                if score.max() > 0:
                    bi, bj = np.unravel_index(int(np.argmax(score)), score.shape)
                    i, j = bi * blk + blk // 2, bj * blk + blk // 2
                    w = quality_valid[i - 5:i + 6, j - 5:j + 6]
                    if w.sum() > 0:
                        ref_rng = float(np.median(rng[i - 5:i + 6, j - 5:j + 6][w]))
                        rng = rng - ref_rng
                        if azi is not None:
                            ref_azi = float(np.median(azi[i - 5:i + 6, j - 5:j + 6][w]))
                            azi = azi - ref_azi
                        logger.info("[%s] auto reference: corr %.2f, subtracted "
                                    "range %+.3f m, azimuth %+.3f m",
                                    name, float(score[bi, bj]), ref_rng, ref_azi or 0.0)

        # Deramp. Residual orbit and coregistration error leaves a near-planar
        # tilt across an offset field, so subtracting a single constant is not
        # enough - a stable winter pair still showed a -646 mm median before
        # this. Fit a + bx + cy on good ground OUTSIDE the AOI and remove it,
        # so the plane is never fitted to the signal being measured.
        ramp_rms = None
        if deramp:
            fit_mask = quality_valid.copy()
            if aoi_mask is not None:
                fit_mask &= ~aoi_mask
            if fit_mask.sum() > 5000:
                gy, gx = np.nonzero(fit_mask)
                step = max(1, gy.size // 200_000)          # cap the design matrix
                gy, gx = gy[::step], gx[::step]
                zr = rng[gy, gx]
                keep = np.abs(zr - np.median(zr)) <= 4 * robust_sigma(zr)
                gy, gx, zr = gy[keep], gx[keep], zr[keep]
                A = np.column_stack([np.ones_like(gx, dtype=float), gx.astype(float),
                                     gy.astype(float)])
                coef, *_ = np.linalg.lstsq(A, zr, rcond=None)
                YY, XX = np.mgrid[0:rng.shape[0], 0:rng.shape[1]]
                plane = coef[0] + coef[1] * XX + coef[2] * YY
                ramp_rms = float(np.sqrt(np.mean((plane[fit_mask] * 1000.0) ** 2)))
                rng = rng - plane
                if azi is not None:
                    za = azi[gy, gx]
                    coefa, *_ = np.linalg.lstsq(A, za, rcond=None)
                    azi = azi - (coefa[0] + coefa[1] * XX + coefa[2] * YY)
                # The RMS is of the plane over the FIT region, which is the
                # stable ground outside the AOI - not over the AOI, where the
                # plane is interpolated across a hole. Saying which matters:
                # they are different numbers and only the second describes
                # what was removed from the measurement.
                inside = ~aoi_mask if aoi_mask is None else aoi_mask
                aoi_ramp = (float(np.sqrt(np.mean((plane[inside] * 1000.0) ** 2)))
                            if inside.any() else float("nan"))
                logger.info("[%s] deramped: plane is %.0f mm RMS on the stable "
                            "ground it was fitted to (%d px outside the AOI), "
                            "%.0f mm RMS across the AOI itself",
                            name, ramp_rms, int(gy.size), aoi_ramp)
                # The plane carries a constant term, so it re-removes whatever
                # the auto-reference already subtracted. Two mechanisms, one
                # job: the reference chooses WHERE zero is when deramping is
                # off, and is otherwise absorbed here. The logged reference
                # offset above is therefore informational, not the offset that
                # ends up in the data.
                if ref_rng is not None:
                    logger.info("[%s] (the auto-reference constant %+.3f m is "
                                "absorbed by the plane's intercept)", name, ref_rng)
            else:
                logger.warning("[%s] too little stable ground to deramp", name)

        # Cull gross outliers. NASA ships these unculled and a raw offset field
        # always contains mismatches that are metres wrong.
        if valid.sum() > 10 and cull_sigma > 0:
            s = robust_sigma(rng[valid])
            med = float(np.median(rng[valid]))
            if np.isfinite(s) and s > 0:
                keep = np.abs(rng - med) <= cull_sigma * s
                valid &= keep
                gates[f"within {cull_sigma:g}x MAD"] = int(valid.sum())

        out[name] = {
            "layer": name,
            "range_m": rng,
            "azimuth_m": azi,
            "correlation": L["corr"],
            "snr": L["snr"],
            "range_var": L["rng_var"],
            "valid": valid,
            "xs": L["xs"], "ys": L["ys"], "epsg": L["epsg"],
            # Cells the AOI covers, so coverage has a denominator that means
            # something. See gunw_reader.report for what quoting the frame
            # fraction instead cost this project.
            "aoi_px": int(aoi_mask.sum()) if aoi_mask is not None else None,
            "gates": gates,
            "ramp_rms_mm": ramp_rms,
            "ref_range_m": ref_rng,
            "ref_azimuth_m": ref_azi,
        }

    ref, sec = read_pair_dates(path)
    return {"file": path.name, "reference_date": ref, "secondary_date": sec,
            "layers": out}


# ---------------------------------------------------------------------------
def export_clipped(res: dict, outdir: Path) -> list[dict]:
    """
    Write the AOI window of each layer as a 3-band GeoTIFF.

    A GOFF product is about 1 GB; the AOI window is a fraction of a megabyte.
    Without this the only way to free the disk is to delete the products, and
    with them every per-pixel value - the summary CSV keeps medians and
    percentiles but cannot give back a map. Anything spatial (where inside the
    AOI moved, how the offset field is shaped, whether a signal is coherent or
    speckle) would then need a re-download.

    Bands: slant-range offset (mm), along-track offset (mm), correlation. Range
    is the line-of-sight component and drops straight into the same inversion
    as GUNW phase; azimuth is kept because it carries the across-track motion
    that no single interferogram can see.
    """
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ImportError:
        logger.error("rasterio required for --export:  pip install rasterio")
        return []

    out = []
    proc = re.search(r"NISAR_L2_([A-Z]{2})_", Path(res["file"]).name)
    tag = f"_{proc.group(1)}" if proc else ""
    outdir.mkdir(parents=True, exist_ok=True)

    for name, L in sorted(res["layers"].items()):
        valid = L["valid"]
        if not valid.any() or L["xs"] is None:
            continue
        # Fixed AOI grid, so two layers or two pairs over the same ground come
        # out the same size and can be differenced. See gunw_reader.aoi_grid.
        grid = _g.aoi_grid(L["xs"], L["ys"], _g.AOI_RING, L["epsg"])

        rows_any = valid.any(axis=1)
        cols_any = valid.any(axis=0)
        r0, r1 = int(np.argmax(rows_any)), int(len(rows_any) - np.argmax(rows_any[::-1]))
        c0, c1 = int(np.argmax(cols_any)), int(len(cols_any) - np.argmax(cols_any[::-1]))

        def band(key, scale=1.0):
            if L.get(key) is None:
                return None
            full = np.where(valid, L[key] * scale, np.nan)
            if grid is not None:
                return _g.place_on_grid(full, grid)
            return full[r0:r1, c0:c1].astype("float32")

        bands = [(band("range_m", 1000.0), "slant-range offset (mm)"),
                 (band("azimuth_m", 1000.0), "along-track offset (mm)"),
                 (band("correlation"), "correlation")]
        bands = [(a, d) for a, d in bands if a is not None]
        if not bands:
            continue

        if grid is not None:
            transform = grid["transform"]
        else:
            xs, ys = L["xs"][c0:c1], L["ys"][r0:r1]
            resx = abs(float(L["xs"][1] - L["xs"][0]))
            resy = abs(float(L["ys"][1] - L["ys"][0]))
            transform = from_origin(float(xs.min()) - resx / 2,
                                    float(ys.max()) + resy / 2, resx, resy)
            if ys[0] < ys[-1]:
                bands = [(np.flipud(a), d) for a, d in bands]

        safe = name.replace("/", "-")
        target = outdir / (f"GOFF_{res['reference_date']}_{res['secondary_date']}"
                           f"{tag}_{safe}.tif")
        with rasterio.open(target, "w", driver="GTiff",
                           height=bands[0][0].shape[0], width=bands[0][0].shape[1],
                           count=len(bands), dtype="float32",
                           crs=f"EPSG:{L['epsg'] or 4326}", transform=transform,
                           nodata=np.nan, compress="deflate", predictor=3) as dst:
            for k, (arr, desc) in enumerate(bands, 1):
                dst.write(arr, k)
                dst.set_band_description(k, desc)
            dst.update_tags(reference=res["reference_date"],
                            secondary=res["secondary_date"],
                            layer=name, source=res["file"])
        mb = target.stat().st_size / 1024 / 1024
        logger.info("Exported %s  %dx%d  %.2f MB", target.name,
                    bands[0][0].shape[0], bands[0][0].shape[1], mb)
        out.append({"layer": name, "export_file": target.name,
                    "export_mb": round(mb, 3)})
    return out


def report(res: dict, span_days: float | None = None) -> list[dict]:
    print(f"\n=== {res['file']}")
    print(f"  pair  {res['reference_date']} -> {res['secondary_date']}"
          + (f"   ({span_days:.0f} d)" if span_days else ""))
    rows = []
    for name, L in sorted(res["layers"].items()):
        v = L["valid"]
        total = L["range_m"].size
        n = int(v.sum())
        print(f"\n  --- {name} ---")
        for k, c in L["gates"].items():
            print(f"    {k:<26}{c:>12,}  {100*c/total:>7.3f}% of frame")
        if L.get("aoi_px"):
            print(f"    {'-> coverage of the AOI':<26}{n:>12,}  "
                  f"{100*n/L['aoi_px']:>7.1f}% of {L['aoi_px']:,} AOI cells")
        if n == 0:
            print("    NO VALID PIXELS")
            continue

        r = L["range_m"][v] * 1000.0            # mm
        sig_r = robust_sigma(r)
        row = {"file": res["file"], "layer": name,
               "reference": res["reference_date"], "secondary": res["secondary_date"],
               "valid_px": n,
               "aoi_px": L.get("aoi_px"),
               "aoi_pct": (round(100 * n / L["aoi_px"], 2)
                           if L.get("aoi_px") else None),
               "frame_px": total,
               "frame_pct": round(100 * n / total, 4),
               "range_median_mm": round(float(np.median(r)), 2),
               "track": track_of(res.get("file", "")),
               "maturity": product_maturity(res.get("file", "")),
               "crid": crid_of(res.get("file", "")),
               "processing": "UR" if "_UR_" in str(res.get("file", "")) else "PR",
               "range_mad_sigma_mm": round(sig_r, 2),
               "range_p5_mm": round(float(np.percentile(r, 5)), 2),
               "range_p95_mm": round(float(np.percentile(r, 95)), 2),
               "range_max_abs_mm": round(float(np.max(np.abs(r))), 2)}
        print(f"    range offset  median {row['range_median_mm']:>10.1f} mm   "
              f"robust sigma {sig_r:>8.1f} mm")
        print(f"                  p5..p95 {row['range_p5_mm']:>9.1f} .. "
              f"{row['range_p95_mm']:.1f} mm   |max| {row['range_max_abs_mm']:.1f} mm")

        if L["azimuth_m"] is not None:
            a = L["azimuth_m"][v] * 1000.0
            row["azimuth_median_mm"] = round(float(np.median(a)), 2)
            row["azimuth_mad_sigma_mm"] = round(robust_sigma(a), 2)
            mag = np.hypot(r, a)
            row["magnitude_p95_mm"] = round(float(np.percentile(mag, 95)), 2)
            print(f"    azimuth       median {row['azimuth_median_mm']:>10.1f} mm   "
                  f"robust sigma {row['azimuth_mad_sigma_mm']:>8.1f} mm")
            print(f"    magnitude p95 {row['magnitude_p95_mm']:>10.1f} mm")

        if L["correlation"] is not None:
            row["mean_correlation"] = round(float(L["correlation"][v].mean()), 3)
            print(f"    mean correlation {row['mean_correlation']:.3f}")

        if span_days:
            row["span_days"] = span_days
            row["range_velocity_mm_day"] = round(row["range_median_mm"] / span_days, 3)
            row["detect_floor_mm_day"] = round(3 * sig_r / span_days, 2)
            print(f"    implied range velocity {row['range_velocity_mm_day']:+.2f} mm/day")
            print(f"    3-sigma detection floor {row['detect_floor_mm_day']:.1f} mm/day")
        rows.append(row)
    return rows


def span_of(name: str) -> float | None:
    from datetime import datetime
    s = re.findall(r"_(\d{8})T\d{6}", name)
    if len(s) < 4:
        return None
    a = datetime.strptime(s[0], "%Y%m%d")
    b = datetime.strptime(s[2], "%Y%m%d")
    return (b - a).days


def noise_floor(directory: Path, **kw) -> None:
    """
    Measure the offset-tracking noise floor on pairs you believe are stable.

    On a static winter glacier the scatter in the referenced offset field IS
    the detection threshold. This turns an assumed noise level into a measured
    one, and it is the number that defines the blind band between the GUNW
    phase ceiling and GOFF sensitivity.
    """
    files = sorted(p for p in directory.rglob("*.h5") if "GOFF" in p.name.upper())
    if not files:
        logger.error("No GOFF files under %s", directory)
        return
    logger.info("Measuring noise floor on %d stable pairs", len(files))

    all_rows = []
    for fp in files:
        try:
            res = read_goff(fp, **kw)
        except Exception as exc:
            logger.error("%s: %s", fp.name, exc)
            continue
        all_rows += report(res, span_days=span_of(fp.name))

    if not all_rows:
        return
    print("\n" + "=" * 78)
    print("OFFSET-TRACKING NOISE FLOOR")
    print("=" * 78)
    for lay in sorted({r["layer"] for r in all_rows}):
        sub = [r for r in all_rows if r["layer"] == lay]
        sig = np.array([r["range_mad_sigma_mm"] for r in sub])
        floors = np.array([r.get("detect_floor_mm_day", np.nan) for r in sub], dtype=float)
        print(f"\n  {lay}: {len(sub)} pairs")
        print(f"    range sigma      median {np.median(sig):>8.1f} mm   "
              f"range {sig.min():.1f} - {sig.max():.1f} mm")
        good = floors[np.isfinite(floors)]
        if good.size:
            print(f"    3-sigma floor    median {np.median(good):>8.1f} mm/day  "
                  f"range {good.min():.1f} - {good.max():.1f}")
            print(f"    -> GOFF cannot see motion slower than ~{np.median(good):.0f} mm/day "
                  f"on a 12-day pair")
    from gunw_reader import NISAR_LAMBDA_M
    ceil_mm = NISAR_LAMBDA_M / 4 * 1000.0
    print(f"\n  GUNW phase ceiling for comparison: {ceil_mm:.1f} mm per 12-day "
          f"pair = {ceil_mm/12:.2f} mm/day")
    print("  Anything between those two numbers is invisible to both products.")


def quicklook(res: dict, out: Path, layer: str | None = None) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.error("matplotlib required for --quicklook")
        return
    name = layer or sorted(res["layers"])[0]
    L = res["layers"][name]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    for ax, key, title in ((axes[0], "range_m", "Slant range offset"),
                           (axes[1], "azimuth_m", "Along-track offset")):
        if L[key] is None:
            ax.axis("off"); continue
        d = np.where(L["valid"], L[key] * 1000.0, np.nan)
        fin = d[np.isfinite(d)]
        lim = float(np.percentile(np.abs(fin), 98)) if fin.size else 1.0
        im = ax.imshow(d, cmap="RdBu_r", vmin=-lim, vmax=lim)
        ax.set_title(f"{title} (mm)"); ax.set_xticks([]); ax.set_yticks([])
        fig.colorbar(im, ax=ax, fraction=.046)
    fig.suptitle(f"{res['reference_date']} to {res['secondary_date']}  |  GOFF {name}")
    fig.tight_layout(); fig.savefig(out, dpi=140); plt.close(fig)
    logger.info("Wrote %s", out)


def inspect(path: Path) -> None:
    with h5py.File(path, "r") as f:
        datasets = walk(f)
        groups = layer_groups(datasets)
        print(f"\n{path.name}")
        print(f"  {len(datasets)} datasets, {len(groups)} offset layer(s)")
        for g in groups:
            print(f"\n  {g}")
            for k, (shp, dt) in sorted(datasets.items()):
                if k.startswith(g + "/"):
                    u = f[k].attrs.get("units")
                    u = u.decode() if isinstance(u, bytes) else u
                    print(f"    {k.rsplit('/',1)[-1]:<28}{str(shp):<16}{dt}  {u or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description="NISAR GOFF -> ground displacement")
    m = ap.add_mutually_exclusive_group(required=True)
    m.add_argument("--inspect", metavar="FILE")
    m.add_argument("--read", metavar="FILE")
    m.add_argument("--batch", metavar="DIR", nargs="?", const=str(NISAR / "GOFF"),
                   help="folder of GOFF products (default: data/nisar_l2/GOFF)")
    m.add_argument("--noise-floor", metavar="DIR",
                   help="measure the detection floor on pairs you believe are stable")
    ap.add_argument("--aoi", choices=("source", "langtang", "lhende"), default="source",
                    help="which box to clip to. 'source' is the confirmed detachment zone and the default; 'lhende' is the runout corridor the avalanche dammed, not a control")
    ap.add_argument("--layer", default="best", help="layer1, layer2, or best (both)")
    ap.add_argument("--correlation", type=float, default=DEFAULT_CORRELATION)
    ap.add_argument("--snr", type=float, default=DEFAULT_SNR)
    ap.add_argument("--cull-sigma", type=float, default=4.0)
    ap.add_argument("--no-clip", action="store_true")
    ap.add_argument("--no-auto-ref", action="store_true")
    ap.add_argument("--no-deramp", action="store_true",
                    help="keep the planar orbital ramp (default: remove it)")
    ap.add_argument("--quicklook", metavar="OUT.png")
    ap.add_argument("--export", metavar="DIR",
                    help="write AOI-clipped GeoTIFFs. Do this BEFORE deleting "
                         "products - the CSV keeps statistics, not maps.")
    ap.add_argument("--csv", metavar="OUT.csv")
    args = ap.parse_args()

    set_aoi(args.aoi)

    kw = dict(layer=args.layer, correlation_min=args.correlation, snr_min=args.snr,
              cull_sigma=args.cull_sigma, clip_aoi=not args.no_clip,
              auto_ref=not args.no_auto_ref, deramp=not args.no_deramp)

    if args.inspect:
        inspect(resolve(args.inspect)); return 0

    if args.noise_floor:
        noise_floor(resolve(args.noise_floor), **kw); return 0

    files = [resolve(args.read)] if args.read else sorted(
        p for p in resolve(args.batch).rglob("*.h5") if "GOFF" in p.name.upper())
    if not files:
        logger.error("No GOFF files found"); return 1

    rows = []
    for fp in files:
        try:
            res = read_goff(fp, **kw)
        except Exception as exc:
            logger.error("%s: %s", fp.name, exc); continue
        new = report(res, span_days=span_of(fp.name))
        if args.export:
            exp = {e["layer"]: e for e in export_clipped(res, resolve(args.export))}
            for r in new:
                r.update({k: v for k, v in exp.get(r.get("layer"), {}).items()
                          if k != "layer"})
        rows += new
        if args.quicklook and len(files) == 1:
            quicklook(res, Path(args.quicklook))

    if args.csv and rows:
        keys = sorted({k for r in rows for k in r})
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys); w.writeheader(); w.writerows(rows)
        logger.info("Wrote %s (%d rows)", args.csv, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
