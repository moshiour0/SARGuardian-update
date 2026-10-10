"""
geometry_merge.py
-----------------
Combine ascending and descending line-of-sight time series into one denser
record of downslope motion.

The rule you cannot break
=========================
You can NEVER interfere an ascending acquisition with a descending one. The
look geometry differs, the scattering differs, coherence is zero. That is
physics.

But once each geometry has been inverted separately into its own LOS
displacement series - which is what timeseries.py produces - you are no longer
combining *phase*, you are combining *measurements*. Those can be merged, and
the merged record is much denser than any single track.

Over the Langtang AOI: five tracks (NISAR ASC 98 + DESC 48, Sentinel-1 ASC 85
+ DESC 19 + DESC 121) sample at a median of 4 days, against 12 days for any one
of them alone. Per the detectability sweep, that is the difference between
never detecting a 10-day precursor and marginally detecting one.

How the projection works
========================
InSAR measures only the component of motion along the line of sight:

    d_los = d . los_hat

With a single geometry you cannot separate subsidence from horizontal creep.
The standard fix on a hillslope is to assume motion is downslope, take that
direction from the DEM, and solve for its magnitude:

    d_downslope = d_los / (slope_hat . los_hat)

That denominator is the *sensitivity*. When it approaches zero the track is
effectively blind to the motion, and dividing by it amplifies noise without
limit. This tool computes it per track and refuses to use geometries below a
threshold - which is the whole point, because a slope facing across the look
direction can be moving fast and show nothing.

Satellite geometry
==================
Heading is derived from orbital inclination and latitude:

    sin(heading_ascending) = cos(inclination) / cos(latitude)

For a right-looking sensor the target-to-satellite unit vector in ENU is

    E = -sin(theta) cos(heading)
    N =  sin(theta) sin(heading)
    U =  cos(theta)

Check: descending Sentinel-1 at 28 N has heading ~189 deg, giving E > 0 - the
satellite is east of the target, which is correct for a west-looking descending
pass.

Look side is a property of the mission, not a default
=====================================================
That check is the one this module used to fail. `los_unit` defaults to
left-looking, and every caller took the default, so Sentinel-1 was modelled
left-looking too - which is precisely the case the paragraph above says must
give E > 0, and it was giving E < 0.

The two missions differ and both are now carried on the Track:

    NISAR       LEFT-looking   (a mission choice, for full Antarctic coverage)
    Sentinel-1  RIGHT-looking  (ascending images east, descending images west)

It is not a footnote. At the 26 Aug 2026 failure point - a 27.4 deg slope
facing 273 deg - getting Sentinel-1's side wrong reverses the verdict on three
of the five tracks:

    track           wrong side   correct    verdict
    S1 ASC 85         -0.904      +0.188    usable -> BLIND
    NISAR ASC 98      -0.889      -0.889    unchanged
    S1 DESC 19        +0.198      -0.913    blind  -> USABLE
    S1 DESC 121       +0.198      -0.913    blind  -> USABLE
    NISAR DESC 48     +0.163      +0.163    unchanged

so the site has THREE usable tracks, not one look direction, and a genuine
ascending/descending pair that `decompose()` can separate into east and
vertical. Where a granule is at hand, prefer `los_from_product()` over any of
this - the product carries its own look vectors and settles the question
without reconstructing anything.

Usage
-----
    # Which tracks can even see motion on this slope? No data needed.
    python src/geometry_merge.py --sensitivity --lat 28.29 --lon 85.51

    # Merge the per-geometry series from timeseries.py
    python src/geometry_merge.py --merge --ts outputs/ts.csv \\
        --lat 28.29 --lon 85.51 --csv outputs/merged.csv --plot outputs/merged.png
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("geometry")


@dataclass(frozen=True)
class Track:
    name: str
    mission: str
    ascending: bool
    inclination_deg: float
    incidence_deg: float
    repeat_days: int
    # Which side the antenna points. NISAR looks LEFT; Sentinel-1 looks RIGHT.
    # There is deliberately no default: a look side that can be omitted is a
    # look side that gets omitted, and this module already made that mistake
    # once for every Sentinel-1 track. Adding a mission means stating it.
    left_looking: bool


# Tracks covering the Langtang / Lhende AOIs. Incidence angles are nominal
# mid-swath values - read the incidenceAngle layer from the GUNW for the real
# per-pixel number when you have the products.
# Below this slope the aspect - and therefore the sensitivity, which is a
# projection onto the downslope direction - is dominated by DEM noise rather
# than by terrain. Measured at 28.27484 N 85.47405 E: widening the SRTM stencil
# from 60 m to 300 m moves the aspect 74 degrees and takes the NISAR ascending
# sensitivity from +0.020 to -0.642, sign included. A 62-degree slope 3 km away
# holds its aspect to 4 degrees over the same range. Sensitivity on gentle
# ground is not a small number; it is an undetermined one.
MIN_SLOPE_DEG = 10.0

TRACKS = [
    Track("NISAR ASC 98", "NISAR", True, 98.4, 37.0, 12, left_looking=True),
    Track("NISAR DESC 48", "NISAR", False, 98.4, 37.0, 12, left_looking=True),
    Track("S1 ASC 85", "Sentinel-1", True, 98.18, 39.0, 12, left_looking=False),
    Track("S1 DESC 19", "Sentinel-1", False, 98.18, 39.0, 12, left_looking=False),
    Track("S1 DESC 121", "Sentinel-1", False, 98.18, 39.0, 12, left_looking=False),
]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def heading_deg(inclination_deg: float, lat_deg: float, ascending: bool) -> float:
    """
    Satellite heading (azimuth of flight, clockwise from North).

    sin(heading_asc) = cos(inclination) / cos(latitude)
    """
    ratio = math.cos(math.radians(inclination_deg)) / math.cos(math.radians(lat_deg))
    ratio = max(-1.0, min(1.0, ratio))
    asc = math.degrees(math.asin(ratio))           # negative for a retrograde orbit
    return asc % 360.0 if ascending else (180.0 - asc) % 360.0


def los_unit(heading: float, incidence: float, left_looking: bool) -> np.ndarray:
    """
    Unit vector from the ground target toward the satellite, in ENU.

    `left_looking` is REQUIRED and has no default. That is the whole point of
    this signature. It used to default to True, every caller took the default,
    and so Sentinel-1 - which looks RIGHT - was modelled as a left-looking
    sensor for the entire multi-geometry analysis.

    The failure is quiet in exactly the way that does most damage. Flipping the
    side reverses both horizontal components and leaves the vertical untouched,
    so an AOI median - dominated by the vertical term - stays perfectly
    plausible while every east-west inference, and every sensitivity on a
    steep slope, comes out backwards.

    Checked against the products' own losUnitVectorX/Y at 28.275 N:

        NISAR ASC 98   right-looking form   E -0.6261  N -0.1053  U +0.7726
                       product              E +0.6161  N +0.1531  U +0.7727

    which is the left-looking form, and confirms NISAR's stated look side.
    Sentinel-1 is the other way round: descending at 28 N has heading ~189 deg
    and must give E > 0, the satellite east of the target, because a descending
    right-looking pass images west.

    Prefer los_from_product() when a granule is at hand; this is the fallback
    for planning before anything is downloaded.
    """
    h, t = math.radians(heading), math.radians(incidence)
    side = -1.0 if left_looking else 1.0
    return np.array([side * -math.sin(t) * math.cos(h),
                     side * math.sin(t) * math.sin(h),
                     math.cos(t)])


def los_from_product(path, lat: float, lon: float) -> dict | None:
    """
    Read the look geometry the product itself carries, at one location.

    NISAR L2 ships losUnitVectorX/Y and incidenceAngle on a metadata cube, so
    the true geometry never has to be reconstructed from orbital elements at
    all. Z follows from the unit-length constraint, and the result is checked
    against cos(incidence) before being returned - if those disagree the
    convention is not what we think it is and guessing would be worse than
    failing.
    """
    try:
        import h5py
        from pyproj import Transformer
    except ImportError:
        logger.warning("h5py and pyproj are needed to read look geometry.")
        return None

    rg = "science/LSAR/GUNW/metadata/radarGrid"
    with h5py.File(path, "r") as f:
        if f"{rg}/losUnitVectorX" not in f:
            return None
        xs = np.asarray(f[f"{rg}/xCoordinates"][()])
        ys = np.asarray(f[f"{rg}/yCoordinates"][()])
        epsg = int(np.ravel(f[f"{rg}/projection"][()])[0]) if f"{rg}/projection" in f else 4326
        tx, ty = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
        j = int(np.argmin(np.abs(xs - tx)))
        i = int(np.argmin(np.abs(ys - ty)))

        def col(name):
            a = np.asarray(f[f"{rg}/{name}"][:, i, j], dtype=float)
            a = a[np.isfinite(a)]
            return float(a.mean()) if a.size else float("nan")

        lx, ly, inc = col("losUnitVectorX"), col("losUnitVectorY"), col("incidenceAngle")
        look = f["science/LSAR/identification/lookDirection"][()]
        look = look.decode() if isinstance(look, bytes) else str(look)

    if not all(np.isfinite([lx, ly, inc])):
        return None
    lz = math.sqrt(max(0.0, 1.0 - lx * lx - ly * ly))
    if abs(lz - math.cos(math.radians(inc))) > 0.02:
        logger.warning("losUnitVector Z (%.4f) disagrees with cos(incidence) "
                       "(%.4f) - convention unclear, not using it.",
                       lz, math.cos(math.radians(inc)))
        return None
    return {"los": np.array([lx, ly, lz]), "incidence": inc, "look": look}


def decompose(d_asc: float, d_desc: float, los_asc: np.ndarray,
              los_desc: np.ndarray) -> dict:
    """
    Two line-of-sight rates into east and vertical, assuming no north motion.

    Both geometries are nearly blind to north - the N components here are
    +0.15 and +0.18 against E of +0.62 and -0.59 - so solving for it would
    divide by almost nothing. Setting u_N = 0 is the standard reduction and
    the honest one.

    The reason to bother is that a path delay is not a vector. A delay changes
    both geometries by the same LOS amount, and because U is the largest
    component of both look vectors (0.77 and 0.79) it decomposes to almost
    pure vertical. So the vertical fraction is a diagnostic: a slope creeps
    parallel to its own surface, and a plunge steeper than the terrain is a
    delay wearing a vector's clothes.
    """
    M = np.array([[los_asc[0], los_asc[2]], [los_desc[0], los_desc[2]]])
    if abs(np.linalg.det(M)) < 1e-6:
        raise ValueError("The two geometries are too similar to separate.")
    u_e, u_u = np.linalg.solve(M, [d_asc, d_desc])
    mag = float(math.hypot(u_e, u_u))
    return {"east": float(u_e), "up": float(u_u), "magnitude": mag,
            "plunge_deg": float(math.degrees(math.atan2(-u_u, abs(u_e)))) if mag else 0.0,
            "vertical_fraction": float(abs(u_u) / mag) if mag else 0.0}


def downslope_unit(slope_deg: float, aspect_deg: float) -> np.ndarray:
    """
    Unit vector pointing down the slope, in ENU.

    slope 0 deg  -> horizontal, along aspect
    slope 90 deg -> straight down
    """
    b, a = math.radians(slope_deg), math.radians(aspect_deg)
    return np.array([math.cos(b) * math.sin(a),
                     math.cos(b) * math.cos(a),
                     -math.sin(b)])


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------
def elevation_grid(lats, lons, batch: int = 100):
    """
    One elevation raster for the whole AOI, in batched requests.

    slope_aspect() costs a request per point, which is fine for one location
    and hopeless for a map. OpenTopoData takes 100 locations per call, so a
    40x36 grid is fifteen requests instead of fourteen hundred.
    """
    pts = [(a, b) for a in lats for b in lons]
    out = []
    for k in range(0, len(pts), batch):
        chunk = pts[k:k + batch]
        loc = "|".join(f"{a:.6f},{b:.6f}" for a, b in chunk)
        url = "https://api.opentopodata.org/v1/srtm30m?locations=" + urllib.parse.quote(loc)
        for attempt in range(4):
            try:
                d = json.loads(urllib.request.urlopen(url, timeout=120).read().decode())
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                logger.warning("retry (%s)", exc)
                time.sleep(4)
        out += [r["elevation"] if r["elevation"] is not None else np.nan
                for r in d["results"]]
        time.sleep(1.1)                      # the public endpoint allows 1/sec
        logger.info("elevation %d/%d", min(k + batch, len(pts)), len(pts))
    return np.array(out, dtype=float).reshape(len(lats), len(lons))


def slope_aspect_grid(z: np.ndarray, lats, lons):
    """Horn's method across a whole grid. Row 0 is north."""
    dy = abs(lats[1] - lats[0]) * 110570.0
    dx = abs(lons[1] - lons[0]) * 111320.0 * math.cos(math.radians(float(np.mean(lats))))
    zp = np.pad(z, 1, mode="edge")
    dzdx = ((zp[:-2, 2:] + 2 * zp[1:-1, 2:] + zp[2:, 2:])
            - (zp[:-2, :-2] + 2 * zp[1:-1, :-2] + zp[2:, :-2])) / (8 * dx)
    dzdy = ((zp[:-2, :-2] + 2 * zp[:-2, 1:-1] + zp[:-2, 2:])
            - (zp[2:, :-2] + 2 * zp[2:, 1:-1] + zp[2:, 2:])) / (8 * dy)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    aspect = np.degrees(np.arctan2(-dzdx, -dzdy)) % 360.0
    return slope, aspect


def slope_aspect(lat: float, lon: float, spacing_m: float = 90.0) -> tuple[float, float, float]:
    """Slope, aspect and elevation at a point, from SRTM via OpenTopoData."""
    dlat = spacing_m / 110570.0
    dlon = spacing_m / (111320.0 * math.cos(math.radians(lat)))
    pts = [(lat + i * dlat, lon + j * dlon) for i in (1, 0, -1) for j in (-1, 0, 1)]
    loc = "|".join(f"{a:.6f},{b:.6f}" for a, b in pts)
    url = "https://api.opentopodata.org/v1/srtm30m?locations=" + urllib.parse.quote(loc)
    for attempt in range(4):
        try:
            d = json.loads(urllib.request.urlopen(url, timeout=90).read().decode())
            break
        except Exception as exc:
            if attempt == 3:
                raise
            logger.warning("retry (%s)", exc)
            time.sleep(3)
    z = np.array([r["elevation"] for r in d["results"]], dtype=float).reshape(3, 3)

    # Horn's method. Row 0 is north, so dz/dy uses (north - south).
    dzdx = ((z[0, 2] + 2 * z[1, 2] + z[2, 2]) - (z[0, 0] + 2 * z[1, 0] + z[2, 0])) / (8 * spacing_m)
    dzdy = ((z[0, 0] + 2 * z[0, 1] + z[0, 2]) - (z[2, 0] + 2 * z[2, 1] + z[2, 2])) / (8 * spacing_m)
    slope = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
    aspect = (math.degrees(math.atan2(-dzdx, -dzdy))) % 360.0   # azimuth of steepest DESCENT
    return slope, aspect, float(z[1, 1])


# ---------------------------------------------------------------------------
def sensitivities(lat: float, lon: float, slope: float, aspect: float,
                  min_sensitivity: float) -> list[dict]:
    s_hat = downslope_unit(slope, aspect)
    rows = []
    for tr in TRACKS:
        h = heading_deg(tr.inclination_deg, lat, tr.ascending)
        l_hat = los_unit(h, tr.incidence_deg, tr.left_looking)
        sens = float(np.dot(s_hat, l_hat))
        rows.append({
            "track": tr.name, "ascending": tr.ascending,
            "heading_deg": h, "incidence_deg": tr.incidence_deg,
            "sensitivity": sens,
            "amplification": abs(1.0 / sens) if abs(sens) > 1e-6 else float("inf"),
            "usable": abs(sens) >= min_sensitivity,
            "repeat_days": tr.repeat_days,
        })
    return rows


def report_sensitivity(lat, lon, slope, aspect, elev, rows, min_sensitivity):
    print(f"\nPoint {lat:.4f} N, {lon:.4f} E   elevation {elev:.0f} m")
    print(f"Slope {slope:.1f} deg, aspect {aspect:.0f} deg "
          f"({compass(aspect)}-facing) - from SRTM at 90 m spacing")
    if slope < MIN_SLOPE_DEG:
        print(f"\n  *** SENSITIVITY NOT DETERMINED AT THIS POINT ***")
        print(f"  The slope is {slope:.1f} deg, below {MIN_SLOPE_DEG:.0f} deg. Aspect on")
        print(f"  ground this gentle is set by DEM noise, not by terrain, so every")
        print(f"  number below inherits that. Run --stencil-sweep to see the spread")
        print(f"  before quoting any of them, and do not report three decimals.")

    print(f"\nAssuming pure downslope motion. Sensitivity = slope_hat . los_hat;")
    print(f"a track needs |sensitivity| >= {min_sensitivity} to be usable.\n")
    print(f"{'TRACK':<16}{'HEADING':>9}{'INCID':>7}{'SENSITIVITY':>13}{'NOISE x':>9}  VERDICT")
    print("-" * 68)
    for r in sorted(rows, key=lambda x: -abs(x["sensitivity"])):
        amp = f"{r['amplification']:.1f}" if math.isfinite(r["amplification"]) else "inf"
        verdict = "usable" if r["usable"] else "BLIND - reject"
        print(f"{r['track']:<16}{r['heading_deg']:>8.1f}d{r['incidence_deg']:>6.0f}d"
              f"{r['sensitivity']:>13.3f}{amp:>9}  {verdict}")

    usable = [r for r in rows if r["usable"]]
    print(f"\n  {len(usable)} of {len(rows)} tracks usable on this slope.")
    if usable:
        eff = harmonic_revisit([r["repeat_days"] for r in usable])
        print(f"  Combined sampling from the usable tracks: ~{eff:.1f} days "
              f"(vs {min(r['repeat_days'] for r in usable)} d for any one alone).")
    signs = {r["sensitivity"] > 0 for r in usable}
    if len(signs) > 1:
        print("  Ascending and descending have OPPOSITE sign here - a genuine")
        print("  cross-check: real downslope motion must appear with opposite")
        print("  LOS sign in the two geometries. Atmosphere will not do that.")
    else:
        print("  All usable tracks share the same sign - no sign-based cross-check")
        print("  available at this point.")


def stencil_sweep(lat: float, lon: float, min_sensitivity: float,
                  spacings=(60.0, 90.0, 150.0, 210.0, 300.0)) -> None:
    """
    Sensitivity across DEM stencil widths.

    A point estimate hides how much of the answer is the choice of scale. On
    steep ground the spread is small and the value is real; on gentle ground the
    sign itself moves. Report the range, not the point.
    """
    print(f"\nSENSITIVITY ACROSS DEM STENCIL WIDTHS at {lat:.5f} N {lon:.5f} E")
    print(f"{'stencil':>9}{'slope':>8}{'aspect':>9}", end="")
    names = [t.name for t in TRACKS]
    for n in names:
        print(f"{n:>16}", end="")
    print()
    got = {n: [] for n in names}
    slopes = []
    for sp in spacings:
        try:
            sl, asp, _ = slope_aspect(lat, lon, spacing_m=sp)
        except Exception as exc:
            print(f"{sp:>8.0f}m   failed: {exc}")
            continue
        slopes.append(sl)
        rows = {r["track"]: r["sensitivity"]
                for r in sensitivities(lat, lon, sl, asp, min_sensitivity)}
        print(f"{sp:>8.0f}m{sl:>8.1f}{asp:>9.0f}", end="")
        for n in names:
            got[n].append(rows[n])
            print(f"{rows[n]:>16.3f}", end="")
        print()
    if not slopes:
        return
    print(f"\n{'':>26}", end="")
    for n in names:
        v = got[n]
        print(f"{min(v):+.2f}..{max(v):+.2f}".rjust(16), end="")
    print("   <- range")
    unstable = [n for n in names if got[n] and min(got[n]) * max(got[n]) < 0]
    print(f"\n  slope over the sweep: {min(slopes):.1f} to {max(slopes):.1f} deg")
    if unstable:
        print(f"  SIGN IS NOT STABLE for: {', '.join(unstable)}")
        print("  A track whose sensitivity changes sign with the stencil cannot")
        print("  support a geometry cross-check, and its downslope magnitudes")
        print("  (which divide by sensitivity) are not meaningful either.")
    elif max(slopes) < MIN_SLOPE_DEG:
        print(f"  All below {MIN_SLOPE_DEG:.0f} deg - treat every value as indicative only.")
    else:
        print("  Sign is stable across the sweep. Quote the range, not a point value.")


def compass(azimuth: float) -> str:
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((azimuth % 360) / 22.5 + 0.5) % 16]


def harmonic_revisit(repeats: list[int]) -> float:
    """Mean gap when several independent tracks are interleaved."""
    return 1.0 / sum(1.0 / r for r in repeats)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
def load_timeseries(path: Path) -> dict[str, list[dict]]:
    series: dict[str, list[dict]] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            key = f"{row['geometry']}|{row['component']}"
            series.setdefault(key, []).append({
                "epoch": datetime.strptime(row["epoch"], "%Y-%m-%d").date(),
                "cumulative_mm": float(row["cumulative_mm"]),
                "error_mm": float(row["error_mm"]) if row.get("error_mm") else None,
            })
    for v in series.values():
        v.sort(key=lambda r: r["epoch"])
    return series


def match_track(geometry_label: str) -> Track | None:
    """Map a timeseries.py label like 'ASC path 98' onto a Track."""
    lab = geometry_label.upper().replace("PATH", "").split()
    asc = lab[0].startswith("ASC")
    num = next((t for t in lab if t.isdigit()), None)
    for tr in TRACKS:
        if tr.ascending == asc and num and num in tr.name:
            return tr
    return None


def merge(ts_path: Path, lat: float, lon: float, min_sensitivity: float):
    slope, aspect, elev = slope_aspect(lat, lon)
    s_hat = downslope_unit(slope, aspect)
    series = load_timeseries(ts_path)

    print(f"\nProjecting onto downslope at {lat:.4f} N {lon:.4f} E "
          f"(slope {slope:.1f} deg, aspect {aspect:.0f} deg / {compass(aspect)})\n")

    merged: list[dict] = []
    for key, rows in sorted(series.items()):
        geom, comp = key.split("|")
        tr = match_track(geom)
        if tr is None:
            logger.warning("No geometry known for '%s' - skipped", geom)
            continue
        h = heading_deg(tr.inclination_deg, lat, tr.ascending)
        sens = float(np.dot(s_hat, los_unit(h, tr.incidence_deg, tr.left_looking)))
        if abs(sens) < min_sensitivity:
            print(f"  {geom} block {comp}: sensitivity {sens:+.3f} - BLIND, rejected")
            continue
        print(f"  {geom} block {comp}: sensitivity {sens:+.3f}, "
              f"noise amplified x{abs(1/sens):.1f}, {len(rows)} epochs")
        for r in rows:
            merged.append({
                "epoch": r["epoch"], "geometry": geom, "component": int(comp),
                "los_mm": r["cumulative_mm"],
                "downslope_mm": r["cumulative_mm"] / sens,
                "sensitivity": sens,
                # Inverse-variance weight. d_downslope = d_los / s, so its
                # variance scales as sigma^2 / s^2 and the optimal weight is
                # s^2, not |s|. Weighting by |s| under-weights the geometry
                # that actually sees the motion.
                "weight": sens ** 2,
                "error_mm": (abs(r["error_mm"] / sens) if r["error_mm"] else None),
            })

    merged.sort(key=lambda r: r["epoch"])
    if not merged:
        print("\nNothing usable - every geometry was blind to motion on this slope.")
        return []

    epochs = [r["epoch"] for r in merged]
    gaps = [(epochs[i + 1] - epochs[i]).days for i in range(len(epochs) - 1)]
    gaps = [g for g in gaps if g > 0]

    print(f"\n{'EPOCH':<12}{'GEOMETRY':<14}{'BLK':>4}{'LOS mm':>10}{'DOWNSLOPE mm':>14}{'SENS':>8}")
    print("-" * 64)
    for r in merged:
        print(f"{str(r['epoch']):<12}{r['geometry']:<14}{r['component']:>4}"
              f"{r['los_mm']:>10.2f}{r['downslope_mm']:>14.2f}{r['sensitivity']:>8.3f}")

    if gaps:
        print(f"\n  merged sampling: {len(merged)} epochs, "
              f"median gap {float(np.median(gaps)):.1f} d, min {min(gaps)} d, max {max(gaps)} d")
    print("\n  NOTE: blocks keep their own zero. Points from different components")
    print("  or different geometries are NOT on a common datum - the merged record")
    print("  is denser in TIME, but only comparable within a block.")
    return merged


def plot_merged(rows, out: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.error("matplotlib required for --plot")
        return
    fig, ax = plt.subplots(figsize=(10, 6))
    by_geom: dict[str, list] = {}
    for r in rows:
        by_geom.setdefault(f"{r['geometry']} blk{r['component']}", []).append(r)
    for label, rs in sorted(by_geom.items()):
        ax.plot([datetime.combine(r["epoch"], datetime.min.time()) for r in rs],
                [r["downslope_mm"] for r in rs], "o-", label=label, alpha=.85)
    ax.axvline(datetime(2026, 8, 26), color="crimson", ls="--", lw=1.2)
    ax.set_ylabel("Downslope displacement (mm)")
    ax.set_title("Multi-geometry merge: LOS projected onto slope direction\n"
                 "each block keeps its own zero", fontsize=11)
    ax.grid(alpha=.3); ax.legend(fontsize=9)
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out, dpi=140)
    logger.info("Wrote %s", out)


def coverage_map(aoi: str, step: float, min_sens: float, out_csv=None) -> int:
    """
    How much of an AOI could each geometry actually have seen?

    A non-detection is only as strong as the ground it covers. Quoting one
    sensitivity at one point says nothing about a valley whose aspect swings
    through every direction, and this is the number that qualifies the null:
    motion is invisible wherever the slope happens to lie across the look
    direction, however good the interferogram is.

    It also separates two different questions. One usable geometry gives a
    line-of-sight rate and nothing more; only where BOTH are usable can
    vertical be told from horizontal, and that is a much smaller area.
    """
    import gunw_reader as _g
    ring = _g.AOIS[aoi]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    LO = np.arange(min(lons), max(lons) + 1e-9, step)
    LA = np.arange(max(lats), min(lats) - 1e-9, -step)
    logger.info("%s: %d x %d grid at ~%.0f m", aoi, len(LA), len(LO), step * 111000)

    z = elevation_grid(LA, LO)
    slope, aspect = slope_aspect_grid(z, LA, LO)
    inside = np.array([[_g.point_in_ring(b, a, ring) for b in LO] for a in LA])
    ok = inside & np.isfinite(z)

    print(f"\n{'=' * 68}")
    print(f"{aoi.upper()}   {int(ok.sum())} cells inside the AOI")
    print("=" * 68)
    print(f"  elevation      {np.nanmin(z[ok]):.0f} - {np.nanmax(z[ok]):.0f} m")
    print(f"  slope          median {np.median(slope[ok]):.1f} deg, "
          f"{100 * np.mean(slope[ok] > 20):.0f}% steeper than 20 deg")

    # Take the geometry from TRACKS rather than restating it. This block used
    # to carry its own incidence angles (39.4 and 37.7 against the 37.0 in
    # TRACKS) and its own implicit look side, so the map and the point report
    # could disagree about the same satellite.
    sens = {}
    for tr in [t for t in TRACKS if t.mission == "NISAR"]:
        label = tr.name
        h = heading_deg(tr.inclination_deg, float(np.mean(LA)), tr.ascending)
        L = los_unit(h, tr.incidence_deg, tr.left_looking)
        S = np.zeros_like(slope)
        for i in range(slope.shape[0]):
            for j in range(slope.shape[1]):
                if ok[i, j]:
                    S[i, j] = float(np.dot(downslope_unit(slope[i, j], aspect[i, j]), L))
        sens[label] = S
        steep = ok & (slope > 20)
        print(f"  {label:<15} usable over {100 * (np.abs(S[ok]) >= min_sens).mean():5.1f}% "
              f"of the AOI, {100 * (np.abs(S[steep]) >= min_sens).mean():5.1f}% of "
              f"slopes >20 deg")

    A, D = sens["NISAR ASC 98"], sens["NISAR DESC 48"]
    either = (np.abs(A) >= min_sens) | (np.abs(D) >= min_sens)
    both = (np.abs(A) >= min_sens) & (np.abs(D) >= min_sens)
    print(f"\n  EITHER geometry usable   {100 * either[ok].mean():5.1f}%  "
          f"<- the area a non-detection actually covers")
    print(f"  BOTH usable              {100 * both[ok].mean():5.1f}%  "
          f"<- the only area where vertical can be separated from horizontal")
    print(f"  NEITHER                  {100 * (~either[ok]).mean():5.1f}%  "
          f"<- blind, whatever the interferogram says")

    if out_csv:
        with open(out_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["lat", "lon", "elev_m", "slope_deg", "aspect_deg",
                        "sens_asc", "sens_desc"])
            for i, a in enumerate(LA):
                for j, b in enumerate(LO):
                    if ok[i, j]:
                        w.writerow([f"{a:.5f}", f"{b:.5f}", f"{z[i,j]:.0f}",
                                    f"{slope[i,j]:.1f}", f"{aspect[i,j]:.0f}",
                                    f"{A[i,j]:.3f}", f"{D[i,j]:.3f}"])
        logger.info("Wrote %s", out_csv)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge ascending and descending LOS series")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sensitivity", action="store_true",
                      help="which tracks can see motion here (needs no data)")
    mode.add_argument("--merge", action="store_true", help="merge a timeseries.py CSV")
    mode.add_argument("--map", metavar="AOI", choices=("source", "langtang", "lhende"),
                      help="what fraction of an AOI each geometry can actually see. "
                           "'source' is the analysis AOI and was missing from this "
                           "list, which is why the coverage of the null was never "
                           "computed over the polygon the null is about")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--step", type=float, default=0.003,
                    help="map grid spacing in degrees (0.003 ~ 333 m)")
    ap.add_argument("--ts", metavar="TS.csv", help="output of timeseries.py --csv")
    ap.add_argument("--stencil-sweep", action="store_true",
                    help="report sensitivity across DEM stencil widths rather "
                         "than at one scale. Slower (one SRTM query per width) "
                         "and the honest way to quote it")
    ap.add_argument("--aspect", type=float, default=None,
                    help="override the SRTM aspect, in degrees azimuth of "
                         "steepest descent. Use it to test the north-face "
                         "hypothesis against the west-facing DEM pixel")
    ap.add_argument("--slope", type=float, default=None,
                    help="override the SRTM slope, in degrees")
    ap.add_argument("--min-sensitivity", type=float, default=0.3,
                    help="reject tracks below this |slope_hat . los_hat|")
    ap.add_argument("--csv", metavar="OUT.csv")
    ap.add_argument("--plot", metavar="OUT.png")
    args = ap.parse_args()

    if args.map:
        return coverage_map(args.map, args.step, args.min_sensitivity, args.csv)

    if args.sensitivity:
        if args.lat is None or args.lon is None:
            ap.error("--sensitivity needs --lat and --lon")
        slope, aspect, elev = slope_aspect(args.lat, args.lon)
        # The aspect at this point is the single largest open uncertainty in
        # the whole analysis: SRTM reads west-facing at the estimated failure
        # pixel, while the published accounts put the scar on the NORTH face of
        # Langtang Lirung. Every sensitivity below is a dot product with the
        # downslope vector, so aspect decides which tracks are called blind -
        # and until these flags existed the alternative could be stated but not
        # tested. Overriding is not cheating; refusing to check is.
        if args.aspect is not None:
            print(f"[OVERRIDE] aspect {aspect:.0f} deg from SRTM -> "
                  f"{args.aspect:.0f} deg as given")
            aspect = args.aspect
        if args.slope is not None:
            print(f"[OVERRIDE] slope {slope:.1f} deg from SRTM -> "
                  f"{args.slope:.1f} deg as given")
            slope = args.slope
        rows = sensitivities(args.lat, args.lon, slope, aspect, args.min_sensitivity)
        report_sensitivity(args.lat, args.lon, slope, aspect, elev, rows, args.min_sensitivity)
        if args.stencil_sweep:
            stencil_sweep(args.lat, args.lon, args.min_sensitivity)
        if args.csv:
            with open(args.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader(); w.writerows(rows)
            logger.info("Wrote %s", args.csv)
        return 0

    if not args.ts:
        ap.error("--merge needs --ts")
    rows = merge(Path(args.ts), args.lat, args.lon, args.min_sensitivity)
    if rows and args.csv:
        keys = ["epoch", "geometry", "component", "los_mm", "downslope_mm",
                "sensitivity", "weight", "error_mm"]
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "epoch": str(r["epoch"])})
        logger.info("Wrote %s", args.csv)
    if rows and args.plot:
        plot_merged(rows, Path(args.plot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
