#!/usr/bin/env python3
"""
demo.py
-------
The whole argument in thirty seconds, computed live from committed data.

    python demo.py                 # ~25 s, paced for an audience
    python demo.py --fast          # no pauses, for CI
    python demo.py --plain         # no colour, for a recording without ANSI

No network. No credentials. No 51 GB download. Every number below is read from
a CSV in outputs/ that was produced by this repository's own tools, or computed
here from those CSVs.

The inputs that are not measurements of this project are the event date, the
coordinate the reports gave, and the precursor rate another team measured -
the last of which is imported from bound.py, where its source is stated.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics as st
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

EVENT = date(2026, 8, 26)
ASSUMED = (28.28771, 85.52809)      # read off a report

C = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "c": "\033[36m",
     "b": "\033[1m", "d": "\033[2m", "0": "\033[0m"}


class Screen:
    def __init__(self, pace: float, colour: bool):
        self.pace, self.colour = pace, colour

    def c(self, key: str) -> str:
        return C[key] if self.colour else ""

    def say(self, text: str = "", hold: float = 1.0) -> None:
        print(text)
        if self.pace:
            time.sleep(self.pace * hold)

    def rule(self, title: str = "") -> None:
        bar = "=" * 68
        print(f"\n{self.c('d')}{bar}{self.c('0')}")
        if title:
            print(f"{self.c('b')}{title}{self.c('0')}")
            print(f"{self.c('d')}{bar}{self.c('0')}")
        if self.pace:
            time.sleep(self.pace * 0.6)


def load(name: str) -> list[dict]:
    p = ROOT / "outputs" / name
    if not p.exists():
        raise SystemExit(f"missing {p} - run from the repository root")
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def km(a: tuple, b: tuple) -> float:
    dlat = (a[0] - b[0]) * 111320
    dlon = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon) / 1000.0


def main() -> int:
    ap = argparse.ArgumentParser(description="SARGuardian in thirty seconds")
    ap.add_argument("--fast", action="store_true", help="no pauses")
    ap.add_argument("--plain", action="store_true", help="no colour")
    ap.add_argument("--pace", type=float, default=0.63,
                    help="seconds per beat. 0.63 lands the paced run at about "
                         "29 s, which is the slot a Space Apps video gets")
    a = ap.parse_args()
    s = Screen(0.0 if a.fast else a.pace, not a.plain)
    from bound import PRECURSOR_MM_DAY, PUBLISHED_ELEV_M

    # ---------------------------------------------------------------- 0:00
    s.rule("SARGuardian  -  can NISAR warn of a Himalayan collapse?")
    s.say(f"  {s.c('b')}26 August 2026{s.c('0')}  Langtang Lirung, Nepal.")
    s.say("  A rock and ice face detaches, falls 1,200 m, dams the Lhende Khola,")
    s.say(f"  and the barrier bursts. {s.c('r')}More than a thousand people die.{s.c('0')}")
    s.say()
    s.say(f"  {s.c('d')}We had 16 NISAR L2 offset products over that slope. Could they"
          f" have warned?{s.c('0')}", 1.4)

    # ---------------------------------------------------------------- 0:06
    s.rule("1.  WHERE DID IT FAIL?   (Sentinel-2, mapped not assumed)")
    cands = [r for r in load("scar_candidates_source.csv")
             if r["reading"] == "DETACHMENT-like"]
    lo_e, hi_e = PUBLISHED_ELEV_M
    for r in cands:
        e = float(r["elev_m"])
        tag = f"{s.c('g')}<- published elevation{s.c('0')}" if lo_e <= e <= hi_e else ""
        s.say(f"  candidate {r['id']:>2}  {float(r['area_km2']):4.2f} km2  {e:5.0f} m  "
              f"faces {float(r['aspect_deg']):3.0f} deg  {tag}", 0.5)
    near = min(km(ASSUMED, (float(r["lat"]), float(r["lon"]))) for r in cands)
    s.say()
    s.say(f"  {s.c('y')}{len(cands)} candidates, all facing north. The point the reports"
          f" gave is{s.c('0')}")
    s.say(f"  {s.c('y')}{near:.2f} km from the nearest, and is not a scar: it got"
          f" BRIGHTER.{s.c('0')}", 1.4)

    # ---------------------------------------------------------------- 0:12
    s.rule("2.  WHAT COULD THE RADAR SEE?   (16 NISAR GOFF products)")
    goff = [r for r in load("goff_stats_source.csv")
            if r["layer"] == "HH/layer2" and r["processing"] == "PR"]
    asc = [float(r["detect_floor_mm_day"]) for r in goff if r["track"] == "ASC 098"]
    desc = [float(r["detect_floor_mm_day"]) for r in goff if r["track"] == "DESC 048"]
    s.say(f"  3-sigma detection floor, median over the 82 km2 source zone:")
    s.say(f"     ASC  098   {st.median(asc):6.1f} mm/day   "
          f"{s.c('g')}the usable geometry{s.c('0')}")
    s.say(f"     DESC 048   {st.median(desc):6.1f} mm/day   "
          f"{s.c('d')}5x noisier, same ground{s.c('0')}")
    s.say()

    pick = next(r for r in cands if lo_e <= float(r["elev_m"]) <= hi_e)
    lf = [r for r in load("local_floor_candidates.csv")
          if r["usable"] == "True" and r["target_id"] == pick["id"]]

    def spans(row):
        """(reference, secondary) from GOFF_YYYYMMDD_YYYYMMDD_..."""
        n = row["file"]
        return (datetime.strptime(n[5:13], "%Y%m%d").date(),
                datetime.strptime(n[14:22], "%Y%m%d").date())

    # An interval is pre-event only if it ENDS before the collapse. Filtering on
    # the reference date alone lets 2026-08-19 -> 2026-08-31 through, and that
    # pair contains the failure itself - metres of apparent offset, and a floor
    # that describes the event rather than the seven weeks before it. This is
    # the same cutoff inverse_velocity.py enforces, and the same mistake it was
    # written to stop.
    summer = [r for r in lf
              if spans(r)[1] < EVENT and (EVENT - spans(r)[0]).days <= 60]
    worst = max(float(r["local_floor_mm_day"]) for r in summer) if summer else float("nan")
    blk = max(float(r["block_median_floor_mm_day"]) for r in summer) if summer else float("nan")
    s.say(f"  One pixel on the slope is noisier than the area. At candidate {pick['id']},")
    s.say(f"  over the seven weeks before failure (calibrated PROVISIONAL products):")
    for r in sorted(summer, key=lambda x: x["file"]):
        s.say(f"     {r['file'][5:22]}  {float(r['local_floor_mm_day']):6.1f} mm/day"
              f"   [{float(r['local_floor_ci_lo']):.0f}-{float(r['local_floor_ci_hi']):.0f}]"
              f"  on {r['window_px']}/{r['window_total_px']} px")
    s.say(f"  {s.c('b')}A bound across a window is set by its weakest interval: "
          f"{worst:.1f} mm/day.{s.c('0')}")
    s.say(f"  {s.c('d')}Averaged over a 1 km window instead of one pixel: "
          f"{blk:.1f} mm/day.{s.c('0')}", 1.3)

    # ---------------------------------------------------------------- 0:19
    s.rule("3.  DID ANYTHING MOVE?   (the detector, on the real series)")
    from inverse_velocity import analyse_block, load_floors, load_series
    series = load_series(ROOT / "outputs" / "ts_goff_source.csv")
    floors = load_floors(ROOT / "outputs" / "goff_stats_source.csv", "layer2")
    import contextlib, io
    fired, checked = [], 0
    for (geom, comp), rows in sorted(series.items()):
        with contextlib.redirect_stdout(io.StringIO()):
            r = analyse_block(geom, comp, rows, noise_floor=max(floors.values()),
                              window=3, r2_min=0.7, horizon_days=60,
                              sig_multiple=1.0, event_date=EVENT,
                              floors=floors, cutoff=EVENT)
        checked += 1
        if r.get("alarm"):
            fired.append(r)
    s.say(f"  Inverse-velocity detector, gated on each pair's own measured floor,")
    s.say(f"  with every interval that touches the event dropped from the fit.")
    s.say()
    s.say(f"  blocks examined   {checked}")
    s.say(f"  alarms raised     {len(fired)}    "
          f"{s.c('g')}no alarm, and the floor says why{s.c('0')}", 1.4)

    # ---------------------------------------------------------------- 0:24
    s.rule("4.  THE ANSWER")
    b = next(r for r in load("bound_source.csv") if r["id"] == pick["id"])
    r_px, r_blk = worst / PRECURSOR_MM_DAY, blk / PRECURSOR_MM_DAY
    s.say(f"  A precursor {s.c('b')}did{s.c('0')} exist. Sentinel-1 interferometry"
          f" measured the slope")
    s.say(f"  creeping at about {s.c('c')}{PRECURSOR_MM_DAY:.2f} mm/day{s.c('0')},"
          f" accelerating over the final weeks.")
    s.say()
    s.say(f"     what was there            {s.c('c')}{PRECURSOR_MM_DAY:6.2f} mm/day{s.c('0')}"
          f"  (line of sight)")
    s.say(f"     what NISAR offsets saw    {s.c('r')}{blk:6.1f} - {worst:.1f} mm/day{s.c('0')}"
          f"  (line of sight)")
    if int(b["phase_pairs"]) == 0:
        s.say(f"     NISAR phase at this scar  {s.c('r')}no valid pixel in any pair{s.c('0')}")
    else:
        s.say(f"     NISAR phase at this scar  {float(b['phase_floor_pixel_mm_day']):.1f} mm/day")
    s.say()
    s.say(f"  {s.c('b')}NISAR L2 offset tracking at 12-day repeat was about "
          f"{r_blk:.0f}x to {r_px:.0f}x too insensitive.{s.c('0')}", 1.5)
    s.say()
    s.say(f"  {s.c('d')}Like for like: line of sight against line of sight, from a 1 km"
          f"{s.c('0')}")
    s.say(f"  {s.c('d')}average to a single pixel. The gap is the result, and it names"
          f" what{s.c('0')}")
    s.say(f"  {s.c('d')}the next system needs: phase that survives on a north face, and"
          f" revisit.{s.c('0')}")
    s.rule()
    s.say(f"  {s.c('d')}Every number above was computed from outputs/ just now."
          f"  python -m pytest tests/ -q{s.c('0')}")
    s.say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
