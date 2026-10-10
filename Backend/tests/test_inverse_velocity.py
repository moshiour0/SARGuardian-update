"""
Does the detector detect?

The suite had 23 tests and not one of them ran the alarm. Every real dataset
returned "no alarm", which is the correct answer for a null result and also
the output of a detector that cannot alarm at all - the two are
indistinguishable without a positive control.

A referee found the difference. The gate read `v > threshold` on a SIGNED
line-of-sight velocity, so a slope moving toward the satellite was discarded
entirely. Downslope motion projects negative in ascending on a west-facing
slope, which is the dominant configuration at this site (sensitivity -0.908),
so the detector was blind to precisely the case it exists for.

These tests build a textbook Fukuzono failure - 1/v falling linearly to zero -
and require the same answer in both directions.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from inverse_velocity import analyse_block, fit_inverse_velocity, velocities

ROOT = Path(__file__).resolve().parent.parent
FAIL_DAY = 60.0
A = 0.004                       # 1/v = A * (T - t)


def failure_series(sign: int, days=(0, 6, 12, 18, 24, 30, 36, 42, 48)):
    """Voight/Fukuzono acceleration, projected into LOS with a given sign."""
    from datetime import date, timedelta
    start = date(2026, 6, 1)
    rows, cum, prev = [], 0.0, None
    for d in days:
        v = 1.0 / (A * (FAIL_DAY - d))
        if prev is not None:
            cum += sign * v * (d - prev)
        prev = d
        rows.append({"epoch": start + timedelta(days=d), "cumulative_mm": cum,
                     "error_mm": None})
    return rows


def run(rows, noise_floor=0.5):
    return analyse_block("TEST", 1, rows, noise_floor=noise_floor, window=3,
                         r2_min=0.8, horizon_days=120, sig_multiple=1.0,
                         event_date=None)


# ---------------------------------------------------------------------------
def test_detector_alarms_on_a_textbook_failure():
    """The positive control that never existed. If this fails, nothing else matters."""
    r = run(failure_series(+1))
    assert r["alarm"], "no alarm on a perfect Fukuzono acceleration"
    assert r["r2"] > 0.99


@pytest.mark.parametrize("sign", [+1, -1])
def test_alarm_does_not_depend_on_look_direction(sign):
    """
    Away from the satellite or toward it, the ground is doing the same thing.
    Which sign that produces is a property of the orbit, not of the slope.
    """
    r = run(failure_series(sign))
    assert r["alarm"], (
        f"sign {sign:+d} produced no alarm - the gate is reading signed velocity")


def test_both_directions_predict_the_same_failure_date():
    """A sign convention must not move the answer."""
    a = run(failure_series(+1))
    b = run(failure_series(-1))
    assert a["predicted"] == b["predicted"]
    assert a["lead_days"] == b["lead_days"]


def test_reported_peak_is_the_largest_magnitude():
    """
    `max()` on signed velocities reports the largest motion AWAY from the
    satellite and silently discards everything moving toward it. On the
    committed GOFF summer series that understated the peak by a factor of ten,
    -15.86 mm/day reported as +1.55.
    """
    rows = [{"epoch": d, "cumulative_mm": c, "error_mm": None} for d, c in
            zip(*[[__import__("datetime").date(2026, 7, x) for x in (1, 13, 25)],
                  [0.0, -190.0, -170.0]])]
    r = run(rows, noise_floor=100.0)          # floor high enough to block the fit
    assert not r["alarm"]
    assert r["max_velocity"] == pytest.approx(-15.833, abs=0.01), (
        f"reported {r['max_velocity']:+.2f}; the largest magnitude is -15.83")


def test_inverse_velocity_fit_uses_speed_not_signed_velocity():
    """
    1/v built from a negative velocity rises toward zero instead of falling to
    it, so the `slope < 0` test rejects a real failure. The fit has to work in
    speed and carry direction separately.
    """
    for sign in (+1, -1):
        rows = failure_series(sign)
        vs = velocities(rows)
        fit = fit_inverse_velocity(vs[-3:])
        assert fit["slope"] < 0, f"sign {sign:+d}: 1/v is not falling"
        assert fit["r2"] > 0.99


def test_direction_reversal_is_not_treated_as_acceleration():
    """
    Taking |v| everywhere would let noise either side of zero look like a
    consistent run. A slope approaching failure does not reverse.
    """
    from datetime import date, timedelta
    start = date(2026, 7, 1)
    cum, rows = 0.0, []
    for k, v in enumerate([0.0, +30.0, -30.0, +30.0, -30.0]):
        cum += v
        rows.append({"epoch": start + timedelta(days=12 * k),
                     "cumulative_mm": cum, "error_mm": None})
    r = run(rows, noise_floor=1.0)
    assert not r["alarm"], "alternating noise produced an alarm"


def test_cli_runs_end_to_end(tmp_path):
    """The module is used from the command line; that path must work too."""
    p = tmp_path / "ts.csv"
    rows = failure_series(-1)
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["geometry", "component", "epoch", "days_from_start",
                    "cumulative_mm", "error_mm"])
        for i, r in enumerate(rows):
            w.writerow(["DESC path 48", 1, r["epoch"], i,
                        round(r["cumulative_mm"], 3), ""])
    out = subprocess.run([sys.executable, str(ROOT / "src" / "inverse_velocity.py"),
                          "--ts", str(p), "--noise-floor", "0.5"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-500:]
    assert "ALARM" in out.stdout


# ---------------------------------------------------------------------------
# Per-pair gating.
#
# One scalar floor across a stack whose per-pair floors vary twelvefold is not a
# threshold, it is an average of thresholds. Measured on the source zone, the
# descending interval 2026-06-29 -> 2026-07-11 reads +19.60 mm/day and clears a
# global 18.6 mm/day gate at 1.05x, while that pair's own 3-sigma floor is
# 114.3 mm/day - against which the same number is 0.17x, plainly inside the
# noise. A gate that manufactures excursions on the noisy geometry is worse
# than no gate, because it looks like a measurement.
# ---------------------------------------------------------------------------
import csv as _csv
from datetime import date as _date

from inverse_velocity import load_floors, velocities


def _series(pairs):
    """[(epoch, cumulative_mm), ...] -> rows in load_series shape."""
    return [{"epoch": e, "cumulative_mm": v, "error_mm": None} for e, v in pairs]


def test_each_interval_carries_the_floor_of_the_pair_that_produced_it():
    rows = _series([(_date(2026, 6, 29), 0.0),
                    (_date(2026, 7, 11), 235.1),
                    (_date(2026, 7, 23), 138.8)])
    floors = {(_date(2026, 6, 29), _date(2026, 7, 11)): 114.3,
              (_date(2026, 7, 11), _date(2026, 7, 23)): 86.2}
    vs = velocities(rows, floors)
    assert [w["floor"] for w in vs] == [114.3, 86.2]
    # and the first interval, which clears a 18.6 global gate, does not clear
    # its own
    assert abs(vs[0]["v_mm_day"]) > 18.6
    assert abs(vs[0]["v_mm_day"]) < vs[0]["floor"]


def test_an_interval_with_no_measured_floor_reports_none():
    rows = _series([(_date(2026, 6, 29), 0.0), (_date(2026, 7, 11), 10.0)])
    assert velocities(rows, {})[0]["floor"] is None
    assert velocities(rows)[0]["floor"] is None


def test_duplicate_processings_keep_the_larger_floor(tmp_path):
    """
    Routine and urgent processing of the same acquisitions both appear in the
    stats CSV. A bound must not be improved by reprocessing the same data, so
    the pessimistic floor wins.
    """
    p = tmp_path / "goff.csv"
    with open(p, "w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["file", "layer", "reference",
                                            "secondary", "detect_floor_mm_day"])
        w.writeheader()
        w.writerow({"file": "..._PR_...", "layer": "HH/layer2",
                    "reference": "20260816", "secondary": "20260828",
                    "detect_floor_mm_day": "85.6"})
        w.writerow({"file": "..._UR_...", "layer": "HH/layer2",
                    "reference": "20260816", "secondary": "20260828",
                    "detect_floor_mm_day": "44.6"})
    got = load_floors(p)
    assert got[(_date(2026, 8, 16), _date(2026, 8, 28))] == 85.6


# ---------------------------------------------------------------------------
from datetime import date, timedelta  # noqa: E402

# Hindsight must not reach the fit.
#
# `--event-date` used to score the prediction and nothing else, so an interval
# whose SECOND acquisition fell after the collapse went straight into the fit.
# On a real failure that interval carries the collapse - metres of apparent
# offset - so it clears any floor, supplies the third velocity the fit needs,
# and the detector announces an alarm with a lead time. The whole thesis of
# this project is that detecting a collapse and predicting one are different
# problems; a forecast built on the event is the exact confusion it argues
# against, dressed as a success.
#
# The midpoint labelling hid it: the fit reported "ending 2026-08-25", the
# midpoint of an interval running to 08-31, so the output read as pre-event.

def _leaky_block():
    """Two usable pre-event velocities, then one that spans the event."""
    base = date(2026, 7, 2)
    cum = [0.0, -36.0, -336.0, -1296.0, -3696.0]        # 3, 25, 40, 200 mm/day
    days = [0, 12, 24, 48, 60]
    return [{"epoch": base + timedelta(days=d), "cumulative_mm": c}
            for d, c in zip(days, cum)]


def test_post_event_interval_is_dropped_from_the_fit():
    rows = _leaky_block()
    leaked = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0,
                           date(2026, 8, 26), None, cutoff=None)
    guarded = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0,
                            date(2026, 8, 26), None, cutoff=date(2026, 8, 26))
    # Without the cutoff the event-spanning interval reaches the fit: it
    # carries the collapse itself, so it clears any floor and supplies the
    # third velocity. That is the leak, and it is what the cutoff exists to
    # stop - assert on the mechanism, not on whether an alarm happened to fire.
    event_span = (date(2026, 8, 19), date(2026, 8, 31))
    assert event_span in leaked["usable"], (
        "fixture must let the event-spanning interval into the fit")
    assert not leaked["dropped"]

    assert event_span in guarded["dropped"], "the cutoff must drop it"
    assert event_span not in guarded.get("usable", [])
    assert not guarded["alarm"]

    # Second line of defence, and it is independent of the cutoff. Measured
    # honestly - from the last ACQUISITION rather than an interval midpoint -
    # the leaked fit predicts a failure that had already happened by the time
    # of the last observation it used, so it is not a forecast at all.
    assert not leaked["alarm"], (
        "a fit resting on the event should not produce a forward-looking "
        "prediction once lead time is measured from the acquisition")


def test_the_cutoff_keeps_everything_before_the_event():
    """It must drop the leaking interval, not the whole block."""
    rows = _leaky_block()
    r = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0,
                      date(2026, 8, 26), None, cutoff=date(2026, 8, 26))
    assert r.get("reason") != "all intervals post-cutoff"


def test_an_interval_ending_exactly_on_the_event_is_excluded():
    """
    An acquisition on the day of the collapse may already contain it. The
    boundary is >=, not >, and that choice is deliberate.

    The fixture has to be built so the boundary is what decides. Three usable
    velocities are needed for a fit, and the third one is the interval landing
    exactly on the event date - so `>=` leaves two and refuses, while `>` lets
    it through and alarms. An earlier version of this test used only two usable
    velocities and passed either way; the mutation harness caught it.
    """
    base = date(2026, 7, 2)
    days = [0, 12, 24, 36, 55]           # last epoch is 2026-08-26 itself
    cum = [0.0, -36.0, -336.0, -816.0, -4616.0]   # 3, 25, 40, 200 mm/day
    rows = [{"epoch": base + timedelta(days=d), "cumulative_mm": c}
            for d, c in zip(days, cum)]
    assert rows[-1]["epoch"] == date(2026, 8, 26)

    boundary = (date(2026, 8, 7), date(2026, 8, 26))

    loose = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0,
                          date(2026, 8, 26), None, cutoff=None)
    assert boundary in loose["usable"], (
        "with an exclusive boundary the interval landing on the event day "
        "reaches the fit - which is the thing being guarded against")

    r = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0,
                      date(2026, 8, 26), None, cutoff=date(2026, 8, 26))
    assert boundary in r["dropped"], "an acquisition on the day may contain the event"
    assert boundary not in r.get("usable", [])
    assert not r["alarm"]


def test_no_cutoff_leaves_behaviour_unchanged():
    """Passing cutoff=None must reproduce the old path exactly."""
    rows = _leaky_block()
    a = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0, None, None)
    b = analyse_block("ASC", 1, rows, 18.6, 3, 0.8, 60.0, 1.0, None, None,
                      cutoff=None)
    assert a["alarm"] == b["alarm"]


# ---------------------------------------------------------------------------
# Lead time is measured from an acquisition, never from an interval midpoint
# ---------------------------------------------------------------------------
def test_lead_time_runs_from_the_last_acquisition_not_the_midpoint():
    """
    A velocity sits at the midpoint of the interval that produced it, but the
    last thing actually OBSERVED is that interval's second acquisition. The
    alarm used to compute `t_fail - mid` and print it as "lead time N days from
    the last observation", which inflates the lead by half the interval - six
    days on a 12-day pair, twelve on a 24-day one.

    That is the same midpoint confusion that let post-event data into the fit
    and produced a "successful forecast" of an event already observed. It is
    worth pinning in the one place where it silently flatters the result.
    """
    rows = failure_series(+1)
    r = run(rows)
    assert r["alarm"]

    vs = velocities(rows)
    closing = next(w for w in vs if w["t1"] == r["last_observation"])
    assert closing["t1"] > closing["mid"], (
        "an acquisition is later than the midpoint of the interval it closes")
    last = closing

    expected = (r["predicted"] - r["last_observation"]).days
    assert r["lead_days"] == expected

    # And the midpoint version really is longer, so the two cannot be confused
    # for one another by accident.
    from_mid = (r["predicted"] - last["mid"]).days
    assert from_mid > r["lead_days"]


def test_lead_time_shrinks_as_the_closing_interval_lengthens():
    """
    Two series ending on the same acquisition, one closed by a long interval.
    Measured from the acquisition the lead is identical; measured from the
    midpoint the long interval would appear to buy days of extra warning it
    did not buy.
    """
    short = run(failure_series(+1, days=(0, 6, 12, 18, 24, 30, 36, 42, 48)))
    long_ = run(failure_series(+1, days=(0, 6, 12, 18, 24, 30, 36, 48)))
    assert short["alarm"] and long_["alarm"]
    if short["last_observation"] == long_["last_observation"]:
        assert short["lead_days"] == long_["lead_days"]


# ---------------------------------------------------------------------------
# The summary must gate the same way the table does
# ---------------------------------------------------------------------------
def test_summary_reports_against_the_pairs_own_floor(tmp_path, capsys):
    """
    Every block gates per pair. The SUMMARY used to recompute
    sig_multiple * noise_floor and divide the fastest velocity by the scalar,
    reintroducing the exact defect --floors was written to remove - in the last
    thing the reader sees.

    The concrete case: descending 2026-06-29 -> 2026-07-11 reads +19.60 mm/day.
    Against a global 18.6 mm/day gate that is 1.05x and looks like motion.
    Against its own 114.3 mm/day floor it is 0.17x and is plainly noise.
    """
    import subprocess as sp

    ts = tmp_path / "ts.csv"
    with open(ts, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["geometry", "component", "epoch", "days_from_start",
                    "cumulative_mm", "error_mm"])
        for epoch, cum in (("2026-06-29", 0.0), ("2026-07-11", 235.147),
                           ("2026-07-23", 138.849)):
            w.writerow(["DESC path 48", 2, epoch, 0, cum, ""])

    floors = tmp_path / "floors.csv"
    with open(floors, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["reference", "secondary", "layer", "detect_floor_mm_day"])
        w.writerow(["20260629", "20260711", "HH/layer2", "114.31"])
        w.writerow(["20260711", "20260723", "HH/layer2", "86.24"])

    out = sp.run([sys.executable, str(ROOT / "src" / "inverse_velocity.py"),
                  "--ts", str(ts), "--floors", str(floors),
                  "--floors-layer", "layer2", "--noise-floor", "18.6"],
                 capture_output=True, text=True, check=True).stdout

    assert "SUMMARY" in out
    summary = out.split("SUMMARY")[1]
    assert "0.17x" in summary, (
        "the summary must rate +19.60 mm/day against its own 114.3 mm/day "
        f"floor, not against the 18.6 scalar. Got:\n{summary}")
    assert "1.05x" not in summary
    assert "its own measured floor" in summary


def test_summary_says_so_when_it_falls_back_to_the_scalar(tmp_path):
    """A fallback that is not announced is indistinguishable from a measurement."""
    import subprocess as sp

    ts = tmp_path / "ts.csv"
    with open(ts, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["geometry", "component", "epoch", "days_from_start",
                    "cumulative_mm", "error_mm"])
        for epoch, cum in (("2026-06-29", 0.0), ("2026-07-11", 10.0),
                           ("2026-07-23", 20.0)):
            w.writerow(["DESC path 48", 2, epoch, 0, cum, ""])

    out = sp.run([sys.executable, str(ROOT / "src" / "inverse_velocity.py"),
                  "--ts", str(ts), "--noise-floor", "18.6"],
                 capture_output=True, text=True, check=True).stdout
    assert "fallback floor" in out.lower() or "no per-pair floors" in out.lower()


# ---------------------------------------------------------------------------
# The failure date is a ratio, and a ratio needs a Fieller interval.
# ---------------------------------------------------------------------------
def test_a_three_point_fit_on_noise_does_not_bound_a_failure_date():
    """
    The x-intercept is -b/a, a ratio of correlated estimates. When the slope
    is not resolved from zero the interval is genuinely unbounded, and the
    delta method reports a small symmetric sigma anyway. With window=3 the
    residual variance has ONE degree of freedom, which is exactly that regime.
    Reporting a confident date there is the failure mode this guards.
    """
    from inverse_velocity import fieller_intercept_ci
    t = np.array([0.0, 12.0, 24.0])
    y = np.array([0.050, 0.049, 0.051])          # flat: no acceleration
    a, b = np.polyfit(t, y, 1)
    ss_res = float(np.sum((y - (a * t + b)) ** 2))
    lo, hi, bounded = fieller_intercept_ci(t, y, float(a), float(b), ss_res)
    assert bounded is False


def test_a_clean_acceleration_does_bound_a_failure_date():
    """The interval must not be unbounded for every input, or it says nothing."""
    from inverse_velocity import fieller_intercept_ci
    t = np.array([0.0, 12.0, 24.0, 36.0, 48.0])
    # Falling hard, with a little scatter - a noiseless line makes the Fieller
    # discriminant exactly zero and the test would only be probing rounding.
    y = 0.10 - 0.0018 * t + np.array([2e-4, -1e-4, 1e-4, -2e-4, 1e-4])
    a, b = np.polyfit(t, y, 1)
    ss_res = float(np.sum((y - (a * t + b)) ** 2))
    lo, hi, bounded = fieller_intercept_ci(t, y, float(a), float(b), ss_res)
    assert bounded is True
    assert lo < (-b / a) < hi


def test_the_t_table_is_not_the_normal_approximation_at_small_dof():
    """
    1 dof is 12.7, not 1.96. Using the normal there would shrink every
    interval by a factor of six and manufacture bounded forecasts.
    """
    from inverse_velocity import t_crit_95
    assert t_crit_95(1) > 12.0
    assert t_crit_95(2) > 4.0
    assert abs(t_crit_95(500) - 1.96) < 0.01


# ---------------------------------------------------------------------------
# A non-finite floor must never reach the gate.
# ---------------------------------------------------------------------------
def test_a_non_finite_floor_is_rejected_not_parsed(tmp_path):
    """
    An infinite floor puts every velocity below the gate and manufactures a
    non-detection silently - the direction of the published conclusion, which
    is the one direction this project cannot afford to fail in. The old guard
    compared the raw string against ("nan", "inf") and missed "NaN" and "-inf".
    """
    from inverse_velocity import load_floors
    p = tmp_path / "floors.csv"
    p.write_text(
        "reference,secondary,layer,detect_floor_mm_day\n"
        "20260702,20260714,HH/layer2,NaN\n"
        "20260714,20260726,HH/layer2,-inf\n"
        "20260726,20260819,HH/layer2,Infinity\n"
        "20260819,20260831,HH/layer2,15.6\n", encoding="utf-8")
    floors = load_floors(p, "layer2")
    assert len(floors) == 1
    assert all(np.isfinite(v) and v > 0 for v in floors.values())
