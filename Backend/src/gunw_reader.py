"""
gunw_reader.py
--------------
Read NISAR L2 GUNW products into line-of-sight displacement.

    unwrapped phase  ->  LOS displacement (mm)
    coherence        ->  quality gate
    connectedComponents -> unwrapping-reliability gate
    AOI polygon      ->  spatial clip
    reference region ->  removes the arbitrary phase offset

Why paths are discovered, not hardcoded
=======================================
NISAR L2 went public in 2026 and the group layout has moved between product
versions. Rather than hardcode /science/LSAR/GUNW/grids/... and fail on a
version bump, this walks the file and finds datasets by name. Run --inspect
on your first file to see exactly what was found and confirm it is sane.

Sign convention
===============
    d_los = -(lambda / 4*pi) * unwrapped_phase

Positive = motion AWAY from the satellite (range increase). For a descending
pass on a west-facing slope that usually reads as downslope motion, but ALWAYS
confirm against a known signal before interpreting. Use --flip-sign if your
convention is the opposite.

Unwrapped phase is RELATIVE. It carries an arbitrary constant per connected
component, so an absolute displacement number is meaningless until you
reference it to something you believe is stable. --ref-lat/--ref-lon does that.
Without a reference the script warns and reports relative values only.

Usage
-----
    python gunw_reader.py --inspect FILE.h5
    python gunw_reader.py --read FILE.h5 --ref-lat 28.21 --ref-lon 28.55
    python gunw_reader.py --read FILE.h5 --geotiff out.tif --quicklook out.png
    python gunw_reader.py --batch ./nisar_l2 --csv timeseries.csv

Requires: h5py, numpy.  Optional: rasterio + pyproj (GeoTIFF), matplotlib (PNG).
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import h5py
except ImportError:
    print("h5py is required:  pip install h5py", file=sys.stderr)
    raise SystemExit(1)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("gunw")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import NISAR, OUTPUTS, resolve  # noqa: E402

SPEED_OF_LIGHT = 299_792_458.0
NISAR_L_BAND_HZ = 1_257_500_000.0          # fallback if not in the file
# One wavelength for the whole project. Every module that needs a phase
# ceiling imports this rather than hardcoding its own - three different values
# were in circulation (0.2384 and 0.2439, 2.3% apart) and the headline ceiling
# depends on it. Products carry their own centre frequency; read_wavelength()
# uses it when present and falls back here when not.
NISAR_LAMBDA_M = SPEED_OF_LIGHT / NISAR_L_BAND_HZ      # 0.23840 m

TRACK_RE = re.compile(r"NISAR_L2_(?:PR|UR)_[A-Z]+_\d+_(\d{3})_([AD])_")


def track_of(name: str) -> str:
    """'ASC 098' / 'DESC 048' from a granule name; '' when it is not one."""
    m = TRACK_RE.search(str(name))
    if not m:
        return ""
    return ("ASC " if m.group(2) == "A" else "DESC ") + m.group(1)


# Product maturity. NISAR products are not all the same quality, and nothing in
# this repository used to say so. ASF releases them in stages, by ACQUISITION
# date (nisar-docs.asf.alaska.edu/availability-overview):
#
#     BETA         released 27 Feb 2026, acquisitions 17 Oct 2025 - 20 Jan 2026,
#                  "pre-calibration", with known issues
#     PROVISIONAL  released 20 Jul 2026, acquisitions from 17 Jun 2026,
#                  "fully calibrated", validated at a limited set of sites
#
# Every winter pair here is BETA and every summer pair PROVISIONAL, so any
# winter-against-monsoon comparison is ALSO a BETA-against-PROVISIONAL
# comparison. The two cannot be separated with this archive, and the gap
# between the two windows (20 Jan - 17 Jun 2026) is a release gap, not a
# physical one. Classified from the dates rather than from the CRID letter
# because the CRID encoding is not documented where it can be cited; the CRID
# is carried alongside so a reader can check.
BETA_WINDOW = ("20251017", "20260120")
PROVISIONAL_FROM = "20260617"
CRID_RE = re.compile(r"_([A-Z]\d{5})_[A-Z]_[A-Z]_[A-Z]_\d{3}")
STAMP_RE = re.compile(r"_(\d{8})T\d{6}")


def crid_of(name: str) -> str:
    """The composite release ID in a granule name, e.g. 'X05010'; '' if absent."""
    m = CRID_RE.search(str(name))
    return m.group(1) if m else ""


def product_maturity(name: str) -> str:
    """
    'BETA', 'PROVISIONAL' or 'UNKNOWN' for a granule name, from its dates.

    A pair is only as mature as its older acquisition, so both must fall in a
    window; a pair straddling the release gap would be 'UNKNOWN', not rounded.
    """
    stamps = STAMP_RE.findall(str(name))
    if len(stamps) < 4:
        return "UNKNOWN"
    ref, sec = stamps[0], stamps[2]
    lo, hi = BETA_WINDOW
    if lo <= ref <= hi and lo <= sec <= hi:
        return "BETA"
    if ref >= PROVISIONAL_FROM and sec >= PROVISIONAL_FROM:
        return "PROVISIONAL"
    return "UNKNOWN"


DEFAULT_COHERENCE = 0.3

# Areas of interest. Select at runtime with --aoi.
#
# CORRECTION (4 Sep 2026). The note that used to stand here said the Langtang
# box does not contain the 26 Aug 2026 failure zone, and that the source sat
# ~9 km north in the Lhende Khola catchment. That was wrong. The Lhende extent
# came from a report published immediately after the collapse and was never
# checked. The confirmed source zone is SOURCE_RING below: it contains
# 28.28771 N 85.52809 E, lies inside the Langtang box, and is 5.8 km SOUTH of
# the southern edge of the Lhende box.
#
# Every result computed with --aoi lhende therefore describes ground that does
# not contain the DETACHMENT. It is no longer the analysis AOI.
#
# SECOND CORRECTION (9 Sep 2026). The line that replaced it called Lhende "a
# labelled control region where nothing happened", and that is also wrong, in
# a way that would quietly poison any control measured there. The avalanche
# ran INTO the Lhende Khola: it dammed the channel, the barrier breached, and
# the outburst scoured 22 km of valley in about seven minutes. Enormous
# geomorphic change happened there - just not the detachment.
#
# So Lhende is the RUNOUT AND IMPOUNDMENT CORRIDOR. It is a legitimate AOI for
# studying the dam-and-breach step, and it is disqualified as a stable-ground
# control for exactly the period this project measures. A post-event noise
# floor taken there is measuring a rearranged valley floor, not instrument
# noise. Nothing in this repository may use it as a reference or a control.
LHENDE_IS_A_CONTROL = False
LANGTANG_RING = [
    (85.46683434336315, 28.324709534140283),
    (85.45910958140026, 28.277704299412660),
    (85.47267083017955, 28.253664665263376),
    (85.50734379401987, 28.245345485689977),
    (85.53583958259410, 28.244740597906280),
    (85.55884220710581, 28.249882034713380),
    (85.56485035529917, 28.272864249930198),
    (85.56193211189097, 28.289493024692200),
    (85.55918552985972, 28.307177143980763),
    (85.54854252448862, 28.316849264798610),
    (85.52468159309214, 28.328333765691790),
    (85.50614216438120, 28.329693690212682),
]

LHENDE_RING = [
    (85.44, 28.34), (85.62, 28.34), (85.62, 28.47), (85.44, 28.47),
]

# The confirmed 26 Aug 2026 source zone. Reported failure point sits at
# 28.28771 N 85.52809 E (28d17'15.76"N 85d31'41.13"E), 0.94 km from the
# centroid of the co-event decorrelation footprint measured on both geometries.
SOURCE_RING = [
    (85.46451876449485, 28.345047192655944),
    (85.46623537826439, 28.277644183693493),
    (85.48958132553001, 28.247707190492925),
    (85.53558657455345, 28.245287672353218),
    (85.55618593978782, 28.260106359331573),
    (85.55069277572532, 28.290342185190823),
    (85.54931948470970, 28.311199899644855),
    (85.53558657455345, 28.330240328302096),
    (85.51018069076439, 28.340212525080158),
    (85.49644778060814, 28.352903058100104),
]

AOIS = {"source": SOURCE_RING, "langtang": LANGTANG_RING, "lhende": LHENDE_RING}

AOI_RING = LANGTANG_RING          # module default; set_aoi() overrides


def set_aoi(name: str) -> None:
    """Switch the active AOI. Both readers import AOI_RING from here."""
    global AOI_RING
    if name not in AOIS:
        raise SystemExit(f"Unknown AOI '{name}'. Choose from {sorted(AOIS)}")
    AOI_RING = AOIS[name]


# ---------------------------------------------------------------------------
# HDF5 discovery
# ---------------------------------------------------------------------------
def walk(h5file) -> dict[str, tuple]:
    """Map every dataset path -> (shape, dtype)."""
    found: dict[str, tuple] = {}

    def visit(name, obj):
        if isinstance(obj, h5py.Dataset):
            found[name] = (obj.shape, obj.dtype)

    h5file.visititems(visit)
    return found


def find(datasets: dict, *patterns: str, ndim: int | None = None) -> str | None:
    """
    First dataset whose path matches all patterns (case-insensitive substrings).
    Prefers frequencyA and HH/VV polarisations when several match.
    """
    hits = []
    for path, (shape, _dtype) in datasets.items():
        low = path.lower()
        if all(p.lower() in low for p in patterns):
            if ndim is not None and len(shape) != ndim:
                continue
            hits.append(path)
    if not hits:
        return None
    hits.sort(key=lambda p: (
        0 if "frequencya" in p.lower() else 1,
        0 if re.search(r"/(hh|vv)/", p.lower()) else 1,
        len(p),
    ))
    return hits[0]


def read_wavelength(h5file, datasets: dict) -> float:
    """Radar wavelength in metres, from the file if possible."""
    for key in ("centerfrequency", "center_frequency", "radarcenterfrequency"):
        path = find(datasets, key)
        if path:
            try:
                freq = float(np.ravel(h5file[path][()])[0])
                if 1e8 < freq < 1e11:
                    lam = SPEED_OF_LIGHT / freq
                    logger.info("Wavelength from file: %.4f m (%.1f MHz)", lam, freq / 1e6)
                    return lam
            except Exception:
                pass
    lam = SPEED_OF_LIGHT / NISAR_L_BAND_HZ
    logger.warning("Center frequency not found; assuming NISAR L-band %.4f m", lam)
    return lam


def read_pair_dates(path: Path) -> tuple[str, str]:
    stamps = re.findall(r"_(\d{8})T\d{6}", path.name)
    if len(stamps) >= 4:
        return stamps[0], stamps[2]
    return "unknown", "unknown"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon. No shapely dependency."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def build_aoi_mask(xs: np.ndarray, ys: np.ndarray, ring, epsg: int | None) -> np.ndarray | None:
    """
    Boolean mask of grid cells inside the AOI ring.

    Works directly when the grid is geographic. For a projected grid it needs
    pyproj to convert the ring into grid coordinates.
    """
    ring_xy = ring
    if epsg and epsg != 4326:
        try:
            from pyproj import Transformer
            tf = Transformer.from_crs(4326, epsg, always_xy=True)
            ring_xy = [tf.transform(lon, lat) for lon, lat in ring]
        except ImportError:
            logger.warning("Grid is EPSG:%s and pyproj is missing - skipping AOI clip.", epsg)
            return None

    xmin = min(p[0] for p in ring_xy); xmax = max(p[0] for p in ring_xy)
    ymin = min(p[1] for p in ring_xy); ymax = max(p[1] for p in ring_xy)

    gx, gy = np.meshgrid(xs, ys)
    mask = (gx >= xmin) & (gx <= xmax) & (gy >= ymin) & (gy <= ymax)
    idx = np.argwhere(mask)
    for i, j in idx:
        if not point_in_ring(float(gx[i, j]), float(gy[i, j]), ring_xy):
            mask[i, j] = False
    return mask


# ---------------------------------------------------------------------------
# Core read
# ---------------------------------------------------------------------------
def read_gunw(
    path: Path,
    coherence_threshold: float = DEFAULT_COHERENCE,
    ref_lat: float | None = None,
    ref_lon: float | None = None,
    ref_radius_px: int = 5,
    clip_aoi: bool = True,
    flip_sign: bool = False,
    apply_iono: bool = True,
    auto_ref: bool = False,
) -> dict:
    with h5py.File(path, "r") as f:
        datasets = walk(f)

        p_phase = find(datasets, "unwrappedphase", ndim=2)
        if not p_phase:
            raise RuntimeError(
                "No 'unwrappedPhase' dataset found. Run --inspect and check the layout."
            )

        # A real GUNW carries THREE grids at different postings:
        #   unwrappedInterferogram   4275 x 4356
        #   pixelOffsets             4275 x 4356
        #   wrappedInterferogram    17100 x 17424
        # Several layer names (coherenceMagnitude, xCoordinates, projection)
        # appear in more than one of them. Searching the whole file by name can
        # therefore pair the WRAPPED coherence with the UNWRAPPED phase and blow
        # up on the shape mismatch. Anchor every sibling lookup to the group the
        # phase actually lives in, and to its parent for grid-level layers.
        grp = p_phase.rsplit("/", 1)[0]              # .../unwrappedInterferogram/HH
        parent = grp.rsplit("/", 1)[0]               # .../unwrappedInterferogram
        shape = datasets[p_phase][0]

        def sibling(*names, group=grp, ndim=2):
            for n in names:
                for cand in (f"{group}/{n}", f"{parent}/{n}"):
                    for key, (shp, _dt) in datasets.items():
                        if key.lower() == cand.lower() and len(shp) == ndim:
                            if ndim != 2 or shp == shape:
                                return key
            return None

        p_coh = sibling("coherenceMagnitude", "coherence")
        p_conn = sibling("connectedComponents")
        p_mask = sibling("mask", "layoverShadowMask")
        p_iono = sibling("ionospherePhaseScreen")
        p_x = sibling("xCoordinates", ndim=1)
        p_y = sibling("yCoordinates", ndim=1)

        logger.info("group      : %s", grp)
        logger.info("phase      : %s", shape)
        logger.info("coherence  : %s", p_coh.rsplit("/", 1)[-1] if p_coh else "NOT FOUND")
        logger.info("components : %s", p_conn.rsplit("/", 1)[-1] if p_conn else "not found")
        logger.info("ionosphere : %s", p_iono.rsplit("/", 1)[-1] if p_iono else "not found")

        phase = np.asarray(f[p_phase][()], dtype=np.float64)
        coherence = np.asarray(f[p_coh][()], dtype=np.float64) if p_coh else None
        components = np.asarray(f[p_conn][()]) if p_conn else None
        losmask = np.asarray(f[p_mask][()]) if p_mask else None
        iono = np.asarray(f[p_iono][()], dtype=np.float64) if p_iono else None
        xs = np.asarray(f[p_x][()], dtype=np.float64) if p_x else None
        ys = np.asarray(f[p_y][()], dtype=np.float64) if p_y else None

        epsg = None
        for cand in (f"{grp}/projection", f"{parent}/projection"):
            hit = next((k for k in datasets if k.lower() == cand.lower()), None)
            if hit:
                try:
                    val = f[hit]
                    epsg = int(val.attrs.get("epsg_code", np.ravel(val[()])[0]))
                    break
                except Exception:
                    pass

        wavelength = read_wavelength(f, datasets)

    # Ionospheric correction. L-band phase delay scales as 1/f^2, so NISAR is
    # far more affected than C-band and ships an estimated screen with every
    # GUNW. Subtract it unless explicitly told not to.
    iono_rms_mm = None
    scale = -(wavelength / (4.0 * math.pi)) * 1000.0
    if flip_sign:
        scale = -scale

    # Which pixels carry no phase, decided on the RAW array. GUNW fills nodata
    # with exact zeros, and the ionosphere screen is not zero there - so
    # subtracting the screen first turns every nodata pixel into a small
    # non-zero number and the `phase != 0` test below stops finding any of
    # them. That was survivable only because connectedComponents happens to
    # reject the same pixels, and that layer is optional.
    has_phase = phase != 0

    if iono is not None and apply_iono and iono.shape == phase.shape:
        good = np.isfinite(iono)
        if good.any():
            iono_rms_mm = float(np.sqrt(np.nanmean((iono[good] * scale) ** 2)))
        phase = phase - np.nan_to_num(iono, nan=0.0)
        logger.info("Ionosphere screen subtracted (RMS %.1f mm in LOS)",
                    iono_rms_mm if iono_rms_mm is not None else float("nan"))
    elif iono is not None and not apply_iono:
        logger.warning("Ionosphere screen present but NOT applied (--no-iono)")

    # phase -> LOS displacement, millimetres
    disp = phase * scale

    valid = np.isfinite(disp) & has_phase
    gates = {"finite": int(valid.sum())}

    if coherence is not None:
        valid &= coherence >= coherence_threshold
        gates[f"coherence>={coherence_threshold}"] = int(valid.sum())
    if components is not None:
        valid &= components > 0          # component 0 = not reliably unwrapped
        gates["connected_component>0"] = int(valid.sum())
    if losmask is not None and losmask.shape == valid.shape:
        # NISAR's GUNW 'mask' is NOT a layover/shadow flag. Per its own
        # description it is a three-digit code:
        #     hundreds = water flag in the reference RSLC (1 = water)
        #     tens     = subswath number in the reference RSLC (0 = invalid)
        #     units    = subswath number in the secondary RSLC (0 = invalid)
        # So a usable pixel is dry land that fell inside a real subswath in
        # BOTH acquisitions. Naively keeping mask == 0 keeps precisely the
        # pixels that were invalid in both - the exact inverse of what you want.
        m = losmask.astype(np.int32)
        water = m // 100
        ref_sub = (m // 10) % 10
        sec_sub = m % 10
        usable = (water == 0) & (ref_sub > 0) & (sec_sub > 0) & (losmask != 255)
        valid &= usable
        gates["land + valid subswath"] = int(valid.sum())

    # Quality mask WITHOUT the AOI clip. The reference point is usually chosen
    # on stable ground outside the area of interest, so it must not be clipped
    # away before we can use it.
    quality_valid = valid.copy()

    aoi_mask = None
    if clip_aoi and xs is not None and ys is not None:
        aoi_mask = build_aoi_mask(xs, ys, globals()['AOI_RING'], epsg)
        if aoi_mask is not None:
            valid &= aoi_mask
            gates["inside AOI"] = int(valid.sum())

    # Automatic reference selection. Guessing a reference point does not work:
    # on this scene the three "obvious" choices had ZERO usable pixels because
    # winter snow had decorrelated them, while a block 8 km away sat at 0.84
    # coherence. Pick the best fully-valid block instead, preferring ground
    # outside the AOI so the reference is not part of what is being measured.
    if auto_ref and xs is not None and ys is not None:
        blk = max(3, 2 * ref_radius_px + 1)
        ny, nx = coherence.shape if coherence is not None else disp.shape
        by, bx = ny // blk, nx // blk
        if by and bx:
            cut_v = quality_valid[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
            frac_valid = cut_v.mean(axis=(1, 3))
            full = cut_v.all(axis=(1, 3))

            # The reference MUST sit in the same connected component as the
            # target. Each component carries its own arbitrary phase constant,
            # so subtracting a value measured in a different one injects a
            # whole-fringe offset - which is exactly what produced medians of
            # 2 to 4 fringes (248, 285, 473 mm at lambda/2 = 122 mm) in the
            # summer pairs, and why the same pair processed twice differed by
            # 265 mm.
            target_comp = None
            comp_ok = np.ones_like(full)
            if components is not None and aoi_mask is not None:
                inside = components[quality_valid & aoi_mask]
                if inside.size:
                    ids, counts = np.unique(inside, return_counts=True)
                    target_comp = int(ids[int(np.argmax(counts))])
                    frac = float(counts.max() / inside.size)
                    logger.info("AOI dominant connected component: %d (%.0f%% of AOI)",
                                target_comp, 100 * frac)
                    if frac < 0.8:
                        logger.warning(
                            "AOI spans %d components; only %.0f%% is in the dominant "
                            "one. Displacements across component boundaries are not "
                            "comparable.", len(ids), 100 * frac)
                    # Every VALID pixel in the block must belong to the target
                    # component. Invalid pixels carry no phase and no component,
                    # so requiring them to match would reject blocks for having
                    # holes rather than for being in the wrong component.
                    cut_c = components[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
                    comp_ok = (~cut_v | (cut_c == target_comp)).all(axis=(1, 3))

            if aoi_mask is not None:
                cut_a = aoi_mask[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
                outside = ~cut_a.any(axis=(1, 3))     # keep blocks clear of the AOI
            else:
                outside = np.ones_like(full)

            # Demanding a block where EVERY pixel is valid is too strict in
            # summer: six of the fifteen pairs found none and fell back to
            # referencing inside the AOI, which subtracts part of the very
            # thing being measured and drives any AOI-wide motion toward zero.
            # A non-detection produced that way is an artefact of the
            # reference, not a result.
            #
            # So relax the fullness requirement in steps and take the first
            # level that finds anything. A block that is 80% valid, entirely
            # outside the AOI and entirely within the target component is a
            # far better reference than a perfect block inside it.
            if coherence is not None:
                cut_h = coherence[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
                mean_coh = np.where(cut_v, cut_h, 0.0).sum(axis=(1, 3)) / np.maximum(
                    cut_v.sum(axis=(1, 3)), 1)
            else:
                mean_coh = np.ones_like(frac_valid)

            chosen_level = None
            score = None
            for level in (1.0, 0.8, 0.6, 0.4):
                ok = (frac_valid >= level) & outside & comp_ok
                if level >= 1.0:
                    ok &= full
                if ok.any():
                    score = np.where(ok, mean_coh, -1.0)
                    chosen_level = level
                    break

            if score is not None and score.max() > 0:
                bi, bj = np.unravel_index(int(np.argmax(score)), score.shape)
                i = bi * blk + blk // 2
                j = bj * blk + blk // 2
                ref_lon, ref_lat = float(xs[j]), float(ys[i])   # grid units here
                logger.info("Auto reference: grid (%d, %d), coherence %.2f, "
                            "component %s, block %.0f%% valid",
                            i, j, float(score[bi, bj]),
                            components[i, j] if components is not None else "n/a",
                            100 * chosen_level)
                _auto_grid_ref = (i, j)
            elif target_comp is not None:
                # No clean block of that component outside the AOI. Referencing
                # anywhere else would be wrong, so reference INSIDE the AOI and
                # say so - relative motion within the AOI stays meaningful.
                sel = quality_valid & aoi_mask & (components == target_comp)
                if sel.any():
                    ii, jj = np.nonzero(sel)
                    k = int(np.argmax(coherence[sel])) if coherence is not None else 0
                    i, j = int(ii[k]), int(jj[k])
                    logger.warning(
                        "No stable block of component %d outside the AOI. "
                        "Referencing INSIDE the AOI at (%d, %d) - values are "
                        "relative to that point, not to stable ground.",
                        target_comp, i, j)
                    _auto_grid_ref = (i, j)
                else:
                    logger.warning("Auto reference found no usable block.")
                    _auto_grid_ref = None
            else:
                logger.warning("Auto reference found no fully-valid block.")
                _auto_grid_ref = None
        else:
            _auto_grid_ref = None
    else:
        _auto_grid_ref = None

    # reference correction - unwrapped phase is relative
    ref_value = None
    if _auto_grid_ref is not None:
        i, j = _auto_grid_ref
        r = ref_radius_px
        window = disp[max(0, i - r): i + r + 1, max(0, j - r): j + r + 1]
        wvalid = quality_valid[max(0, i - r): i + r + 1, max(0, j - r): j + r + 1]
        if wvalid.sum() > 0:
            ref_value = float(np.median(window[wvalid]))
            disp = disp - ref_value
            logger.info("Referenced automatically: subtracted %.2f mm (%d px)",
                        ref_value, int(wvalid.sum()))
        ref_lat = ref_lon = None      # consumed; skip the manual branch below
    if ref_lat is not None and ref_lon is not None and xs is not None and ys is not None:
        rx, ry = ref_lon, ref_lat
        if epsg and epsg != 4326:
            try:
                from pyproj import Transformer
                rx, ry = Transformer.from_crs(4326, epsg, always_xy=True).transform(ref_lon, ref_lat)
            except ImportError:
                logger.warning("pyproj missing - cannot place reference point on a projected grid.")
                rx = ry = None
        if rx is not None:
            j = int(np.argmin(np.abs(xs - rx)))
            i = int(np.argmin(np.abs(ys - ry)))
            r = ref_radius_px
            window = disp[max(0, i - r): i + r + 1, max(0, j - r): j + r + 1]
            wvalid = quality_valid[max(0, i - r): i + r + 1, max(0, j - r): j + r + 1]
            if wvalid.sum() > 0:
                ref_value = float(np.median(window[wvalid]))
                disp = disp - ref_value
                logger.info("Referenced to %.4f N %.4f E: subtracted %.2f mm (%d px)",
                            ref_lat, ref_lon, ref_value, int(wvalid.sum()))
            else:
                logger.warning(
                    "Reference window at %.4f N %.4f E has no usable pixels - NOT referenced. "
                    "Pick a point with good coherence, or widen --ref-radius.",
                    ref_lat, ref_lon)
    elif ref_value is None:
        logger.warning(
            "No reference applied. Values are RELATIVE - differences within the "
            "scene are meaningful, absolute magnitudes are not. Use --auto-ref."
        )

    return {
        "file": path.name,
        "reference_date": read_pair_dates(path)[0],
        "secondary_date": read_pair_dates(path)[1],
        "displacement_mm": disp,
        "coherence": coherence,
        "valid": valid,
        "xs": xs, "ys": ys, "epsg": epsg,
        "wavelength_m": wavelength,
        # How many grid cells the AOI actually covers. Without this the only
        # denominator available is the full frame - 17.7 M pixels ascending,
        # 26.6 M descending - and "coverage over the source zone" then gets
        # computed against a number that has nothing to do with the source
        # zone. It also makes the ascending/descending comparison unfair,
        # because the two frames differ in size by 1.5x.
        "aoi_px": int(aoi_mask.sum()) if aoi_mask is not None else None,
        "iono_rms_mm": iono_rms_mm,
        "ref_value_mm": ref_value,
        # Where the reference was taken, in grid indices. Exposed so a caller
        # can check it landed on stable ground outside the AOI rather than
        # inside the thing being measured - a distinction that changes the
        # answer and is invisible in the returned displacement.
        "ref_grid": _auto_grid_ref,
        "gates": gates,
    }


# ---------------------------------------------------------------------------
# Reporting / output
# ---------------------------------------------------------------------------
def report(result: dict) -> dict:
    disp, valid = result["displacement_mm"], result["valid"]
    total = disp.size
    print(f"\n=== {result['file']}")
    print(f"  pair            {result['reference_date']} -> {result['secondary_date']}")
    print(f"  grid            {disp.shape[0]} x {disp.shape[1]} = {total:,} px")
    print(f"  wavelength      {result['wavelength_m']:.4f} m")
    if result["epsg"]:
        print(f"  projection      EPSG:{result['epsg']}")
    if result.get("iono_rms_mm") is not None:
        print(f"  iono screen     {result['iono_rms_mm']:.1f} mm RMS (subtracted)")

    # Percentages here are of the FRAME, and are labelled so, because a gate
    # applied before the AOI clip has no AOI to be a fraction of. The coverage
    # figure that matters is printed below, against the AOI.
    print("\n  quality gates (cumulative, % of frame):")
    for name, n in result["gates"].items():
        print(f"    {name:<28} {n:>10,}  {100*n/total:>7.3f}%")

    n = int(valid.sum())
    aoi_px = result.get("aoi_px")

    # Two coverage numbers, and only one of them answers a question anybody
    # asks. `frame_pct` is valid pixels over the WHOLE product - tens of
    # millions of cells, most of them nowhere near the area of interest - so it
    # lands between 0.01 and 0.04 here and is useless for comparing anything.
    # `aoi_pct` is valid pixels over the cells the AOI actually covers, which
    # is what "coverage over the source zone" means.
    #
    # Quoting the wrong one cost this project a factor of ~16: winter ascending
    # coverage of the 82 km2 source zone is 55%, not the 3.5% that came from
    # reading a frame fraction as though it were an AOI fraction. It also made
    # the ascending/descending ratio meaningless, because the two frames are
    # different sizes.
    stats = {"file": result["file"], "reference": result["reference_date"],
             "secondary": result["secondary_date"], "valid_px": n,
             "aoi_px": aoi_px,
             "aoi_pct": round(100 * n / aoi_px, 2) if aoi_px else None,
             "frame_px": total,
             "frame_pct": round(100 * n / total, 4),
             # check_consistency sizes a fringe from this. It was never
             # written, so the fringe test always fell back to the module
             # constant instead of the wavelength the product reports.
             "wavelength_m": round(float(result["wavelength_m"]), 6),
             # Track, from the granule name, because nothing downstream can
             # recover it from an export filename and geometry is the largest
             # single control on data quality here. Without this column every
             # consumer that pools pairs is pooling ascending with descending,
             # whose floors differ about fivefold - the exact trap the GOFF
             # season analysis documents and then avoids.
             "track": track_of(result.get("file", "")),
             # BETA or PROVISIONAL: the season comparison is confounded with
             # it, so every row says which it is. See product_maturity().
             "maturity": product_maturity(result.get("file", "")),
             "crid": crid_of(result.get("file", "")),
             "processing": "UR" if "_UR_" in str(result.get("file", "")) else "PR"}

    if n == 0:
        print("\n  NO VALID PIXELS. Lower --coh-threshold or check the AOI overlaps the frame.")
        return stats

    vals = disp[valid]
    qs = np.percentile(vals, [1, 5, 25, 50, 75, 95, 99])
    print(f"\n  LOS displacement over {n:,} valid px (mm, +ve = away from satellite):")
    for label, q in zip(["p1", "p5", "p25", "median", "p75", "p95", "p99"], qs):
        print(f"    {label:<8}{q:>10.2f}")
    print(f"    {'min':<8}{vals.min():>10.2f}")
    print(f"    {'max':<8}{vals.max():>10.2f}")
    print(f"    {'std':<8}{vals.std():>10.2f}")

    if result["coherence"] is not None:
        print(f"\n  mean coherence (valid px): {result['coherence'][valid].mean():.3f}")

    if aoi_px:
        print(f"\n  coverage        {n:,} of {aoi_px:,} AOI cells = "
              f"{stats['aoi_pct']:.1f}% of the area of interest")
        print(f"                  ({stats['frame_pct']:.3f}% of the {total:,}-cell frame, "
              f"which is not a coverage figure)")

    # Gate on AOI coverage, not frame coverage. The frame fraction is ~0.03%
    # for every pair over an AOI this size, so this warning used to fire on all
    # 15 of them - including the winter pairs at 55% AOI coverage, which are
    # the usable ones. A warning that fires every time carries no information.
    cover = stats["aoi_pct"] if aoi_px else None
    if cover is not None and cover < 10:
        print(f"\n  WARNING: only {cover:.1f}% of the AOI survived gating. Treat with "
              "suspicion -")
        print("  likely monsoon decorrelation or a 24-day span. Consider GOFF instead.")
    elif cover is None:
        print("\n  NOTE: no AOI clip applied, so there is no coverage figure. Only the")
        print("  frame fraction is available, and it is not comparable between tracks.")

    stats.update({k: round(float(v), 3) for k, v in
                  zip(["p1", "p5", "p25", "median", "p75", "p95", "p99"], qs)})
    stats["min"] = round(float(vals.min()), 3)
    stats["max"] = round(float(vals.max()), 3)
    stats["std"] = round(float(vals.std()), 3)
    stats["mean_coherence"] = (round(float(result["coherence"][valid].mean()), 4)
                               if result["coherence"] is not None else None)
    return stats


def write_geotiff(result: dict, out: Path) -> None:
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ImportError:
        logger.error("rasterio is required for GeoTIFF output:  pip install rasterio")
        return
    xs, ys = result["xs"], result["ys"]
    if xs is None or ys is None:
        logger.error("No coordinate arrays - cannot georeference.")
        return
    data = np.where(result["valid"], result["displacement_mm"], np.nan).astype("float32")
    resx = abs(float(xs[1] - xs[0])); resy = abs(float(ys[1] - ys[0]))
    transform = from_origin(float(xs.min()) - resx / 2, float(ys.max()) + resy / 2, resx, resy)
    if ys[0] < ys[-1]:
        data = np.flipud(data)
    with rasterio.open(
        out, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
        count=1, dtype="float32", crs=f"EPSG:{result['epsg'] or 4326}",
        transform=transform, nodata=np.nan, compress="deflate",
    ) as dst:
        dst.write(data, 1)
        dst.update_tags(units="mm", pair=f"{result['reference_date']}_{result['secondary_date']}")
    logger.info("Wrote %s", out)



def aoi_grid(xs: np.ndarray, ys: np.ndarray, ring, epsg: int | None,
             pad_px: int = 2) -> dict | None:
    """
    The fixed output grid for this AOI at this product's posting.

    Why exports were not differenceable
    ===================================
    Each export used to be clipped to the bounding box of its own VALID pixels,
    which is a different rectangle in every product - 179x220 for one GUNW pair
    over this AOI and 181x142 for the next. Two rasters of the same ground at
    different sizes cannot be subtracted, so coherence-change mapping, the
    standard way to map a landslide after the fact and the method this project's
    own method notes recommend for it, was unreachable from the pipeline. Every
    comparison in the analysis had to crop to a common extent by hand, which is
    an invitation to a half-pixel error nobody would notice.

    So the grid is defined by the AOI, not by the data. Cell edges are anchored
    to absolute multiples of the pixel size in the projected CRS, and the AOI
    bounds are snapped outward onto them. Any two products in the same CRS at
    the same posting therefore produce byte-for-byte comparable rasters: same
    origin, same size, same pixel centres, with nodata wherever a particular
    product had nothing.

    The anchoring is what makes it work. Snapping to the AOI bounds alone would
    still drift, because the AOI corner is not generally a whole number of
    pixels from anywhere. Anchoring to multiples of `res` gives every product
    the same lattice to land on.

    Returns None when the product grid is not itself aligned to that lattice,
    because the only ways forward from there are to resample - which invents
    values - or to stop. Stopping, loudly, is the better one.
    """
    if xs is None or ys is None or len(xs) < 2 or len(ys) < 2:
        return None
    res_x = abs(float(xs[1] - xs[0]))
    res_y = abs(float(ys[1] - ys[0]))
    if res_x <= 0 or res_y <= 0:
        return None

    ring_xy = ring
    if epsg and epsg != 4326:
        try:
            from pyproj import Transformer
            tf = Transformer.from_crs(4326, epsg, always_xy=True)
            ring_xy = [tf.transform(lon, lat) for lon, lat in ring]
        except ImportError:
            logger.warning("pyproj missing - cannot place the AOI on a fixed grid")
            return None
    rx = [p[0] for p in ring_xy]
    ry = [p[1] for p in ring_xy]

    # Snap outward onto the absolute lattice, then pad so a clipped signal at
    # the AOI edge is not cut flush against it.
    x0 = math.floor(min(rx) / res_x) * res_x - pad_px * res_x
    x1 = math.ceil(max(rx) / res_x) * res_x + pad_px * res_x
    y0 = math.floor(min(ry) / res_y) * res_y - pad_px * res_y
    y1 = math.ceil(max(ry) / res_y) * res_y + pad_px * res_y
    width = int(round((x1 - x0) / res_x))
    height = int(round((y1 - y0) / res_y))
    if width <= 0 or height <= 0:
        return None

    # Where each product pixel centre lands. Half a pixel of tolerance: the
    # product must share the lattice, not merely overlap it.
    col_f = (np.asarray(xs, dtype=float) - (x0 + res_x / 2.0)) / res_x
    row_f = ((y1 - res_y / 2.0) - np.asarray(ys, dtype=float)) / res_y
    col_off = np.abs(col_f - np.round(col_f)).max()
    row_off = np.abs(row_f - np.round(row_f)).max()
    if col_off > 0.01 or row_off > 0.01:
        logger.warning(
            "Product grid is off the AOI lattice by %.3f px in x and %.3f px in y. "
            "Exporting on the product's own grid instead; this file will NOT align "
            "with the others and cannot be differenced.", col_off, row_off)
        return None

    from rasterio.transform import from_origin
    return {"transform": from_origin(x0, y1, res_x, res_y),
            "height": height, "width": width,
            "cols": np.round(col_f).astype(int),
            "rows": np.round(row_f).astype(int),
            "res_x": res_x, "res_y": res_y}


def place_on_grid(arr: np.ndarray, grid: dict) -> np.ndarray:
    """Drop a product-shaped array onto the fixed AOI grid, nodata elsewhere."""
    out = np.full((grid["height"], grid["width"]), np.nan, dtype="float32")
    rows, cols = grid["rows"], grid["cols"]
    rok = (rows >= 0) & (rows < grid["height"])
    cok = (cols >= 0) & (cols < grid["width"])
    if not rok.any() or not cok.any():
        return out
    sub = arr[np.ix_(rok, cok)]
    out[np.ix_(rows[rok], cols[cok])] = sub.astype("float32")
    return out


def export_clipped(result: dict, outdir: Path) -> dict:
    """
    Write only the AOI subset, so a 2.4 GB product becomes a file you can email.

    The full grid is 4275 x 4356; the AOI is a few hundred cells across. Writing
    the clip rather than the frame is a ~1000x reduction and loses nothing that
    the analysis uses. Bands: displacement (mm) and coherence.
    """
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ImportError:
        logger.error("rasterio required for --export:  pip install rasterio")
        return {}

    xs, ys = result["xs"], result["ys"]
    if xs is None or ys is None:
        logger.error("No coordinates - cannot export.")
        return {}

    valid = result["valid"]
    if not valid.any():
        logger.warning("Nothing valid to export for %s", result["file"][:44])
        return {}

    full_disp = np.where(valid, result["displacement_mm"], np.nan)
    full_coh = (np.where(valid, result["coherence"], np.nan)
                if result["coherence"] is not None else None)

    grid = aoi_grid(xs, ys, globals()["AOI_RING"], result["epsg"])
    if grid is not None:
        # Fixed AOI grid: every export over this AOI at this posting comes out
        # the same size, on the same lattice, and can be differenced directly.
        disp = place_on_grid(full_disp, grid)
        coh = place_on_grid(full_coh, grid) if full_coh is not None else None
        transform = grid["transform"]
    else:
        # Fall back to the old per-product clip, and the caller has already been
        # warned that this file will not align with the others.
        rows = np.any(valid, axis=1)
        cols = np.any(valid, axis=0)
        r0, r1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
        c0, c1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
        disp = full_disp[r0:r1, c0:c1].astype("float32")
        coh = full_coh[r0:r1, c0:c1].astype("float32") if full_coh is not None else None
        sub_x, sub_y = xs[c0:c1], ys[r0:r1]
        resx = abs(float(xs[1] - xs[0])); resy = abs(float(ys[1] - ys[0]))
        transform = from_origin(float(sub_x.min()) - resx / 2,
                                float(sub_y.max()) + resy / 2, resx, resy)
        if sub_y[0] < sub_y[-1]:
            disp = np.flipud(disp)
            if coh is not None:
                coh = np.flipud(coh)

    outdir.mkdir(parents=True, exist_ok=True)
    # Include the processing type. NASA publishes a routine (PR) and an urgent
    # (UR) product for the same pair of acquisitions, and naming by dates alone
    # made the second silently overwrite the first - 15 pairs processed, 14
    # files on disk, and no warning that one had vanished.
    proc = re.search(r"NISAR_L2_([A-Z]{2})_", Path(result["file"]).name)
    tag = f"_{proc.group(1)}" if proc else ""
    stem = f"{result['reference_date']}_{result['secondary_date']}{tag}"
    target = outdir / f"GUNW_{stem}.tif"
    count = 2 if coh is not None else 1
    with rasterio.open(target, "w", driver="GTiff", height=disp.shape[0],
                       width=disp.shape[1], count=count, dtype="float32",
                       crs=f"EPSG:{result['epsg'] or 4326}", transform=transform,
                       nodata=np.nan, compress="deflate", predictor=3) as dst:
        dst.write(disp, 1); dst.set_band_description(1, "LOS displacement (mm)")
        if coh is not None:
            dst.write(coh, 2); dst.set_band_description(2, "coherence")
        dst.update_tags(reference=result["reference_date"],
                        secondary=result["secondary_date"],
                        wavelength_m=str(result["wavelength_m"]),
                        source=result["file"])
    mb = target.stat().st_size / 1024 / 1024
    logger.info("Exported %s  %dx%d  %.2f MB", target.name, disp.shape[0], disp.shape[1], mb)
    return {"export_file": target.name, "export_mb": round(mb, 3),
            "export_rows": disp.shape[0], "export_cols": disp.shape[1]}


def find_common_reference(files: list[Path], coherence_threshold: float,
                          ref_radius_px: int = 5) -> tuple[float, float] | None:
    """
    One reference point for the whole stack, valid in every pair.

    Why this is not optional
    ========================
    --auto-ref picks the best block independently for each interferogram, and
    the inversion then sums those differences as though they shared a datum.
    They do not. Pair A may be referenced 8 km north-west and pair B 5 km
    south-east, so any differential atmosphere, residual ramp or real motion
    between those two blocks enters the time series as a step at that epoch -
    an artefact of bookkeeping that looks exactly like ground moving.

    SBAS requires a common reference across the stack. That is the whole
    reason the technique works: the arbitrary constant cancels only if it is
    the same constant every time.

    How
    ===
    A first pass reads only the quality layers - mask, coherence, components -
    never the phase, which is the expensive array. It intersects validity
    across every pair and scores blocks by mean coherence, so the chosen point
    is one that survives in all of them rather than one that happened to be
    good in the first. The second pass processes normally with that point
    fixed.

    Returns (lat, lon), or None if no block is valid in every pair - which is
    itself worth knowing, and means the stack cannot be inverted as one.
    """
    always = None
    coh_sum = None
    xs = ys = None
    epsg = None

    for k, fp in enumerate(files, 1):
        with h5py.File(fp, "r") as f:
            datasets = walk(f)
            p_phase = find(datasets, "unwrappedphase", ndim=2)
            if not p_phase:
                continue
            grp = p_phase.rsplit("/", 1)[0]
            shape = datasets[p_phase][0]

            def sib(*names, ndim=2):
                for n in names:
                    for base in (grp, grp.rsplit("/", 1)[0]):
                        key = f"{base}/{n}"
                        if key in datasets and datasets[key][0] == shape:
                            return key
                return None

            p_coh = sib("coherenceMagnitude")
            p_conn = sib("connectedComponents")
            p_mask = sib("mask", "layoverShadowMask")
            coh = np.asarray(f[p_coh][()], dtype=np.float32) if p_coh else None
            conn = np.asarray(f[p_conn][()]) if p_conn else None
            msk = np.asarray(f[p_mask][()]) if p_mask else None

            gx = np.asarray(f[f"{grp}/xCoordinates"][()], dtype=np.float64)
            gy = np.asarray(f[f"{grp}/yCoordinates"][()], dtype=np.float64)
            if xs is None:
                xs, ys = gx, gy
                try:
                    epsg = int(np.ravel(f[f"{grp}/projection"][()])[0])
                except Exception:
                    epsg = None
            elif gx.shape != xs.shape or not np.allclose(gx, xs):
                logger.error("Products are on different grids - a common reference "
                             "only exists within one geometry. Run per path.")
                return None

        v = np.ones(shape, dtype=bool)
        if coh is not None:
            v &= coh >= coherence_threshold
        if conn is not None:
            v &= conn > 0
        if msk is not None and msk.shape == shape:
            m = msk.astype(np.int32)
            v &= (m // 100 == 0) & ((m // 10) % 10 > 0) & (m % 10 > 0) & (msk != 255)

        always = v if always is None else (always & v)
        c = np.nan_to_num(coh, nan=0.0) if coh is not None else np.zeros(shape, np.float32)
        coh_sum = c.copy() if coh_sum is None else (coh_sum + c)
        logger.info("common-ref pass 1: %d/%d, %d px valid in all so far",
                    k, len(files), int(always.sum()))

    if always is None or not always.any():
        logger.error("No pixel is valid in every pair - no common reference exists.")
        return None

    aoi = build_aoi_mask(xs, ys, globals()["AOI_RING"], epsg)
    blk = max(3, 2 * ref_radius_px + 1)
    ny, nx = always.shape
    by, bx = ny // blk, nx // blk
    cut = always[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
    ok = cut.all(axis=(1, 3))
    if aoi is not None:
        ca = aoi[:by * blk, :bx * blk].reshape(by, blk, bx, blk)
        ok &= ~ca.any(axis=(1, 3))
    if not ok.any():
        logger.error("No block is valid in every pair AND outside the AOI.")
        return None

    cc = coh_sum[:by * blk, :bx * blk].reshape(by, blk, bx, blk).mean(axis=(1, 3))
    score = np.where(ok, cc, -1.0)
    bi, bj = np.unravel_index(int(np.argmax(score)), score.shape)
    i, j = bi * blk + blk // 2, bj * blk + blk // 2
    lon, lat = float(xs[j]), float(ys[i])
    if epsg and epsg != 4326:
        try:
            from pyproj import Transformer
            lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(lon, lat)
        except ImportError:
            logger.warning("pyproj missing - reference reported in grid units.")
    logger.info("Common reference for all %d pairs: %.5f N %.5f E "
                "(grid %d,%d, mean coherence %.2f)",
                len(files), lat, lon, i, j, float(score[bi, bj]) / len(files))
    return lat, lon


def verify_products(files: list[Path]) -> tuple[list[Path], list[tuple[Path, str]]]:
    """
    Open every product before processing any of them, and report the broken
    ones up front.

    A truncated download does not announce itself. HDF5 raises only when the
    file is opened, so in a batch the failure appears as one ERROR line among
    hundreds of INFO lines, the loop continues, and the run finishes with an
    exit code of zero and a time series quietly missing an epoch. That is the
    worst possible failure mode: the analysis still produces numbers, and the
    numbers are wrong by omission.

    Checking first turns it into a stated precondition. One of fifteen
    downloads arrived 195 MB short and would otherwise have removed
    2026-06-29 -> 2026-07-11 from the descending network without comment.
    """
    good, bad = [], []
    for f in files:
        try:
            with h5py.File(f, "r") as h:
                h.visititems(lambda name, obj: None)
            good.append(f)
        except Exception as exc:
            bad.append((f, str(exc)[:120]))

    if bad:
        logger.error("%d of %d products failed to open:", len(bad), len(files))
        for f, msg in bad:
            gb = f.stat().st_size / 2**30
            logger.error("  %s  (%.2f GB)", f.name, gb)
            logger.error("     %s", msg)
        print("\nThese are almost certainly incomplete downloads. Delete and")
        print("re-fetch them; a short file is not recoverable by any reader.")
        print("Processing continues with the %d that opened, but the network"
              % len(good))
        print("will be missing their epochs - check --network before trusting")
        print("any time series built from this run.\n")
    return good, bad


def check_consistency(rows: list[dict]) -> list[dict]:
    """
    Cross-check pairs that share both acquisition dates.

    NASA publishes a routine (PR) and an urgent-response (UR) product for the
    same two images. The images are identical, so the measured displacement
    must be identical too - any disagreement is processing, not ground motion.
    That makes duplicate pairs a free, absolute validation of the whole chain,
    and the strongest evidence available that a number is trustworthy.

    Unwrapping recovers phase only up to a whole number of cycles per connected
    component, so the failure mode has a signature: the disagreement lands on a
    near-integer multiple of lambda/2. Anything else is ordinary noise.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["reference"], r["secondary"]), []).append(r)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        return []

    print(f"\n{'='*74}")
    print("CONSISTENCY CHECK - same acquisitions processed more than once")
    print("=" * 74)

    suspect = []
    for (ref, sec), group in sorted(dupes.items()):
        # A pair with no valid pixels returns early from report() without a
        # 'median', so this used to raise KeyError on any duplicate group
        # containing one. That is not hypothetical: monsoon pairs run at 1-3%
        # AOI coverage and the co-event pair has both a routine and an urgent
        # product, so an empty one is exactly where duplicates live.
        measured = [g for g in group if g.get("median") is not None]
        if len(measured) < 2:
            print(f"\n  {ref} -> {sec}   {len(group)} products, but "
                  f"{len(group) - len(measured)} had no valid pixels")
            print("    Cannot cross-check: a product with nothing measured cannot")
            print("    agree or disagree with anything.")
            continue

        group = measured
        lam = group[0].get("wavelength_m") or NISAR_LAMBDA_M
        fringe_mm = float(lam) / 2 * 1000
        meds = [float(g["median"]) for g in group]
        spread = max(meds) - min(meds)
        n_fringes = spread / fringe_mm

        print(f"\n  {ref} -> {sec}   ({len(group)} products, "
              f"one fringe = {fringe_mm:.1f} mm)")
        for g in group:
            proc = re.search(r"NISAR_L2_([A-Z]{2})_", Path(g["file"]).name)
            coh = g.get("mean_coherence")
            coh_s = f"{float(coh):.2f}" if coh is not None else "n/a"
            print(f"    {proc.group(1) if proc else '??'}  median "
                  f"{float(g['median']):>9.2f} mm   coherence {coh_s}")
        print(f"    disagreement: {spread:.2f} mm = {n_fringes:.2f} fringes")

        if abs(n_fringes - round(n_fringes)) < 0.25 and round(n_fringes) >= 1:
            print(f"    UNWRAPPING AMBIGUITY. The same two images cannot move "
                  f"differently,\n"
                  f"    and the gap is {round(n_fringes)} whole fringe(s) - one run "
                  f"resolved a cycle\n"
                  f"    the other did not. Keep the product that agrees with the "
                  f"rest of the\n"
                  f"    stack; discard the outlier. Do NOT average them.")
            suspect.append({"reference": ref, "secondary": sec,
                            "spread_mm": spread, "fringes": n_fringes})
        elif spread > 0.5 * fringe_mm:
            print("    Large disagreement that is NOT a whole fringe - suspect "
                  "the reference\n    point or the coherence gate, not the "
                  "unwrapper.")
        else:
            print("    Consistent. This pair is independently validated.")

    return suspect


def write_quicklook(result: dict, out: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.error("matplotlib is required for quicklooks:  pip install matplotlib")
        return
    data = np.where(result["valid"], result["displacement_mm"], np.nan)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        logger.error("Nothing valid to plot.")
        return
    lim = float(np.percentile(np.abs(finite), 98)) or 1.0
    fig, ax = plt.subplots(figsize=(9, 7))
    im = ax.imshow(data, cmap="RdBu_r", vmin=-lim, vmax=lim)
    ax.set_title(f"{result['reference_date']} to {result['secondary_date']}  |  LOS mm")
    ax.set_xticks([]); ax.set_yticks([])
    fig.colorbar(im, ax=ax, label="LOS displacement (mm), +ve away from satellite")
    fig.tight_layout(); fig.savefig(out, dpi=140); plt.close(fig)
    logger.info("Wrote %s", out)


# ---------------------------------------------------------------------------
def inspect(path: Path) -> None:
    with h5py.File(path, "r") as f:
        datasets = walk(f)
    print(f"\n{path.name}: {len(datasets)} datasets\n")
    interesting = ("unwrapped", "coherence", "connected", "mask", "coordinate",
                   "projection", "frequency", "incidence", "offset", "ionosphere")
    print("--- likely relevant ---")
    for p, (shape, dt) in sorted(datasets.items()):
        if any(k in p.lower() for k in interesting):
            print(f"  {p}\n      shape={shape} dtype={dt}")
    print("\n--- 2-D grids ---")
    for p, (shape, dt) in sorted(datasets.items()):
        if len(shape) == 2 and shape[0] > 50 and shape[1] > 50:
            print(f"  {p}  {shape} {dt}")


def main() -> int:
    ap = argparse.ArgumentParser(description="NISAR GUNW -> LOS displacement")
    ap.add_argument("--inspect", metavar="FILE", help="dump the HDF5 layout and exit")
    ap.add_argument("--read", metavar="FILE", help="read one GUNW")
    ap.add_argument("--batch", metavar="DIR", nargs="?", const=str(NISAR / "GUNW"),
                    help="folder of GUNW products (default: data/nisar_l2/GUNW)")
    ap.add_argument("--coh-threshold", type=float, default=DEFAULT_COHERENCE)
    ap.add_argument("--ref-lat", type=float, help="reference point latitude (stable ground)")
    ap.add_argument("--ref-lon", type=float, help="reference point longitude")
    ap.add_argument("--ref-radius", type=int, default=5, help="reference window half-width in pixels")
    ap.add_argument("--no-clip", action="store_true", help="do not clip to the AOI ring")
    ap.add_argument("--flip-sign", action="store_true", help="invert the LOS sign convention")
    ap.add_argument("--aoi", choices=("source", "langtang", "lhende"), default="source",
                    help="which box to clip to. 'source' is the confirmed detachment zone and the default; 'lhende' is the runout corridor the avalanche dammed, not a control")
    ap.add_argument("--auto-ref", action="store_true",
                    help="pick the highest-coherence fully-valid block outside the AOI "
                         "as the reference, instead of guessing a lat/lon")
    ap.add_argument("--no-iono", action="store_true",
                    help="do NOT subtract the ionosphere phase screen")
    ap.add_argument("--geotiff", metavar="OUT.tif")
    ap.add_argument("--quicklook", metavar="OUT.png")
    ap.add_argument("--csv", metavar="OUT.csv", help="write per-pair statistics")
    ap.add_argument("--common-ref", action="store_true",
                    help="one reference point for the whole batch, valid in every "
                         "pair. SBAS requires this; --auto-ref alone does not "
                         "give it.")
    ap.add_argument("--export", metavar="DIR",
                    help="write AOI-clipped GeoTIFFs (small enough to send back)")
    args = ap.parse_args()

    set_aoi(args.aoi)

    if args.inspect:
        inspect(resolve(args.inspect)); return 0

    files: list[Path] = []
    if args.read:
        files = [resolve(args.read)]
    elif args.batch:
        files = sorted(p for p in resolve(args.batch).rglob("*.h5") if "GUNW" in p.name.upper())
        if not files:
            logger.error("No *GUNW*.h5 files in %s", args.batch); return 1
        logger.info("Found %d GUNW files", len(files))
        files, broken = verify_products(files)
        if not files:
            logger.error("No readable products.")
            return 1
        if args.common_ref:
            if args.ref_lat is not None or args.ref_lon is not None:
                logger.warning("--common-ref overrides --ref-lat/--ref-lon.")
            found = find_common_reference(files, args.coh_threshold, args.ref_radius)
            if not found:
                logger.error("Cannot establish a common reference; refusing to "
                             "invert pairs on different datums.")
                return 1
            args.ref_lat, args.ref_lon = found
            args.auto_ref = False
    else:
        ap.print_help(); return 0

    rows = []
    for fp in files:
        try:
            result = read_gunw(
                fp,
                coherence_threshold=args.coh_threshold,
                ref_lat=args.ref_lat, ref_lon=args.ref_lon, ref_radius_px=args.ref_radius,
                clip_aoi=not args.no_clip, flip_sign=args.flip_sign,
                apply_iono=not args.no_iono,
                auto_ref=args.auto_ref,
            )
        except Exception as exc:
            logger.error("%s: %s", fp.name, exc)
            continue
        row = report(result)
        if args.export:
            row.update(export_clipped(result, resolve(args.export)))
        rows.append(row)
        if args.geotiff and len(files) == 1:
            write_geotiff(result, Path(args.geotiff))
        if args.quicklook and len(files) == 1:
            write_quicklook(result, Path(args.quicklook))

    if rows:
        check_consistency(rows)

    if args.csv and rows:
        keys = sorted({k for r in rows for k in r})
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys); w.writeheader(); w.writerows(rows)
        logger.info("Wrote %s (%d pairs)", args.csv, len(rows))

    return 0


if __name__ == "__main__":
    sys.exit(main())
