"""
bound.py
--------
The headline, computed in one place from the committed measurements.

Why this exists
===============
The headline used to be assembled by hand in four places - README, demo,
site, video script - and it drifted in all of them. It also compared unlike
things: a NISAR floor expressed as DOWNSLOPE motion against a Sentinel-1
precursor that the reports never say is downslope, and a floor for ONE PIXEL
against a rate that is an average over the slope. Both choices inflated the
gap, and together they turned "about 30-100x" into "180-360x".

So this module states each comparison like for like and computes it:

    line of sight against line of sight   the precursor as reported, against
                                          the NISAR ascending LOS floor
    per pixel                             the pessimistic end: one pixel's
                                          3-sigma floor at the candidate
    window median                         the optimistic end: 3 MADs of the
                                          1 km block medians across the AOI,
                                          which includes the correlated error
                                          a single window cannot see

and it does so at EVERY detachment-like candidate, because a floor measured
at one candidate is not the floor at the others. The candidate that matches
the published detachment elevation is flagged, not silently chosen.

Inputs are external facts stated as constants with their source; everything
else is read from outputs/.

    python src/bound.py            # table, and outputs/bound_source.csv
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from geometry_merge import sensitivities  # noqa: E402

OUT = ROOT / "outputs"
EVENT = date(2026, 8, 26)

# External inputs, with where they come from. Not measurements of this project.
#
# Shirzaei (Virginia Tech), Sentinel-1 InSAR, 8 Jan - 18 Aug 2026, as reported
# in the press in September 2026: "roughly 10 millimetres per month", with
# acceleration over the final weeks. The reports do not say whether it is line
# of sight or downslope, or which track; it is taken here as line of sight,
# the form an InSAR rate is normally reported in, and compared as such.
PRECURSOR_MM_DAY = 10.0 / 30.0
# Published accounts: the detachment was on the north face at about
# 5,200-5,400 m (Wikipedia, 2026 Nepal-Tibet floods, citing USGS and GFZ).
PUBLISHED_ELEV_M = (5200.0, 5400.0)

COVER_DAYS = 60            # the seven weeks before failure, plus the 24-day pair
ASC_REFS = {"20251128", "20251210", "20251222", "20260103",
            "20260702", "20260714", "20260726", "20260819"}


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def pair_of(name: str) -> tuple[date, date]:
    """(reference, secondary) from 'GOFF_YYYYMMDD_YYYYMMDD_...' or 'GUNW_...'."""
    parts = Path(name).name.split("_")
    return _d(parts[1]), _d(parts[2])


def covers_pre_event(name: str) -> bool:
    """Ends before the collapse, and starts inside the pre-failure window."""
    ref, sec = pair_of(name)
    return sec < EVENT and (EVENT - ref).days <= COVER_DAYS


def load(name: str) -> list[dict]:
    with open(OUT / name, newline="") as fh:
        return list(csv.DictReader(fh))


def worst(rows: list[dict], key: str) -> float:
    vals = [float(r[key]) for r in rows if r.get(key) not in (None, "", "nan")]
    vals = [v for v in vals if v == v]
    return max(vals) if vals else float("nan")


def assess(cand: dict, goff: list[dict], phase: list[dict], cref: list[dict]) -> dict:
    """Every number the headline needs, for one candidate."""
    cid = cand["id"]
    lat, lon = float(cand["lat"]), float(cand["lon"])
    slope, aspect, elev = (float(cand[k]) for k in ("slope_deg", "aspect_deg", "elev_m"))
    sens = next(r["sensitivity"] for r in sensitivities(lat, lon, slope, aspect, 0.3)
                if r["track"] == "NISAR ASC 98")

    g = [r for r in goff if r["target_id"] == cid and r["usable"] == "True"
         and r["file"][5:13] in ASC_REFS and covers_pre_event(r["file"])]
    p = [r for r in phase if r["target_id"] == cid and r["usable"] == "True"]
    c = [r for r in cref if r["target_id"] == cid and int(r["window_px"]) >= 6]

    px = worst(g, "local_floor_mm_day")
    blk = worst(g, "block_median_floor_mm_day")
    v_common = max((abs(float(r["target_mm_day_common_ref"])) for r in c), default=float("nan"))
    return {
        "id": cid, "lat": lat, "lon": lon, "elev_m": elev, "aspect_deg": round(aspect, 1),
        "matches_published_elevation": PUBLISHED_ELEV_M[0] <= elev <= PUBLISHED_ELEV_M[1],
        "nisar_asc_sensitivity": round(sens, 3),
        "goff_intervals": len(g),
        "los_floor_pixel_mm_day": round(px, 2),
        "los_floor_block_mm_day": round(blk, 2),
        "downslope_bound_mm_day": round(px / abs(sens), 1) if g else float("nan"),
        "ratio_los_pixel": round(px / PRECURSOR_MM_DAY, 1) if g else float("nan"),
        "ratio_los_block": round(blk / PRECURSOR_MM_DAY, 1) if g else float("nan"),
        "max_speed_common_datum_mm_day": round(v_common, 2),
        "phase_pairs": len(p),
        "phase_floor_pixel_mm_day": round(worst(p, "local_floor_mm_day"), 2),
        "phase_floor_block_mm_day": round(worst(p, "block_median_floor_mm_day"), 2),
    }


def main() -> int:
    cands = [r for r in load("scar_candidates_source.csv")
             if r["reading"] == "DETACHMENT-like"]
    goff = load("local_floor_candidates.csv")
    phase = load("local_floor_phase_candidates.csv")
    cref = load("common_ref_summer.csv")
    rows = [assess(c, goff, phase, cref) for c in cands]

    print(f"\nPRECURSOR  {PRECURSOR_MM_DAY:.2f} mm/day (Sentinel-1, as reported; "
          f"taken as line of sight)\n")
    print(f"  {'#':>2}{'elev':>6}{'pub?':>6}{'sens':>7}{'LOS px':>8}{'LOS blk':>9}"
          f"{'x px':>7}{'x blk':>7}{'downsl':>8}{'|v| cd':>8}{'phase':>8}")
    print("  " + "-" * 78)
    for r in rows:
        ph = (f"{r['phase_floor_pixel_mm_day']:.1f}" if r["phase_pairs"] else "none")
        if r["goff_intervals"]:
            print(f"  {r['id']:>2}{r['elev_m']:>6.0f}{'yes' if r['matches_published_elevation'] else 'no':>6}"
                  f"{r['nisar_asc_sensitivity']:>7.3f}{r['los_floor_pixel_mm_day']:>8.1f}"
                  f"{r['los_floor_block_mm_day']:>9.1f}{r['ratio_los_pixel']:>7.0f}"
                  f"{r['ratio_los_block']:>7.0f}{r['downslope_bound_mm_day']:>8.0f}"
                  f"{r['max_speed_common_datum_mm_day']:>8.1f}{ph:>8}")
        else:
            print(f"  {r['id']:>2}{r['elev_m']:>6.0f}{'yes' if r['matches_published_elevation'] else 'no':>6}"
                  f"{r['nisar_asc_sensitivity']:>7.3f}   no valid offsets - UNOBSERVED{ph:>17}")
    print("\n  LOS px   worst per-pixel 3-sigma floor over the intervals covering the")
    print("           seven weeks before failure, at the candidate (pessimistic)")
    print("  LOS blk  worst floor on a 1 km window median, from AOI block scatter")
    print("           (optimistic)")
    print("  x        each divided by the precursor - LOS against LOS")
    print("  |v| cd   fastest covering interval at the candidate on a common datum")
    print("  phase    worst winter per-pixel phase floor; 'none' = no valid phase")
    print("           pixel in any GUNW pair at that candidate")

    with open(OUT / "bound_source.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  wrote {OUT / 'bound_source.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
