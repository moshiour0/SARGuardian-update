"""
candidate_check.py
------------------
Interrogate one location before calling it a landslide.

The problem
===========
A per-pixel stack will always produce candidates. Correlated atmosphere makes
clusters, low coherence makes outliers, and an unwrapper faced with a steep
gradient makes cycle errors that are spatially coherent and beautifully
monotonic. Every one of those looks like creep in a single geometry and a
single season.

So a candidate is not a measurement until it survives being attacked from
four directions at once, and this tool runs the attack.

    geometry     does the other orbit see it, at the sensitivity it should?
    season       is it present when the hazard mechanism is active?
    differential is it local, or is it the whole AOI moving together?
    ceiling      is it slower than lambda/4 per pair, i.e. measurable at all?

The seasonality test, and what it can and cannot say
===================================================
A signal that appears in the dry season and vanishes through the monsoon has
the seasonality of a snowpack. Dry snow is nearly transparent at L-band, so it
preserves coherence while adding a path delay that grows with accumulated water
equivalent - a steady, spatially coherent, high-coherence apparent motion that
is not motion.

The inverse does NOT follow, and an earlier version of this note assumed it
did. It read "Himalayan slope creep is monsoon-driven: pore pressure peaks June
to September", which is Class B reasoning - rainfall-triggered failure on
vegetated slopes. Class A failures, the kind this project targets, are not
necessarily monsoon-driven: Blatten went on 28 May, before any monsoon, and the
26 August 2026 detachment is attributed to permafrost degradation rather than
pore pressure. So dry-season quiescence is evidence AGAINST a snowpack delay,
but dry-season motion is not evidence against creep.

This test therefore only ever fires in one direction: it can reject a
candidate, never confirm one.

Usage
-----
    # measure the target first, in every product
    python src/timeseries.py --dir data/nisar_l2/GUNW --invert --aoi langtang \
        --auto-ref --target-lat 28.27484 --target-lon 85.47405 --target-radius 8

    # then interpret it against the AOI it sits in
    python src/candidate_check.py --target-log run.log \
        --aoi-csv outputs/gunw_stats_langtang.csv --lat 28.27484 --lon 85.47405
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("candidate")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import resolve  # noqa: E402

from gunw_reader import NISAR_LAMBDA_M as WAVELENGTH_M  # noqa: E402
MIN_PX = 20                    # below this a target window is not measured
MONSOON = (6, 7, 8, 9)

TARGET_LINE = re.compile(
    r"(\d{4}-\d\d-\d\d) -> (\d{4}-\d\d-\d\d) :\s+([-+]?[\d.]+) mm\s+"
    r"\((\d+) px, coh ([\d.]+)\)")


def read_target(path: Path) -> dict:
    """Per-pair measurements at the target, from a timeseries.py --target run."""
    out = {}
    for line in open(path, errors="ignore"):
        m = TARGET_LINE.search(line)
        if m:
            key = (m.group(1).replace("-", ""), m.group(2).replace("-", ""))
            out.setdefault(key, (float(m.group(3)), int(m.group(4)), float(m.group(5))))
    return out


def read_aoi(path: Path) -> dict:
    out = {}
    for r in csv.DictReader(open(path, newline="")):
        if "_UR_" in r["file"]:
            continue
        g = re.search(r"_GUNW_\d+_(\d+)_([AD])_", r["file"])
        if g:
            out[(r["reference"], r["secondary"])] = (float(r["median"]), g.group(2))
    return out


def span_days(k) -> int:
    from datetime import datetime
    a = datetime.strptime(k[0], "%Y%m%d")
    b = datetime.strptime(k[1], "%Y%m%d")
    return (b - a).days


MIN_SENS = 0.25            # below this a track is blind and the ratio is unstable
RATIO_MARGIN = 0.15        # predictions closer than this cannot be told apart
MIN_SLOPE_DEG = 10.0       # below this the aspect, and so the sensitivity, is noise


def classify_geometry(sens_asc: float, sens_desc: float,
                      v_asc: float, v_desc: float,
                      slope_deg: float | None = None) -> dict:
    """
    Does the pair of line-of-sight rates look like downslope motion, or like a
    path delay?

    Downslope motion has ONE magnitude D. Each track sees D projected onto its
    own line of sight, so

        v_asc = sens_asc * D        v_desc = sens_desc * D

    and the ratio v_desc/v_asc is fixed by geometry alone at sens_desc/sens_asc.
    Where the two sensitivities have opposite signs - which is most of this
    terrain - real motion MUST appear with opposite LOS signs.

    A path delay is not a vector. Atmosphere, or a snowpack, adds the same
    extra path length whichever direction the radar looks from, so it arrives
    with the SAME sign in both geometries and a ratio near +1.

    The previous version of this test had it backwards: it required matching
    signs before declaring a signal real, which is the signature of the thing
    it was meant to exclude. On the Langtang candidate (sens_asc -0.732,
    sens_desc +0.472) both rates are negative, so the old rule called a phase
    artefact "a real phase signal". The verdict there survived only because the
    seasonality test caught it afterwards.

    Returns a dict rather than printing, so the decision can be tested without
    a network round trip for terrain.
    """
    out = {"sens_asc": sens_asc, "sens_desc": sens_desc,
           "ratio_motion": None, "ratio_measured": None, "verdict": None,
           "reason": None, "conclusive": False}

    # Sensitivity is a projection onto the downslope direction, and that
    # direction comes from the DEM aspect. On gentle ground the aspect is
    # whichever way the DEM noise happens to tilt, so the sensitivity inherits
    # that and is not determined at all - measured here at this candidate, the
    # aspect moves 74 degrees and sens_asc runs from +0.020 to -0.642 as the
    # stencil widens from 60 m to 300 m, while a 62-degree slope 3 km away holds
    # its aspect to 4 degrees over the same range. A sensitivity quoted to three
    # decimals on a 5-degree slope is three decimals of nothing.
    if slope_deg is not None and slope_deg < MIN_SLOPE_DEG:
        out["verdict"] = "undetermined"
        out["reason"] = (f"the slope is {slope_deg:.1f} deg, below {MIN_SLOPE_DEG:.0f} deg. "
                         f"Aspect on ground this gentle is dominated by DEM noise, so the "
                         f"sensitivity it produces - and any ratio built on it - is not "
                         f"determined. Widen or narrow the DEM stencil and the answer "
                         f"changes sign.")
        return out

    if abs(sens_asc) < MIN_SENS or abs(sens_desc) < MIN_SENS:
        out["verdict"] = "undetermined"
        out["reason"] = (f"a track is blind here (|sensitivity| < {MIN_SENS}): "
                         f"asc {sens_asc:+.3f}, desc {sens_desc:+.3f}. Dividing by "
                         f"a near-zero sensitivity amplifies noise without limit.")
        return out

    ratio_motion = sens_desc / sens_asc
    out["ratio_motion"] = ratio_motion

    # If the geometry happens to predict a motion ratio near +1, the two
    # hypotheses make the same prediction and no measurement can separate them.
    # Saying so is the only honest option.
    if abs(ratio_motion - 1.0) < RATIO_MARGIN:
        out["verdict"] = "undetermined"
        out["reason"] = (f"the geometry predicts a motion ratio of {ratio_motion:+.3f}, "
                         f"indistinguishable from the +1.000 a path delay gives. "
                         f"These two tracks cannot separate the hypotheses at this "
                         f"location.")
        return out

    if v_asc == 0.0:
        out["verdict"] = "undetermined"
        out["reason"] = "the ascending rate is exactly zero; the ratio is undefined."
        return out

    ratio = v_desc / v_asc
    out["ratio_measured"] = ratio
    d_motion = abs(ratio - ratio_motion)
    d_delay = abs(ratio - 1.0)
    out["d_motion"], out["d_delay"] = d_motion, d_delay

    if d_motion < d_delay:
        out["verdict"] = "motion"
        out["implied_downslope_mm_day"] = v_asc / sens_asc
        out["reason"] = (f"measured ratio {ratio:+.3f} is closer to the "
                         f"downslope-motion prediction {ratio_motion:+.3f} "
                         f"(distance {d_motion:.3f}) than to the +1.000 of a "
                         f"path delay (distance {d_delay:.3f}).")
    else:
        out["verdict"] = "delay"
        out["reason"] = (f"measured ratio {ratio:+.3f} is closer to the +1.000 of a "
                         f"geometry-independent path delay (distance {d_delay:.3f}) "
                         f"than to the downslope-motion prediction {ratio_motion:+.3f} "
                         f"(distance {d_motion:.3f}).")
    out["conclusive"] = True
    return out


def geometry_test(lat, lon, dry: dict, problems: list) -> None:
    """
    Run classify_geometry on the dry-season rates and report it.

    Needs terrain, so it needs --lat/--lon and a network round trip. Without
    them the test is SKIPPED and says so - silently falling back to a sign
    comparison is what produced the original bug.
    """
    if lat is None or lon is None:
        print("\n  GEOMETRY TEST SKIPPED: needs --lat/--lon to look up slope and")
        print("  aspect. Without terrain there is no sensitivity, and without")
        print("  sensitivity the LOS signs mean nothing.")
        return

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from geometry_merge import slope_aspect, sensitivities
        slope, aspect, elev = slope_aspect(lat, lon)
        rows = sensitivities(lat, lon, slope, aspect, MIN_SENS)
    except Exception as exc:
        print(f"\n  GEOMETRY TEST SKIPPED: could not obtain terrain ({exc}).")
        return

    nisar = {r["ascending"]: r["sensitivity"] for r in rows if r["track"].startswith("NISAR")}
    if True not in nisar or False not in nisar:
        print("\n  GEOMETRY TEST SKIPPED: no NISAR ascending/descending pair in TRACKS.")
        return
    if "A" not in dry or "D" not in dry:
        print("\n  GEOMETRY TEST SKIPPED: needs a dry-season rate in both geometries.")
        return

    res = classify_geometry(nisar[True], nisar[False], dry["A"], dry["D"],
                            slope_deg=slope)
    print(f"\n  GEOMETRY  slope {slope:.1f} deg, aspect {aspect:.0f} deg, "
          f"elevation {elev:.0f} m")
    print(f"            sensitivity  ASC {res['sens_asc']:+.3f}   "
          f"DESC {res['sens_desc']:+.3f}"
          f"   ({'opposite' if res['sens_asc'] * res['sens_desc'] < 0 else 'same'} sign)")
    print(f"            measured     ASC {dry['A']:+.2f}   DESC {dry['D']:+.2f} mm/day")

    if res["verdict"] == "undetermined":
        print(f"\n  GEOMETRY TEST UNDETERMINED: {res['reason']}")
        return

    if res["verdict"] == "motion":
        print(f"\n  Consistent with DOWNSLOPE MOTION.")
        print(f"  {res['reason']}")
        print(f"  Implied downslope rate {res['implied_downslope_mm_day']:+.2f} mm/day.")
        print("  This is necessary, not sufficient: it says the two geometries are")
        print("  consistent with one moving surface, not that the surface moved.")
    else:
        problems.append("geometry")
        print(f"\n  GEOMETRY SAYS PATH DELAY, NOT MOTION.")
        print(f"  {res['reason']}")
        print("  A path delay is not a vector. Atmosphere and snow add the same extra")
        print("  path length whichever direction the radar looks from, so they arrive")
        print("  with the same sign in both geometries. One downslope rate cannot,")
        print("  where the sensitivities are opposed.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Stress-test one candidate location")
    ap.add_argument("--target-log", required=True,
                    help="log from timeseries.py --target-lat/--target-lon")
    ap.add_argument("--aoi-csv", required=True, help="gunw_reader --csv for the same AOI")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--min-px", type=int, default=MIN_PX)
    args = ap.parse_args()

    tgt = read_target(resolve(args.target_log))
    aoi = read_aoi(resolve(args.aoi_csv))
    shared = sorted(set(tgt) & set(aoi))
    if not shared:
        logger.error("No pairs common to the target log and the AOI CSV.")
        return 1

    where = (f"{args.lat:.5f} N {args.lon:.5f} E"
             if args.lat is not None and args.lon is not None else "target")
    print(f"\n{'=' * 74}")
    print(f"CANDIDATE AT {where}")
    print("=" * 74)
    print("\nLocal signal = target window minus AOI median, so whatever the AOI and")
    print("the target share - atmosphere, an unresolved reference offset - cancels.\n")
    print(f"  {'GEOM':<5}{'PAIR':<22}{'TARGET':>9}{'AOI':>9}{'LOCAL':>9}{'PX':>6}{'COH':>6}")
    print("  " + "-" * 68)

    groups: dict[tuple, list] = {}
    for k in sorted(shared, key=lambda k: (aoi[k][1], k[0])):
        med, geom = aoi[k]
        val, px, coh = tgt[k]
        local = val - med
        season = "monsoon" if int(k[0][4:6]) in MONSOON else "dry"
        groups.setdefault((geom, season), []).append((k, local, px))
        flag = "" if px >= args.min_px else "  too few px"
        print(f"  {geom:<5}{k[0]}-{k[1]:<13}{val:>9.1f}{med:>9.1f}"
              f"{local:>9.1f}{px:>6}{coh:>6.2f}{flag}")

    print()
    rates = {}
    for (geom, season), rows in sorted(groups.items()):
        use = [r for r in rows if r[2] >= args.min_px]
        if not use:
            print(f"  {geom} {season:<8} no pair has {args.min_px}+ valid pixels - "
                  f"not measured here")
            continue
        total = sum(r[1] for r in use)
        days = sum(span_days(r[0]) for r in use)
        rates[(geom, season)] = total / days
        print(f"  {geom} {season:<8} {len(use)} pair(s), net {total:+8.1f} mm / "
              f"{days:>3} d = {total/days:+6.2f} mm/day")

    # ---- the four tests ---------------------------------------------------
    print(f"\n{'=' * 74}\nVERDICT\n{'=' * 74}")
    ceiling = WAVELENGTH_M / 4 * 1000 / 12.0
    fast = [f"{g} {s}" for (g, s), v in rates.items() if abs(v) > ceiling]

    dry = {g: v for (g, s), v in rates.items() if s == "dry"}
    mon = {g: v for (g, s), v in rates.items() if s == "monsoon"}

    problems = []
    if len(dry) >= 2:
        geometry_test(args.lat, args.lon, dry, problems)

    if dry and mon:
        d = max(abs(v) for v in dry.values())
        m = max(abs(v) for v in mon.values())
        print(f"\n  dry season   up to {d:.2f} mm/day")
        print(f"  monsoon      up to {m:.2f} mm/day")
        if m < 0.4 * d:
            problems.append("seasonality")
            print("\n  SEASONALITY IS BACKWARDS. Himalayan slope creep is monsoon-driven -")
            print("  pore pressure peaks June to September. A signal that runs through")
            print("  the accumulation season and stops for the monsoon has the")
            print("  seasonality of a snowpack, not a slope.")
            print("\n  Dry snow is nearly transparent at L-band, so it preserves the")
            print("  coherence that makes this look trustworthy while adding a path")
            print("  delay that grows with accumulated water equivalent. The result is")
            print("  steady, spatially coherent apparent motion that is not motion.")

    if fast:
        problems.append("ceiling")
        print(f"\n  ABOVE THE CEILING in {', '.join(fast)}: faster than lambda/4 per")
        print(f"  12-day pair ({ceiling:.2f} mm/day), so the phase has wrapped and the")
        print("  unwrapper has guessed. Not a measurement.")

    if not problems:
        print("\n  Survives every test available here. Still not a detection until an")
        print("  independent measurement - field, GNSS, or a different sensor - agrees.")
    else:
        print(f"\n  NOT GROUND MOTION on this evidence. Record the location and the")
        print("  reason; do not carry it forward as a detection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
