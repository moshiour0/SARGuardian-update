"""
inverse_velocity.py
-------------------
Fukuzono inverse-velocity failure forecasting on a measured displacement
series. This is the step that turns the pipeline into an alarm.

The method
==========
For accelerating creep to failure, Voight's material-failure law

    dv/dt = A * v^alpha

with alpha = 2 integrates to a straight line in inverse velocity:

    1/v(t) = A * (T - t)

so 1/v falls linearly and crosses zero at the failure time T. Fit the trailing
window, extrapolate the x-intercept, and that is a predicted failure date.
Fukuzono (1985); used operationally in open-pit mine slope monitoring.

Why this is not the same code as detectability.py
=================================================
That module runs the detector on a *simulated* curve where the truth is known.
This one runs on measured data, which brings three problems the simulation does
not have:

1. Irregular sampling. Real pairs are 12 and 24 days, not a fixed cadence.
2. Disconnected blocks. Winter and summer have separate zeros and can never be
   joined, so each is forecast independently.
3. A noise floor. Without a significance gate an inverse-velocity fit will
   happily "predict" failure from three noise samples. Every velocity must
   clear a multiple of the measured floor before it is allowed into a fit.

That third point is the whole difference between a detector and a random
number generator. Measure the floor first with:

    python src/goff_reader.py --noise-floor data/nisar_l2/GOFF/2025-12_winter

Reporting a non-detection
=========================
When no alarm fires the tool says which gate stopped it, and computes the
velocity that WOULD have been required. A null result with that number
attached is publishable; a null without it is just silence.

Usage
-----
    python src/inverse_velocity.py --ts outputs/ts_goff.csv --noise-floor 75
    python src/inverse_velocity.py --ts outputs/ts_gunw.csv --noise-floor 5 \
        --event-date 2026-08-26 --plot outputs/inv_velocity.png
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("inv-velocity")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import resolve  # noqa: E402


# ---------------------------------------------------------------------------
def load_series(path: Path) -> dict[tuple[str, int], list[dict]]:
    series: dict[tuple[str, int], list[dict]] = defaultdict(list)
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            series[(row["geometry"], int(row["component"]))].append({
                "epoch": datetime.strptime(row["epoch"], "%Y-%m-%d").date(),
                "cumulative_mm": float(row["cumulative_mm"]),
                "error_mm": float(row["error_mm"]) if row.get("error_mm") else None,
            })
    for v in series.values():
        v.sort(key=lambda r: r["epoch"])
    return dict(series)


def load_floors(path: Path, layer: str | None = None) -> dict[tuple[date, date], float]:
    """
    Per-pair detection floors from a goff_reader --csv summary.

    Why this exists
    ---------------
    One scalar floor across a stack whose per-pair floors vary twelvefold is not
    a threshold, it is an average of thresholds, and it is wrong in both
    directions at once. Measured over the source zone, layer2 floors run from
    8.5 to 117.9 mm/day. Gating every interval against the median manufactures
    excursions on the noisy geometry and hides real ones on the quiet geometry.

    The concrete failure: descending 2026-06-29 -> 2026-07-11 reads +19.60
    mm/day and clears a global 18.6 mm/day gate at 1.05x - while that pair's own
    3-sigma floor is 114.3 mm/day, against which the same velocity is 0.17x and
    plainly inside the noise.

    Keyed by (reference, secondary) so it joins straight onto a velocity
    interval, whose endpoints are the two acquisition dates.
    """
    floors: dict[tuple[date, date], float] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if layer and row.get("layer") and layer not in row["layer"]:
                continue
            raw = row.get("detect_floor_mm_day")
            if not raw:
                continue
            # A string comparison against ("nan", "inf") misses "NaN", "-inf",
            # "1e400" and anything else that parses to a non-finite float. An
            # infinite floor would put every velocity below the gate and
            # manufacture a non-detection silently - the one direction of error
            # this project cannot afford, since it is the direction of the
            # published conclusion.
            try:
                probe = float(raw)
            except ValueError:
                continue
            if not math.isfinite(probe) or probe <= 0:
                continue
            try:
                a = datetime.strptime(row["reference"], "%Y%m%d").date()
                b = datetime.strptime(row["secondary"], "%Y%m%d").date()
            except (KeyError, ValueError):
                continue
            f = float(raw)
            # A pair can appear twice - routine and urgent processing of the
            # same acquisitions. Keep the larger floor: a bound must not be
            # improved by reprocessing the same data.
            floors[(a, b)] = max(f, floors.get((a, b), 0.0))
    return floors


def velocities(rows: list[dict], floors: dict | None = None) -> list[dict]:
    """Mid-interval velocity between consecutive epochs."""
    out = []
    for a, b in zip(rows, rows[1:]):
        dt = (b["epoch"] - a["epoch"]).days
        if dt <= 0:
            continue
        out.append({
            "t0": a["epoch"], "t1": b["epoch"], "days": dt,
            "mid": a["epoch"] + timedelta(days=dt / 2),
            "v_mm_day": (b["cumulative_mm"] - a["cumulative_mm"]) / dt,
            "floor": (floors or {}).get((a["epoch"], b["epoch"])),
        })
    return out


# Two-sided 95% Student-t critical values by degrees of freedom. A lookup
# table rather than scipy, which this project deliberately does not depend on.
# dof 1 is the default window=3 case, and its value of 12.7 is the honest
# reason a three-point forecast rarely bounds anything.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
        13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
        19: 2.093, 20: 2.086, 25: 2.060, 30: 2.042}


def t_crit_95(dof: int) -> float:
    if dof <= 0:
        return float("nan")
    if dof in _T95:
        return _T95[dof]
    if dof > 30:
        return 1.960
    return _T95[max(k for k in _T95 if k < dof)]


def fieller_intercept_ci(t: np.ndarray, y: np.ndarray, a: float, b: float,
                         ss_res: float) -> tuple:
    """
    Exact 95% confidence interval for the x-intercept -b/a, by Fieller's theorem.

    Why not the delta method
    ------------------------
    The x-intercept is a RATIO of two correlated estimates, so its sampling
    distribution is heavy-tailed - Cauchy-like when the slope is poorly
    determined - and has no finite variance in that regime. First-order
    propagation reports a small symmetric sigma there anyway, which is a
    confident-looking number for a date the data does not constrain at all.
    With the default window of 3 the residual variance carries ONE degree of
    freedom, which is exactly the regime where that failure occurs.

    Fieller inverts the test |a*theta + b| <= t_crit * se(a*theta + b), giving
    a quadratic in theta. When the leading coefficient a^2 - t_crit^2*Var(a) is
    not positive the slope is not resolved from zero at 95%, the interval is
    unbounded, and the honest report is "not bounded" rather than a number.

    Returns (low, high, bounded).
    """
    n = len(t)
    dof = n - 2
    if dof < 1 or a == 0:
        return (float("nan"), float("nan"), False)
    tc = t_crit_95(dof)
    X = np.vstack([t, np.ones_like(t)]).T
    try:
        xtx_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return (float("nan"), float("nan"), False)
    s2 = ss_res / dof
    v_aa = s2 * xtx_inv[0, 0]
    v_bb = s2 * xtx_inv[1, 1]
    v_ab = s2 * xtx_inv[0, 1]

    A = a * a - tc * tc * v_aa
    B = 2.0 * (a * b - tc * tc * v_ab)
    C = b * b - tc * tc * v_bb
    if A <= 0:
        return (float("nan"), float("nan"), False)   # slope not resolved
    disc = B * B - 4.0 * A * C
    if disc < 0:
        return (float("nan"), float("nan"), False)
    root = math.sqrt(disc)
    lo, hi = (-B - root) / (2.0 * A), (-B + root) / (2.0 * A)
    return (min(lo, hi), max(lo, hi), True)


def fit_inverse_velocity(win: list[dict]) -> dict:
    """
    Least-squares 1/v against time, with the x-intercept and a Fieller interval.

    t_f = -b/a for the line 1/v = a*t + b. The interval is exact for a ratio
    (see fieller_intercept_ci) rather than first-order propagation, which
    understates it badly at the small windows this tool runs on.
    """
    t = np.array([(w["mid"] - win[0]["mid"]).days for w in win], dtype=float)
    # SPEED, not signed velocity. Fukuzono was written for an extensometer
    # aligned with the movement, where velocity is positive by construction.
    # A line-of-sight velocity is signed, and which sign means downslope
    # depends entirely on the look geometry - so 1/v built from the signed
    # value is negative for half the world's slopes, rises toward zero instead
    # of falling to it, and is rejected by the slope test below.
    v = np.abs(np.array([w["v_mm_day"] for w in win], dtype=float))
    inv = 1.0 / v

    n = len(t)
    A = np.vstack([t, np.ones_like(t)]).T
    coef, *_ = np.linalg.lstsq(A, inv, rcond=None)
    a, b = float(coef[0]), float(coef[1])

    pred = A @ coef
    ss_res = float(np.sum((inv - pred) ** 2))
    ss_tot = float(np.sum((inv - inv.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    t_fail_rel = -b / a if a != 0 else float("nan")
    sigma_tf = float("nan")
    if n > 2 and a != 0:
        s2 = ss_res / (n - 2)
        try:
            cov = s2 * np.linalg.inv(A.T @ A)
            # d(t_f)/da = b/a^2 ,  d(t_f)/db = -1/a
            g = np.array([b / a**2, -1.0 / a])
            var = float(g @ cov @ g)
            sigma_tf = float(np.sqrt(var)) if var > 0 else float("nan")
        except np.linalg.LinAlgError:
            pass

    ci_lo, ci_hi, bounded = fieller_intercept_ci(t, inv, a, b, ss_res)
    d0 = win[0]["mid"]
    return {"slope": a, "intercept": b, "r2": r2,
            "t_fail_days_from_window_start": t_fail_rel,
            "t_fail_date": d0 + timedelta(days=t_fail_rel)
                           if np.isfinite(t_fail_rel) else None,
            "sigma_days": sigma_tf, "n": n,
            "ci_bounded": bounded,
            "ci_low_date": d0 + timedelta(days=ci_lo) if bounded else None,
            "ci_high_date": d0 + timedelta(days=ci_hi) if bounded else None,
            "ci_width_days": (ci_hi - ci_lo) if bounded else float("nan")}


def _spans(ws) -> list[tuple]:
    """(reference, secondary) for each interval, so callers can see what a fit used."""
    return [(w["t0"], w["t1"]) for w in ws]


# ---------------------------------------------------------------------------
def analyse_block(label: str, comp: int, rows: list[dict], noise_floor: float,
                  window: int, r2_min: float, horizon_days: float,
                  sig_multiple: float, event_date: date | None,
                  floors: dict | None = None, cutoff: date | None = None) -> dict:
    print(f"\n{'='*74}")
    print(f"{label}  block {comp}   {rows[0]['epoch']} .. {rows[-1]['epoch']}  "
          f"({len(rows)} epochs)")
    print("=" * 74)

    vs = velocities(rows, floors)
    if not vs:
        print("  no velocity estimates possible")
        return {"alarm": False, "reason": "no intervals",
                "dropped": [], "intervals": []}

    # A forecast may only use data that existed before the thing it forecasts.
    #
    # This is not a hypothetical. `--event-date` used to score the prediction
    # and nothing else, so an interval whose SECOND acquisition came after the
    # collapse went straight into the fit. On a real failure that interval
    # carries the collapse itself - metres of apparent offset - so it clears
    # any floor, supplies the third velocity the fit needs, and the detector
    # announces an alarm with a lead time. Reproduced on synthetic data: three
    # pre-event intervals gave NO ALARM at 2 usable velocities; adding the
    # event-spanning interval produced "predicted failure 2026-08-31, lead 6
    # days, prediction error +5 days". Every number in that line was hindsight.
    #
    # The midpoint labelling hid it. The fit reported "ending 2026-08-25",
    # which is the midpoint of an interval running to 08-31, so the output
    # looked pre-event while resting on post-event data.
    dropped: list[tuple] = []
    if cutoff is not None:
        leaked = [w for w in vs if w["t1"] >= cutoff]
        dropped = [(w["t0"], w["t1"]) for w in leaked]
        if leaked:
            vs = [w for w in vs if w["t1"] < cutoff]
            print(f"\n  FORECAST CUTOFF {cutoff}: dropped {len(leaked)} interval(s)")
            for w in leaked:
                print(f"    {w['t0']} -> {w['t1']}  {w['v_mm_day']:+.2f} mm/day"
                      f"   ends on or after the cutoff")
            print("    A forecast cannot use an observation of the event it")
            print("    forecasts. Pass --allow-post-event to override, and say so.")
        if not vs:
            print("\n  nothing left before the cutoff")
            return {"alarm": False, "reason": "all intervals post-cutoff",
                    "dropped": dropped, "intervals": []}

    # Each interval is gated against the floor of the pair that produced it.
    # Where no per-pair floor is available the scalar falls back in, and the
    # output says so rather than pretending otherwise.
    for w in vs:
        w["gate"] = sig_multiple * (w["floor"] if w["floor"] is not None else noise_floor)
        w["gate_is_own"] = w["floor"] is not None

    n_own = sum(1 for w in vs if w["gate_is_own"])
    if n_own:
        gates = [w["gate"] for w in vs if w["gate_is_own"]]
        print(f"\n  significance gate: per pair, {sig_multiple:g} x that pair's own "
              f"measured floor")
        print(f"  {n_own} of {len(vs)} intervals have one; gates run "
              f"{min(gates):.1f} to {max(gates):.1f} mm/day"
              + (f", the rest fall back to {sig_multiple * noise_floor:.1f}"
                 if n_own < len(vs) else "") + "\n")
    else:
        print(f"\n  significance gate: |v| must exceed {sig_multiple:g} x {noise_floor:g} "
              f"= {sig_multiple * noise_floor:.1f} mm/day  (no per-pair floors given)\n")

    print(f"  {'INTERVAL':<26}{'DAYS':>6}{'v mm/day':>11}{'GATE':>9}{'x GATE':>8}   STATUS")
    print("  " + "-" * 72)
    for w in vs:
        sig = abs(w["v_mm_day"]) > w["gate"]
        ratio = abs(w["v_mm_day"]) / w["gate"] if w["gate"] else float("inf")
        status = ("above floor" if sig else "below floor") + ("" if w["gate_is_own"] else "  (fallback)")
        print(f"  {str(w['t0'])+' -> '+str(w['t1']):<26}{w['days']:>6}"
              f"{w['v_mm_day']:>11.2f}{w['gate']:>9.1f}{ratio:>8.2f}   {status}")

    # Magnitude, not sign. Downslope motion projects NEGATIVE into the line of
    # sight on a west-facing slope viewed from ascending - the dominant
    # configuration at this site, where sensitivity is -0.908. Gating on
    # `v > threshold` discarded precisely the signal the detector exists to
    # find, and did it silently: on null data a detector that cannot alarm and
    # one that correctly finds nothing produce identical output.
    usable = [w for w in vs if abs(w["v_mm_day"]) > w["gate"]]

    # A failing slope does not reverse. Mixing signs inside one fit window
    # would let noise either side of zero masquerade as acceleration, so keep
    # the longest run of consistent direction.
    if usable:
        runs, cur = [], [usable[0]]
        for a, b in zip(usable, usable[1:]):
            if (a["v_mm_day"] > 0) == (b["v_mm_day"] > 0):
                cur.append(b)
            else:
                runs.append(cur); cur = [b]
        runs.append(cur)
        # Ties go to the MOST RECENT run, not the earliest. `max` returns the
        # first maximum, which on a tie preferred a stale excursion months back
        # over the one happening now - the wrong way round for a slope said to
        # be accelerating toward failure.
        longest = max(reversed(runs), key=len)
        if len(longest) < len(usable):
            print(f"\n  {len(usable)} intervals clear the floor but change direction; "
                  f"keeping the longest\n  consistent run of {len(longest)}. A slope "
                  f"approaching failure does not reverse.")
        usable = longest

    print(f"\n  {len(usable)} of {len(vs)} intervals clear the floor")

    # ---- gates, in order, each with a stated reason -----------------------
    if len(usable) < window:
        need = window - len(usable)
        print(f"\n  NO ALARM - only {len(usable)} usable velocities, the fit needs "
              f"{window}. Short by {need}.")
        wmax = max(vs, key=lambda w: abs(w["v_mm_day"]))
        vmax = wmax["v_mm_day"]
        print(f"  fastest interval measured: {vmax:+.2f} mm/day "
              f"({abs(vmax)/wmax['gate']:.2f} x its own gate of "
              f"{wmax['gate']:.1f} mm/day)")
        print(f"  a detection here would have needed {window} consecutive intervals "
              f"each above their own gate.")
        return {"alarm": False, "reason": "insufficient velocities above noise floor",
                "max_velocity": vmax, "required": wmax["gate"],
                "gate_is_own": wmax["gate_is_own"],
                "dropped": dropped, "intervals": _spans(vs), "usable": _spans(usable)}

    # Earliest qualifying window, not the trailing one: the question this tool
    # answers is "when would this detector have fired", so it reports the first
    # moment the evidence was there rather than the most recent fit. Stated
    # here because the two give different lead times and the difference is not
    # visible in the output.
    best = None
    for k in range(window, len(usable) + 1):
        win = usable[k - window:k]
        fit = fit_inverse_velocity(win)
        if fit["slope"] >= 0:
            continue                      # 1/v must be falling
        if fit["r2"] < r2_min:
            continue
        if fit["t_fail_date"] is None:
            continue
        # Lead time runs from the LAST ACQUISITION in the window, not from the
        # midpoint of the last interval. The midpoint is 6 days earlier on a
        # 12-day pair and 12 on a 24-day one, so quoting it inflates the lead
        # by half the interval - and calling that "from the last observation"
        # is the same midpoint confusion that let post-event data into the fit
        # in the first place.
        last_obs = win[-1]["t1"]
        lead = (fit["t_fail_date"] - last_obs).days
        if not (0 < lead <= horizon_days):
            continue
        best = (win, fit, lead, last_obs)
        break

    if best is None:
        print(f"\n  NO ALARM - {len(usable)} usable velocities, but no window of "
              f"{window} met all of:")
        print(f"    1/v decreasing (acceleration), R2 >= {r2_min}, "
              f"predicted failure 0-{horizon_days:g} days ahead")
        return {"alarm": False, "reason": "no qualifying accelerating trend",
                "dropped": dropped, "intervals": _spans(vs), "usable": _spans(usable)}

    win, fit, lead, last_obs = best
    print(f"\n  *** ALARM ***")
    # Name the acquisition, not the midpoint. "ending 2026-08-25" for a window
    # whose last interval runs to 08-31 reads as pre-event while resting on
    # post-event data, which is precisely how the false alarm got published.
    print(f"    fitted on {fit['n']} velocities, last acquisition {last_obs}")
    print(f"    (interval midpoints {win[0]['mid']} .. {win[-1]['mid']}; the fit "
          f"is on midpoints,\n     the lead time is from the acquisition)")
    print(f"    1/v slope {fit['slope']:+.5f} per day, R2 {fit['r2']:.3f}")
    print(f"    predicted failure {fit['t_fail_date']}")
    if fit.get("ci_bounded"):
        print(f"    95% interval (Fieller) {fit['ci_low_date']} .. "
              f"{fit['ci_high_date']}  ({fit['ci_width_days']:.0f} days wide)")
    else:
        print(f"    95% interval (Fieller): UNBOUNDED - the 1/v slope is not "
              f"resolved from")
        print(f"      zero at 95% on {fit['n']} points, so the data does not "
              f"constrain a failure")
        print(f"      date. The point estimate above is a fitted value, not a "
              f"forecast.")
    if np.isfinite(fit["sigma_days"]):
        print(f"    (delta-method sigma {fit['sigma_days']:.1f} d, shown only "
              f"for comparison - it understates a ratio)")
    print(f"    lead time {lead} days from the last observation ({last_obs})")
    if event_date:
        err = (fit["t_fail_date"] - event_date).days
        print(f"    actual event {event_date}  ->  prediction error {err:+d} days")
    return {"alarm": True, "predicted": fit["t_fail_date"], "lead_days": lead,
            "last_observation": last_obs,
            "r2": fit["r2"], "sigma_days": fit["sigma_days"],
            "ci_bounded": fit.get("ci_bounded", False),
            "ci_low_date": fit.get("ci_low_date"),
            "ci_high_date": fit.get("ci_high_date"),
            "dropped": dropped, "intervals": _spans(vs), "usable": _spans(usable),
            "fitted": _spans(win)}


def plot(series, noise_floor, sig_multiple, out: Path, event_date=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.error("matplotlib required for --plot")
        return
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    for (geom, comp), rows in sorted(series.items()):
        lab = f"{geom} blk{comp}"
        ax1.plot([r["epoch"] for r in rows], [r["cumulative_mm"] for r in rows],
                 "o-", label=lab, alpha=.85)
        vs = velocities(rows)
        if vs:
            ax2.plot([w["mid"] for w in vs], [w["v_mm_day"] for w in vs],
                     "o-", label=lab, alpha=.85)
    thr = sig_multiple * noise_floor
    ax2.axhline(thr, color="crimson", ls="--", lw=1.2)
    ax2.axhline(-thr, color="crimson", ls="--", lw=1.2)
    ax2.text(ax2.get_xlim()[0], thr, f"  significance gate {thr:.0f} mm/day",
             color="crimson", va="bottom", fontsize=8)
    if event_date:
        for ax in (ax1, ax2):
            ax.axvline(event_date, color="k", ls=":", lw=1.2)
    ax1.set_ylabel("Cumulative displacement (mm)")
    ax1.set_title("Measured series and velocity against the detection floor", fontsize=12)
    ax2.set_ylabel("Velocity (mm/day)")
    for ax in (ax1, ax2):
        ax.grid(alpha=.3); ax.legend(fontsize=8)
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out, dpi=140)
    logger.info("Wrote %s", out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Inverse-velocity forecasting on measured data")
    ap.add_argument("--ts", required=True, help="CSV from timeseries.py --csv")
    ap.add_argument("--noise-floor", type=float, default=None,
                    help="mm/day, from goff_reader.py --noise-floor (GOFF) "
                         "or the phase ceiling analysis (GUNW). Used only for "
                         "intervals with no per-pair floor - see --floors")
    ap.add_argument("--floors", metavar="STATS.csv",
                    help="goff_reader --csv summary. Gates each interval against "
                         "the floor of the pair that produced it, which is what "
                         "you want whenever per-pair floors vary")
    ap.add_argument("--floors-layer", default=None,
                    help="restrict --floors to one layer, e.g. layer2")
    ap.add_argument("--sig-multiple", type=float, default=1.0,
                    help="velocity must exceed this many times the floor")
    ap.add_argument("--window", type=int, default=3)
    ap.add_argument("--r2-min", type=float, default=0.70)
    ap.add_argument("--horizon", type=float, default=60.0)
    ap.add_argument("--event-date",
                    help="YYYY-MM-DD. Scores the prediction, AND becomes the "
                         "forecast cutoff: intervals ending on or after it are "
                         "dropped from the fit")
    ap.add_argument("--forecast-cutoff", metavar="YYYY-MM-DD",
                    help="drop intervals ending on or after this date. Defaults "
                         "to --event-date")
    ap.add_argument("--allow-post-event", action="store_true",
                    help="disable the cutoff. Only for studying the event pair "
                         "itself - never for a forecast claim")
    ap.add_argument("--plot", metavar="OUT.png")
    args = ap.parse_args()

    ev = datetime.strptime(args.event_date, "%Y-%m-%d").date() if args.event_date else None
    cutoff = (datetime.strptime(args.forecast_cutoff, "%Y-%m-%d").date()
              if args.forecast_cutoff else ev)
    if args.allow_post_event:
        cutoff = None
    series = load_series(resolve(args.ts))
    if not series:
        logger.error("No rows in %s", args.ts); return 1

    floors: dict = {}
    if args.floors:
        floors = load_floors(resolve(args.floors), args.floors_layer)
        logger.info("Loaded %d per-pair floors from %s", len(floors), args.floors)

    # --noise-floor is only needed for intervals that have no per-pair floor.
    # When it is omitted and floors were supplied, fall back to the LARGEST of
    # them: a pair whose own floor is unknown must not be gated more leniently
    # than the noisiest pair that is known, or a missing measurement would
    # improve the bound.
    if args.noise_floor is None:
        if not floors:
            logger.error("Give --noise-floor, or --floors to supply per-pair floors")
            return 2
        args.noise_floor = max(floors.values())
        logger.info("No --noise-floor given; falling back to the largest "
                    "per-pair floor, %.1f mm/day", args.noise_floor)

    print(f"\n{len(series)} independent block(s) from {args.ts}")
    if floors:
        print(f"Per-pair floors from {args.floors}"
              + (f" (layer {args.floors_layer})" if args.floors_layer else "")
              + f", significance gate {args.sig_multiple:g}x")
        print(f"Fallback floor {args.noise_floor:g} mm/day where a pair has none")
    else:
        print(f"Noise floor {args.noise_floor:g} mm/day, "
              f"significance gate {args.sig_multiple:g}x  (no --floors given)")

    results = []
    for (geom, comp), rows in sorted(series.items()):
        results.append(analyse_block(geom, comp, rows, args.noise_floor,
                                     args.window, args.r2_min, args.horizon,
                                     args.sig_multiple, ev, floors, cutoff))

    print(f"\n{'='*74}\nSUMMARY\n{'='*74}")
    fired = [r for r in results if r.get("alarm")]
    if fired:
        for r in fired:
            ci = (f", 95% {r['ci_low_date']}..{r['ci_high_date']}"
                  if r.get("ci_bounded") else ", 95% UNBOUNDED")
            print(f"  ALARM: predicted {r['predicted']}, lead {r['lead_days']} d, "
                  f"R2 {r['r2']:.2f}{ci}")
    else:
        print(f"  No alarm in any of {len(results)} blocks.")
        for r in results:
            print(f"    - {r.get('reason')}")
        # Against the floor of the pair that produced it, not the global
        # scalar. Every block above gates per pair; this summary used to
        # recompute sig_multiple * args.noise_floor and divide by the scalar,
        # which reintroduced - in the last thing the reader sees - exactly the
        # defect --floors exists to remove. On this data that is the difference
        # between "1.05x the gate" and "0.17x the gate" for the same interval.
        cand = [r for r in results if "max_velocity" in r]
        if cand:
            r = max(cand, key=lambda r: abs(r["max_velocity"]) / max(r["required"], 1e-9))
            fastest, gate = r["max_velocity"], r["required"]
            basis = ("its own measured floor" if r.get("gate_is_own")
                     else f"the fallback floor {args.noise_floor:g} mm/day")
            print(f"\n  Fastest interval relative to its own gate: {fastest:+.2f} mm/day "
                  f"against {gate:.1f} mm/day\n  ({abs(fastest)/gate:.2f}x, gated on {basis})")
            print(f"  Detection required: that ratio above 1 across "
                  f"{args.window} consecutive intervals.")
            if not all(x.get("gate_is_own") for x in cand):
                print("  NOTE: at least one block had no per-pair floor and fell back "
                      "to the scalar.")
            # Two different failure modes, and saying which one is the point.
            # A single fast interval that exceeds the gate is not a shortfall in
            # magnitude - it is a shortfall in persistence, which is exactly how
            # atmospheric noise differs from creep. Reporting them the same way
            # would hide the distinction the whole method rests on.
            if abs(fastest) > gate:
                print(f"\n  Motion DID exceed the gate in at least one interval, but "
                      f"never for\n  {args.window} in a row. An isolated excursion "
                      f"that does not persist is the\n  signature of atmosphere, "
                      f"not of accelerating creep - and the series\n  returning to "
                      f"its starting value confirms it.")
            else:
                short = (gate / abs(fastest)) if fastest else float("inf")
                short_s = f"{short:.1f}x" if np.isfinite(short) else "infinitely"
                print(f"\n  No interval anywhere reached the gate; the fastest was "
                      f"{short_s} short.")
            print("\n  This is a bounded non-detection, not an absence of evidence:")
            print("  any precursor slower than the floor is invisible to this product,")
            print("  and that bound is the quotable result.")

    if args.plot:
        plot(series, args.noise_floor, args.sig_multiple, Path(args.plot), ev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
