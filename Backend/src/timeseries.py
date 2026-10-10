"""
timeseries.py
-------------
Assemble NISAR GUNW pairs into a cumulative LOS displacement time series.

Each interferogram measures a *difference*:

    phi(i,j)  =  d(t_j) - d(t_i)

Given a network of pairs, invert for d(t) at every acquisition epoch. This is
the small-baseline (SBAS) idea, reduced to what NISAR L2 actually needs -
the products are already unwrapped and geocoded, so all that is left is the
temporal inversion.

Three things this tool refuses to fudge
=======================================
1. It never mixes orbit geometries. Ascending path 98 and descending path 48
   measure different components of the same 3-D motion. They are inverted
   separately and reported separately.

2. It never bridges a disconnected network. If no chain of interferograms links
   two blocks of epochs, their displacements are not comparable and no amount
   of least squares will make them so. Each connected component gets its own
   series with its own zero.

3. It reports redundancy honestly. A component with as many pairs as unknowns
   has an exact solution and therefore no residual and no error estimate. That
   is not a good fit - it is no fit at all.

Usage
-----
    # Network structure straight from the catalogue, before downloading
    python src/timeseries.py --from-catalogue

    # Network structure of what you already have
    python src/timeseries.py --dir data/nisar_l2 --network

    # Region-mean inversion (fast, robust - start here)
    python src/timeseries.py --dir data/nisar_l2 --invert \
        --ref-lat 28.21 --ref-lon 85.47 --csv outputs/ts.csv --plot outputs/ts.png

Requires: numpy.  --dir modes also need h5py (via gunw_reader).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("timeseries")

from paths import NISAR, resolve  # noqa: E402

EVENT_DATE = date(2026, 8, 26)


# ---------------------------------------------------------------------------
# Pair inventory
# ---------------------------------------------------------------------------
class Pair:
    """One interferogram: a measured difference between two epochs."""

    __slots__ = ("path", "direction", "ref", "sec", "value", "n_px", "coherence", "source")

    def __init__(self, path, direction, ref, sec, value=None, n_px=None, coherence=None, source=""):
        self.path = path
        self.direction = direction
        self.ref = ref
        self.sec = sec
        self.value = value          # mm, region mean, filled by --invert
        self.n_px = n_px
        self.coherence = coherence
        self.source = source

    @property
    def span_days(self) -> int:
        return (self.sec - self.ref).days

    def __repr__(self):
        return f"<{self.direction}{self.path} {self.ref}->{self.sec}>"


def pairs_from_granule_names(names: list[str], paths: list, directions: list) -> list[Pair]:
    out = []
    for name, path, direction in zip(names, paths, directions):
        stamps = re.findall(r"_(\d{8})T\d{6}", name)
        if len(stamps) < 4:
            continue
        out.append(
            Pair(
                path=path,
                direction=(direction or "?")[0],
                ref=datetime.strptime(stamps[0], "%Y%m%d").date(),
                sec=datetime.strptime(stamps[2], "%Y%m%d").date(),
                source=name,
            )
        )
    return out


def pairs_from_catalogue() -> list[Pair]:
    """Query ASF for GUNW pairs over the AOI without downloading anything."""
    import nisar_acquisition as acq

    rows = acq.query_by_year("2025-01-01", date.today().isoformat(),
                             platform="NISAR", processingLevel="GUNW")
    return pairs_from_granule_names(
        [r["granuleName"] for r in rows],
        [r.get("path") for r in rows],
        [r.get("flightDirection") for r in rows],
    )


def pairs_from_stats(path: Path, fringe_mm: float = 121.95,
                     layer: str | None = None) -> list[Pair]:
    """
    Build the network from a gunw_reader --csv summary instead of the products.

    A GUNW is 2.4 GB; the row describing it is 200 bytes. Once the AOI median
    has been measured there is nothing else in the file the inversion needs, so
    a teammate with the disk space can process the stack and send back a CSV
    that reproduces the whole time series. That is the difference between a
    pipeline one machine can run and one a team can.

    Duplicate acquisitions
    ----------------------
    NASA ships a routine (PR) and an urgent (UR) product for the same images.
    Both are valid interferograms; they can differ by a whole number of fringes
    because unwrapping is only unique up to a cycle. Averaging them is
    meaningless. Instead, keep the branch consistent with the rest of that
    geometry - the surrounding pairs already say what magnitude is plausible,
    which is temporal unwrapping applied to the ambiguity.
    """
    rows = list(csv.DictReader(open(path, newline="")))
    if not rows:
        logger.error("No rows in %s", path)
        return []

    # GOFF writes one row per product PER LAYER, so without a filter every pair
    # arrives three times and the duplicate resolver reports three products for
    # a single acquisition pair. Layers are different measurements at different
    # correlation window sizes, not duplicates, so pick one.
    if layer and rows and "layer" in rows[0]:
        before = len(rows)
        rows = [r for r in rows if layer in (r.get("layer") or "")]
        logger.info("Layer filter '%s': %d of %d rows", layer, len(rows), before)
        if not rows:
            logger.error("No rows match layer '%s'", layer)
            return []

    # The two readers write different column names for the same quantity, and
    # this used to read only the GUNW one - so --from-stats with --product GOFF
    # died on KeyError: 'median' rather than saying what was wrong.
    #
    # The fringe reasoning below applies to phase only. Pixel offsets are not
    # wrapped, so a PR/UR disagreement in GOFF is a processing difference and
    # not a cycle ambiguity; quoting it in fringes would invent a mechanism.
    head = rows[0]
    if "median" in head:
        value_col, coh_col, wrapped = "median", "mean_coherence", True
    elif "range_median_mm" in head:
        value_col, coh_col, wrapped = "range_median_mm", "mean_correlation", False
    else:
        logger.error("%s has neither 'median' (GUNW) nor 'range_median_mm' (GOFF). "
                     "Columns present: %s", path, ", ".join(sorted(head)))
        return []
    logger.info("Reading displacement from column '%s'", value_col)

    parsed = []
    for r in rows:
        name = r.get("file", "")
        m = re.search(r"NISAR_L2_([A-Z]{2})_[A-Z]{4}_\d+_(\d+)_([AD])_", name)
        stamps = re.findall(r"_(\d{8})T\d{6}", name)
        if not m or len(stamps) < 4:
            logger.warning("Cannot parse %s", name or "<blank>")
            continue
        parsed.append((m.group(1), Pair(
            path=int(m.group(2)), direction=m.group(3),
            ref=datetime.strptime(stamps[0], "%Y%m%d").date(),
            sec=datetime.strptime(stamps[2], "%Y%m%d").date(),
            value=float(r[value_col]), n_px=int(float(r.get("valid_px") or 0)),
            coherence=float(r[coh_col]) if r.get(coh_col) else None,
            source=name)))

    groups: dict[tuple, list] = defaultdict(list)
    for proc, p in parsed:
        groups[(p.direction, p.path, p.ref, p.sec)].append((proc, p))

    out, dropped = [], []
    for key, cands in groups.items():
        if len(cands) == 1:
            out.append(cands[0][1])
            continue
        # Scale expected from every OTHER pair in the same geometry.
        others = [q.value for k, c in groups.items() for _, q in c
                  if k[:2] == key[:2] and k != key and q.value is not None]
        expect = float(np.median(np.abs(others))) if others else 0.0
        best = min(cands, key=lambda pc: (abs(abs(pc[1].value) - expect),
                                          0 if pc[0] == "PR" else 1))
        for proc, p in cands:
            if p is not best[1]:
                gap = abs(p.value - best[1].value)
                dropped.append((key, proc, p.value, gap, gap / fringe_mm))
        logger.warning("%s -> %s: %d products, keeping %s (%+.1f mm)",
                       key[2], key[3], len(cands), best[0], best[1].value)
        out.append(best[1])

    for key, proc, val, gap, fr in dropped:
        if wrapped:
            logger.warning("  dropped %s (%+.1f mm), %.1f mm from the kept value "
                           "= %.2f fringes", proc, val, gap, fr)
        else:
            logger.warning("  dropped %s (%+.1f mm), %.1f mm from the kept value "
                           "(offsets are not wrapped - this is a processing "
                           "difference, not a cycle)", proc, val, gap)

    logger.info("Loaded %d pairs from %s (%d duplicate product(s) dropped)",
                len(out), path.name, len(dropped))
    return out


def pairs_from_dir(directory: Path, product: str = "GUNW") -> list[Pair]:
    """
    Collect pairs from a folder tree. product is GUNW or GOFF.

    Both are interferometric pairs with the same naming, so the network logic
    is identical - only the reader differs. Searching recursively so the
    organised layout (GUNW/2025-11_winter/...) works without extra flags.
    """
    product = product.upper()
    files = sorted(p for p in directory.rglob("*.h5") if product in p.name.upper())
    if not files:
        logger.error("No *%s*.h5 under %s", product, directory)
        return []
    out = []
    for fp in files:
        stamps = re.findall(r"_(\d{8})T\d{6}", fp.name)
        m = re.search(rf"_{product}_\d+_(\d+)_([AD])_", fp.name)
        if len(stamps) < 4 or not m:
            logger.warning("Cannot parse %s", fp.name)
            continue
        out.append(
            Pair(
                path=int(m.group(1)),
                direction=m.group(2),
                ref=datetime.strptime(stamps[0], "%Y%m%d").date(),
                sec=datetime.strptime(stamps[2], "%Y%m%d").date(),
                source=str(fp),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Network structure
# ---------------------------------------------------------------------------
def connected_components(pairs: list[Pair]) -> list[list[date]]:
    """Group epochs into blocks linked by interferograms (union-find)."""
    parent: dict[date, date] = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for p in pairs:
        union(p.ref, p.sec)

    groups: dict[date, list[date]] = defaultdict(list)
    for epoch in parent:
        groups[find(epoch)].append(epoch)
    return sorted((sorted(v) for v in groups.values()), key=lambda g: g[0])


def describe_network(pairs: list[Pair]) -> dict:
    by_geom: dict[tuple, list[Pair]] = defaultdict(list)
    for p in pairs:
        by_geom[(p.direction, p.path)].append(p)

    print(f"\n{len(pairs)} interferograms across {len(by_geom)} geometries\n")
    summary = {}

    for (direction, path), group in sorted(by_geom.items()):
        comps = connected_components(group)
        epochs = sorted({e for p in group for e in (p.ref, p.sec)})
        label = f"{'ASCENDING' if direction == 'A' else 'DESCENDING'} path {path}"
        print("=" * 68)
        print(f"{label}   {len(group)} pairs, {len(epochs)} epochs, "
              f"{len(comps)} connected component(s)")
        print("=" * 68)

        if len(comps) > 1:
            print("  !! NETWORK IS DISCONNECTED - these blocks cannot be joined.")
            print("     Displacements in one block are NOT comparable to another.")

        comp_info = []
        for k, comp in enumerate(comps, 1):
            inside = [p for p in group if p.ref in comp and p.sec in comp]
            unknowns = len(comp) - 1
            redundancy = len(inside) - unknowns
            print(f"\n  Component {k}: {comp[0]} .. {comp[-1]}  "
                  f"({(comp[-1]-comp[0]).days} d, {len(comp)} epochs, {len(inside)} pairs)")
            print(f"    unknowns {unknowns}, redundancy {redundancy}", end="")
            if redundancy == 0:
                print("  <- exactly determined: no residual, no error estimate")
            elif redundancy < 0:
                print("  <- UNDER-DETERMINED")
            else:
                print()
            for p in sorted(inside, key=lambda x: x.sec):
                flag = "  <- last before event" if p.sec <= EVENT_DATE and \
                    p.sec == max(q.sec for q in inside if q.sec <= EVENT_DATE) else ""
                print(f"      {p.ref} -> {p.sec}  ({p.span_days:>2} d){flag}")
            comp_info.append({"epochs": [str(e) for e in comp], "n_pairs": len(inside),
                              "redundancy": redundancy})
        summary[label] = comp_info
        print()
    return summary


# ---------------------------------------------------------------------------
# Inversion
# ---------------------------------------------------------------------------
def invert_component(pairs: list[Pair], epochs: list[date]) -> dict:
    """
    Least-squares solve for cumulative displacement at each epoch.

    d(epochs[0]) is fixed at 0 - every series is relative to its own first
    acquisition, which is why disconnected components can never be compared.
    """
    epochs = sorted(epochs)
    index = {e: i for i, e in enumerate(epochs)}
    n_unknown = len(epochs) - 1

    usable = [p for p in pairs if p.value is not None]
    if not usable:
        return {"error": "no pairs carry a measured value"}
    if n_unknown == 0:
        return {"error": "single epoch"}

    G = np.zeros((len(usable), n_unknown))
    obs = np.zeros(len(usable))
    for r, p in enumerate(usable):
        i, j = index[p.ref], index[p.sec]
        if i > 0:
            G[r, i - 1] = -1.0
        if j > 0:
            G[r, j - 1] = +1.0
        obs[r] = p.value

    solution, *_ = np.linalg.lstsq(G, obs, rcond=None)
    cumulative = np.concatenate([[0.0], solution])
    residuals = G @ solution - obs

    dof = len(usable) - n_unknown
    if dof > 0:
        sigma = float(np.sqrt(np.sum(residuals ** 2) / dof))
        try:
            cov = sigma ** 2 * np.linalg.inv(G.T @ G)
            errors = np.concatenate([[0.0], np.sqrt(np.diag(cov))])
        except np.linalg.LinAlgError:
            errors = np.full(len(epochs), np.nan)
    else:
        sigma = float("nan")
        errors = np.full(len(epochs), np.nan)

    # linear velocity over the component
    days = np.array([(e - epochs[0]).days for e in epochs], dtype=float)
    velocity = velocity_err = float("nan")
    if len(epochs) >= 3:
        A = np.vstack([days, np.ones_like(days)]).T
        coef, *_ = np.linalg.lstsq(A, cumulative, rcond=None)
        velocity = float(coef[0])
        fit_res = A @ coef - cumulative
        if len(epochs) > 2:
            s = np.sqrt(np.sum(fit_res ** 2) / (len(epochs) - 2))
            try:
                velocity_err = float(s * np.sqrt(np.linalg.inv(A.T @ A)[0, 0]))
            except np.linalg.LinAlgError:
                pass

    return {
        "epochs": epochs,
        "cumulative_mm": cumulative,
        "error_mm": errors,
        "residual_rms_mm": float(np.sqrt(np.mean(residuals ** 2))) if len(residuals) else float("nan"),
        "sigma_mm": sigma,
        "dof": dof,
        "velocity_mm_per_day": velocity,
        "velocity_err_mm_per_day": velocity_err,
        "n_pairs": len(usable),
    }


def jackknife(label: str, comp_index: int, pairs: list[Pair],
              epochs: list[date]) -> dict | None:
    """
    Refit the linear velocity with each pair left out in turn.

    A network with no redundancy fits every observation exactly, so the formal
    error bars are undefined and a trend can look significant while resting
    entirely on one interferogram. Leave-one-out is the cheapest honest test
    available: if dropping a single pair changes the sign of the velocity, the
    trend is that pair, not the ground.

    This is not a refinement. On the current stack it is the step that
    separates a real signal from three artefacts.
    """
    usable = [p for p in pairs if p.value is not None]
    if len(usable) < 4:
        return None

    full = invert_component(usable, epochs)
    v0 = full.get("velocity_mm_per_day")
    if v0 is None or not np.isfinite(v0):
        return None

    print(f"\n  jackknife ({len(usable)} pairs, full fit {v0:+.3f} mm/day)")
    vs, flips, load_bearing = [], [], []
    for k, p in enumerate(usable):
        sub = usable[:k] + usable[k + 1:]
        # Removing a link from a chain splits it. The two halves then have
        # independent zeros and lstsq, handed a rank-deficient system, returns
        # a minimum-norm solution that silently bridges the break. Refitting
        # that number and calling it a jackknife would be worse than not
        # testing at all - it invents agreement or disagreement at random.
        if len(connected_components(sub)) > len(connected_components(usable)):
            load_bearing.append(p)
            print(f"    without {p.ref} -> {p.sec}   network splits - "
                  f"no trend can be fitted")
            continue
        eps = sorted({e for q in sub for e in (q.ref, q.sec)})
        r = invert_component(sub, eps)
        v = r.get("velocity_mm_per_day")
        if v is None or not np.isfinite(v):
            continue
        vs.append(v)
        flip = v * v0 < 0
        if flip:
            flips.append(p)
        print(f"    without {p.ref} -> {p.sec}   {v:+.3f} mm/day"
              + ("   <- SIGN FLIPS" if flip else ""))

    if load_bearing:
        print(f"\n    {len(load_bearing)} of {len(usable)} pairs are load-bearing: "
              f"removing any one\n    disconnects the network. This block is a "
              f"chain with redundancy 0, so\n    leave-one-out cannot test it - "
              f"there is nothing to leave out.")
        print(f"    The trend {v0:+.3f} mm/day is UNTESTED, not confirmed.")
        print(f"    To make it testable, add pairs that close loops: a 24-day\n"
              f"    interferogram spanning two existing 12-day steps gives\n"
              f"    redundancy 1 and a real residual. Those products already\n"
              f"    exist in the archive.")

    if not vs:
        return {"geometry": label, "component": comp_index,
                "velocity_mm_per_day": v0, "testable": False}
    robust = min(vs) * max(vs) > 0
    print(f"    range {min(vs):+.3f} to {max(vs):+.3f} mm/day   ", end="")
    if robust:
        print("ROBUST - every subset agrees in sign")
    else:
        who = ", ".join(f"{p.ref}->{p.sec}" for p in flips)
        print(f"NOT ROBUST\n    the sign depends on a single pair ({who}). "
              f"Treat the trend as\n    undetermined until another pair or the "
              f"other geometry supports it.")
    return {"geometry": label, "component": comp_index, "velocity_mm_per_day": v0,
            "jackknife_min": min(vs), "jackknife_max": max(vs),
            "robust": robust, "testable": True}


def measure_pairs(pairs: list[Pair], coh_threshold: float,
                  ref_lat: float | None, ref_lon: float | None,
                  clip_aoi: bool, flip_sign: bool,
                  target_lat: float | None = None, target_lon: float | None = None,
                  target_radius_px: int = 5, auto_ref: bool = False,
                  goff_layer: str = "layer2", common_ref: bool = False,
                  buffer_km: float = 2.0) -> None:
    """
    Fill Pair.value with a displacement measurement from each GUNW.

    Without a target, the median over the whole valid AOI is used. That is only
    meaningful if the AOI is dominated by the signal you care about - a small
    landslide inside a large stable AOI will be averaged into nothing.

    With --target-lat/--target-lon, the median over a small window at the target
    is used instead. Combined with a reference point on stable ground, that is
    the displacement OF the target RELATIVE TO the reference, which is the only
    thing InSAR can actually measure.
    """
    from gunw_reader import read_gunw

    lattices: dict[int, np.ndarray] = {}
    for p in pairs:
        if not p.source or not Path(p.source).exists():
            continue
        is_goff = "GOFF" in Path(p.source).name.upper()
        try:
            if is_goff:
                # GOFF has no phase. Slant-range offset IS the LOS component,
                # so it drops straight into the same inversion - the network
                # maths does not care which product measured the difference.
                from goff_reader import read_goff
                g = read_goff(Path(p.source), layer=goff_layer, clip_aoi=clip_aoi)
                key = next((k for k in g["layers"] if k.endswith(goff_layer)),
                           sorted(g["layers"])[0])
                L = g["layers"][key]
                r = {"valid": L["valid"],
                     "displacement_mm": L["range_m"] * 1000.0,
                     "coherence": L["correlation"],
                     "xs": L["xs"], "ys": L["ys"], "epsg": L["epsg"]}
            else:
                r = read_gunw(Path(p.source), coherence_threshold=coh_threshold,
                              ref_lat=ref_lat, ref_lon=ref_lon, auto_ref=auto_ref,
                              clip_aoi=clip_aoi, flip_sign=flip_sign)
        except Exception as exc:
            logger.error("%s: %s", Path(p.source).name, exc)
            continue
        valid = r["valid"]
        disp = r["displacement_mm"]

        if common_ref:
            # Onto the fixed AOI lattice, so every pair of a geometry shares
            # pixel centres and one datum can be set across all of them.
            import gunw_reader as _g
            grid = _g.aoi_grid(r["xs"], r["ys"], _g.AOI_RING, r["epsg"])
            if grid is None:
                logger.error("%s: off the AOI lattice, cannot join a common datum; "
                             "excluded", Path(p.source).name)
                continue
            lattices[id(p)] = (_g.place_on_grid(np.where(valid, disp, np.nan), grid),
                               grid, r["epsg"])
            p.n_px = int(valid.sum())
            p.coherence = (float(r["coherence"][valid].mean())
                           if r["coherence"] is not None and valid.any() else None)
            continue

        if target_lat is not None and target_lon is not None and r["xs"] is not None:
            xs, ys = r["xs"], r["ys"]
            tx, ty = target_lon, target_lat
            if r["epsg"] and r["epsg"] != 4326:
                try:
                    from pyproj import Transformer
                    tx, ty = Transformer.from_crs(4326, r["epsg"], always_xy=True).transform(
                        target_lon, target_lat)
                except ImportError:
                    logger.warning("pyproj missing - cannot place target on a projected grid.")
            j = int(np.argmin(np.abs(xs - tx)))
            i = int(np.argmin(np.abs(ys - ty)))
            t = target_radius_px
            sel = np.zeros_like(valid)
            sel[max(0, i - t): i + t + 1, max(0, j - t): j + t + 1] = True
            valid = valid & sel

        n = int(valid.sum())
        if n == 0:
            logger.warning("%s: no valid pixels in the measurement window, excluded",
                           Path(p.source).name)
            continue
        p.value = float(np.median(disp[valid]))
        p.n_px = n
        p.coherence = (float(r["coherence"][valid].mean())
                       if r["coherence"] is not None else None)
        logger.info("%s -> %s : %+8.2f mm  (%d px, coh %.2f)",
                    p.ref, p.sec, p.value, n,
                    p.coherence if p.coherence is not None else float("nan"))

    if common_ref:
        apply_common_datum(pairs, lattices, target_lat, target_lon,
                           target_radius_px, buffer_km)


def apply_common_datum(pairs: list[Pair], lattices: dict, target_lat, target_lon,
                       radius_px: int, buffer_km: float) -> None:
    """
    Fill Pair.value on ONE datum per geometry, not one per pair.

    Every pair used to carry its own reference - an auto-reference block for
    GUNW, a deramp plane for GOFF - and the inversion summed those differences
    as though they shared a zero. Here the datum is the median over pixels
    valid in every pair of the geometry and more than buffer_km from the
    target, so it is never set on the ground being measured. The constant
    removed from each pair is logged: it is exactly the step that pair's
    private reference would have put into the series.
    """
    from rasterio.transform import rowcol
    from common_ref import buffer_mask, common_mask, rereference, window_median

    groups: dict[tuple, list[Pair]] = defaultdict(list)
    for p in pairs:
        if id(p) in lattices:
            groups[(p.direction, p.path)].append(p)

    for (direction, path), group in sorted(groups.items()):
        stack = [lattices[id(p)][0] for p in group]
        _, grid, epsg = lattices[id(group[0])]
        if any(lattices[id(p)][0].shape != stack[0].shape for p in group):
            logger.error("%s path %s: pairs landed on different lattices - no "
                         "common datum", direction, path)
            continue
        centre = None
        if target_lat is not None and target_lon is not None:
            tx, ty = target_lon, target_lat
            if epsg and epsg != 4326:
                from pyproj import Transformer
                tx, ty = Transformer.from_crs(4326, epsg, always_xy=True).transform(
                    target_lon, target_lat)
            centre = rowcol(grid["transform"], tx, ty)
        stable = common_mask(stack)
        if centre is not None:
            px = grid["res_x"]
            stable &= ~buffer_mask(stable.shape, [centre], int(round(buffer_km * 1000 / px)))
        try:
            fixed, consts = rereference(stack, stable)
        except ValueError as exc:
            logger.error("%s path %s: %s - no common datum, pairs left unmeasured",
                         direction, path, exc)
            continue
        logger.info("%s path %s: common datum on %d px valid in all %d pairs",
                    direction, path, int(stable.sum()), len(group))
        for p, f, k in zip(group, fixed, consts):
            if centre is not None:
                p.value, n = window_median(f, centre, radius_px)
            else:
                v = f[np.isfinite(f)]
                p.value, n = (float(np.median(v)) if v.size else float("nan")), int(v.size)
            if not np.isfinite(p.value):
                p.value = None
                logger.warning("%s -> %s: no valid pixel at the target, excluded",
                               p.ref, p.sec)
                continue
            logger.info("%s -> %s : %+8.2f mm on the common datum (%d px), "
                        "datum error %+.1f mm = %+.2f mm/day",
                        p.ref, p.sec, p.value, n, k, k / max(p.span_days, 1))


def report_series(label: str, comp_index: int, result: dict) -> list[dict]:
    if "error" in result:
        print(f"\n{label} component {comp_index}: {result['error']}")
        return []

    print(f"\n{label}  -  component {comp_index}")
    print(f"  {result['n_pairs']} pairs, {len(result['epochs'])} epochs, dof {result['dof']}")
    if result["dof"] > 0:
        print(f"  residual RMS {result['residual_rms_mm']:.2f} mm  (sigma {result['sigma_mm']:.2f} mm)")
    else:
        print("  exactly determined - residual and error are undefined")

    print(f"\n  {'EPOCH':<12}{'CUMULATIVE mm':>15}{'+/- mm':>10}{'DAYS':>7}")
    print("  " + "-" * 44)
    t0 = result["epochs"][0]
    rows = []
    for e, d, err in zip(result["epochs"], result["cumulative_mm"], result["error_mm"]):
        errs = "   n/a" if not np.isfinite(err) else f"{err:>10.2f}"
        print(f"  {str(e):<12}{d:>15.2f}{errs}{(e-t0).days:>7}")
        rows.append({"geometry": label, "component": comp_index, "epoch": str(e),
                     "days_from_start": (e - t0).days,
                     "cumulative_mm": round(float(d), 3),
                     "error_mm": None if not np.isfinite(err) else round(float(err), 3)})

    v, ve = result["velocity_mm_per_day"], result["velocity_err_mm_per_day"]
    if np.isfinite(v):
        span = (result["epochs"][-1] - t0).days
        pm = f" +/- {ve:.3f}" if np.isfinite(ve) else ""
        print(f"\n  linear velocity {v:+.3f}{pm} mm/day   "
              f"({v*365:+.0f} mm/yr, over {span} d)")
        if np.isfinite(ve) and ve > 0 and abs(v) < 2 * ve:
            print("  NOT significant at 2 sigma - consistent with no motion.")
    return rows


# ---------------------------------------------------------------------------
def plot_series(all_rows: list[dict], out: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.error("matplotlib required for --plot:  pip install matplotlib")
        return

    groups = defaultdict(list)
    for r in all_rows:
        groups[(r["geometry"], r["component"])].append(r)

    fig, ax = plt.subplots(figsize=(10, 6))
    for (geom, comp), rows in sorted(groups.items()):
        xs = [datetime.strptime(r["epoch"], "%Y-%m-%d") for r in rows]
        ys = [r["cumulative_mm"] for r in rows]
        es = [r["error_mm"] or 0 for r in rows]
        ax.errorbar(xs, ys, yerr=es, marker="o", capsize=3,
                    label=f"{geom} (block {comp})")
    ax.axvline(datetime(2026, 8, 26), color="crimson", ls="--", lw=1.2)
    ax.text(datetime(2026, 8, 26), ax.get_ylim()[1], " 26 Aug event",
            color="crimson", va="top", fontsize=9)
    ax.set_ylabel("Cumulative LOS displacement (mm)\n+ve away from satellite")
    ax.set_title("NISAR L-band cumulative displacement\n"
                 "each block has its own zero - blocks are not comparable",
                 fontsize=11)
    ax.grid(alpha=.3)
    ax.legend(fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    logger.info("Wrote %s", out)


def main() -> int:
    ap = argparse.ArgumentParser(description="GUNW pairs -> cumulative LOS time series")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", metavar="DIR", nargs="?", const=str(NISAR),
                     help="folder of products (default: data/nisar_l2)")
    src.add_argument("--from-catalogue", action="store_true",
                     help="analyse network from ASF without downloading")
    src.add_argument("--from-stats", metavar="STATS.csv",
                     help="invert from a gunw_reader --csv summary, no products "
                          "needed (implies --invert)")
    ap.add_argument("--network", action="store_true", help="network structure only")
    ap.add_argument("--jackknife", action="store_true",
                    help="refit leaving each pair out; flags trends that rest "
                         "on a single interferogram")
    ap.add_argument("--invert", action="store_true", help="run the inversion (needs --dir)")
    ap.add_argument("--product", choices=("GUNW", "GOFF"), default="GUNW",
                    help="which product to build the series from")
    ap.add_argument("--goff-layer", default="layer2",
                    help="GOFF correlation-window layer (layer3 is usually quietest)")
    ap.add_argument("--aoi", choices=("source", "langtang", "lhende"), default="source",
                    help="analysis AOI. Defaults to the confirmed detachment zone; it used to default to the wider langtang box")
    ap.add_argument("--auto-ref", action="store_true",
                    help="pick the reference automatically (recommended)")
    ap.add_argument("--coh-threshold", type=float, default=0.3)
    ap.add_argument("--ref-lat", type=float)
    ap.add_argument("--ref-lon", type=float)
    ap.add_argument("--target-lat", type=float, help="measure here instead of the AOI median")
    ap.add_argument("--target-lon", type=float)
    ap.add_argument("--target-radius", type=int, default=5, help="target window half-width in px")
    ap.add_argument("--common-ref", action="store_true",
                    help="reference every pair of a geometry to ONE datum: pixels "
                         "valid in all of them, --buffer-km clear of the target. "
                         "Without it each pair keeps its own reference and the "
                         "series inherits a step at every epoch")
    ap.add_argument("--buffer-km", type=float, default=2.0,
                    help="with --common-ref, no datum pixel within this of the target")
    ap.add_argument("--no-clip", action="store_true")
    ap.add_argument("--flip-sign", action="store_true")
    ap.add_argument("--csv", metavar="OUT.csv")
    ap.add_argument("--plot", metavar="OUT.png")
    ap.add_argument("--json", metavar="OUT.json", help="dump the network summary")
    args = ap.parse_args()

    from gunw_reader import set_aoi
    set_aoi(args.aoi)

    if args.from_stats:
        pairs = pairs_from_stats(resolve(args.from_stats),
                                 layer=args.goff_layer
                                 if args.product == "GOFF" else None)
        args.invert = True          # the values are already in the CSV
    elif args.from_catalogue:
        pairs = pairs_from_catalogue()
    else:
        pairs = pairs_from_dir(resolve(args.dir), args.product)
    if not pairs:
        logger.error("No pairs found.")
        return 1

    summary = describe_network(pairs)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1))
        logger.info("Wrote %s", args.json)

    if args.network or not args.invert:
        if args.from_catalogue:
            print("Nothing downloaded yet - run nisar_acquisition.py --download GUNW "
                  "then re-run with --dir to invert.")
        return 0

    if args.from_catalogue:
        logger.error("--invert needs local files. Use --dir.")
        return 1

    if not args.from_stats:
        if args.ref_lat is None or args.ref_lon is None:
            logger.warning(
                "No --ref-lat/--ref-lon. Each interferogram keeps its own arbitrary "
                "offset, so the assembled series will be meaningless. Strongly advised."
            )

        logger.info("Measuring displacement for %d pairs...", len(pairs))
        if args.target_lat is None:
            logger.warning(
                "No --target-lat/--target-lon: using the median over the whole AOI. "
                "A localised signal will be averaged away. Give a target for hazard work."
            )
        measure_pairs(pairs, args.coh_threshold, args.ref_lat, args.ref_lon,
                      not args.no_clip, args.flip_sign,
                      args.target_lat, args.target_lon, args.target_radius,
                      auto_ref=args.auto_ref, goff_layer=args.goff_layer,
                      common_ref=args.common_ref, buffer_km=args.buffer_km)

    by_geom = defaultdict(list)
    for p in pairs:
        by_geom[(p.direction, p.path)].append(p)

    all_rows: list[dict] = []
    for (direction, path), group in sorted(by_geom.items()):
        label = f"{'ASC' if direction == 'A' else 'DESC'} path {path}"
        for k, comp in enumerate(connected_components(group), 1):
            inside = [p for p in group if p.ref in comp and p.sec in comp]
            all_rows += report_series(label, k, invert_component(inside, comp))
            if args.jackknife:
                jackknife(label, k, inside, comp)

    if args.csv and all_rows:
        keys = ["geometry", "component", "epoch", "days_from_start", "cumulative_mm", "error_mm"]
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(all_rows)
        logger.info("Wrote %s (%d rows)", args.csv, len(all_rows))

    if args.plot and all_rows:
        plot_series(all_rows, Path(args.plot))

    return 0


if __name__ == "__main__":
    sys.exit(main())
