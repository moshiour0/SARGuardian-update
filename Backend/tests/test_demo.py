"""
The demo has to compute, and it has to obey the cutoff.

Why this exists
===============
A demo that prints constants is a slideshow, and a demo that quietly includes
the event in its own pre-event window is the exact failure inverse_velocity.py
was written to stop - committed here once already, on the interval
2026-08-19 -> 2026-08-31.

These run the real thing end to end.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run([sys.executable, str(ROOT / "demo.py"), "--fast",
                           "--plain", *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=180)


def test_the_demo_runs_and_reaches_the_answer():
    out = run()
    assert out.returncode == 0, out.stderr[-800:]
    assert "THE ANSWER" in out.stdout
    assert "too insensitive" in out.stdout


def test_no_pre_event_interval_may_end_after_the_collapse():
    """
    The seven-week window is pre-event only if each interval ENDS before
    26 Aug. Filtering on the reference date alone admits 20260819_20260831,
    which contains the failure itself - metres of apparent offset, and a floor
    that describes the collapse rather than the weeks before it.
    """
    out = run()
    assert out.returncode == 0
    body = out.stdout.split("over the seven weeks before failure")[1].split("A bound")[0]
    spans = [ln.split()[0] for ln in body.strip().splitlines() if "_" in ln]
    assert spans, "no intervals listed"
    for s in spans:
        end = datetime.strptime(s.split("_")[1], "%Y%m%d").date()
        assert end < date(2026, 8, 26), f"{s} ends on or after the collapse"


def test_the_bound_is_the_weakest_pre_event_interval():
    """A bound across a window is set by its worst interval, not its median."""
    out = run()
    body = out.stdout.split("over the seven weeks before failure")[1].split("A bound")[0]
    floors = [float(ln.split()[1]) for ln in body.strip().splitlines() if "_" in ln]
    quoted = float(out.stdout.split("weakest interval:")[1].split("mm/day")[0])
    assert quoted == max(floors)


def test_the_detector_really_runs_and_finds_nothing():
    """
    The null must come from running the detector, not from printing "0". If
    this ever alarms on the committed series, the demo is lying.
    """
    out = run()
    assert "blocks examined   4" in out.stdout
    assert "alarms raised     0" in out.stdout


def test_the_headline_ratio_is_derived_not_typed():
    """
    The printed multiple must be the printed floors divided by the precursor,
    line of sight against line of sight, at both ends of the estimator
    bracket - and it must agree with the table bound.py writes. The old
    headline divided a downslope bound by a line-of-sight rate, and carried an
    upper end from a candidate with no valid offsets at all.
    """
    import csv
    from bound import PRECURSOR_MM_DAY
    out = run()
    span = out.stdout.split("what NISAR offsets saw")[1].split("mm/day")[0]
    lo, hi = (float(x) for x in span.strip().split(" - "))
    mult = out.stdout.split("was about")[1].split("too")[0]
    m_lo, m_hi = (float(x.strip().rstrip("x")) for x in mult.split(" to "))
    assert abs(m_lo - lo / PRECURSOR_MM_DAY) < 1.0
    assert abs(m_hi - hi / PRECURSOR_MM_DAY) < 1.0
    assert hi > lo, "the bound must be reported as a range, not a point"
    row = next(r for r in csv.DictReader(open(ROOT / "outputs" / "bound_source.csv"))
               if r["matches_published_elevation"] == "True")
    assert float(row["ratio_los_pixel"]) == pytest.approx(m_hi, abs=1.0)
    assert float(row["ratio_los_block"]) == pytest.approx(m_lo, abs=1.0)


def test_no_single_candidate_is_called_the_scar():
    """Four candidates survive the optical evidence. The demo must show all four."""
    out = run()
    assert "mapped scar" not in out.stdout
    assert out.stdout.count("candidate ") >= 4
