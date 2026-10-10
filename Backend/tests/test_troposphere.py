"""
The stratified troposphere fit must not be steered by outliers.

Why this exists
===============
The first version of this fit used plain least squares, and it produced a
result that looked right and was not. On one winter pair it reported a slope of
-23.6 mm/km, removed it, and cut the standard deviation from 39.4 to 34.3 - the
number least squares is built to reduce. At the same time the MAD scatter of the
core, which is the statistic every other noise figure in this project uses,
went UP from 13.8 to 22.2. The fit was pulling the bulk of the field apart in
order to serve a minority of gross outliers, and the summary line said the
correction was working.

Two things had to hold before the tool could be trusted:

  1. a slope fitted on clean data must survive the addition of gross outliers,
     because unculled SAR products always contain them, and
  2. when the fit IS extrapolated - most valid pixels in a narrow elevation
     band, the line reaching far past it - the tool must say so rather than
     silently applying it.

These tests hold both, plus the sign and units of the removal itself.
"""

from __future__ import annotations

import numpy as np

from troposphere import (LEVERAGE_WARN, MIN_SAMPLES, fit_elevation_trend,
                         remove_trend, robust_sigma, scatter)


def _field(slope_mm_per_km=10.0, n=400, noise=1.0, seed=0):
    """Elevation in metres and a displacement that is linear in it."""
    rng = np.random.default_rng(seed)
    elev = np.linspace(2000.0, 6000.0, n)
    disp = slope_mm_per_km * (elev / 1000.0) + rng.normal(0.0, noise, n)
    return elev, disp


def test_recovers_a_known_slope_in_mm_per_km():
    """The unit is mm per km of elevation, not mm per metre."""
    elev, disp = _field(slope_mm_per_km=12.5)
    fit = fit_elevation_trend(elev, disp)
    assert fit["usable"]
    assert abs(fit["slope_mm_per_km"] - 12.5) < 0.2
    # 4000 m of relief at 12.5 mm/km is 50 mm end to end. If the unit were
    # mm/m the slope would come back near 0.0125 and this would fail.
    assert abs(fit["r"]) > 0.99


def test_outliers_do_not_steer_the_slope():
    """
    The defect this module was rewritten for.

    Ten per cent of pixels are given a large, elevation-uncorrelated excursion -
    the decorrelation tails a real product carries. Plain least squares chases
    them; the robust fit must not.
    """
    elev, disp = _field(slope_mm_per_km=10.0, n=500, noise=1.0)
    rng = np.random.default_rng(7)
    bad = rng.choice(disp.size, size=50, replace=False)
    dirty = disp.copy()
    dirty[bad] += rng.normal(0.0, 300.0, bad.size)

    robust = fit_elevation_trend(elev, dirty, robust=True)
    plain = fit_elevation_trend(elev, dirty, robust=False)

    assert abs(robust["slope_mm_per_km"] - 10.0) < 1.5
    assert abs(robust["slope_mm_per_km"] - 10.0) < abs(plain["slope_mm_per_km"] - 10.0)
    assert robust["n_used"] < robust["n"]        # something was actually clipped


def test_removal_does_not_raise_the_core_scatter():
    """
    Removing the trend must improve the MAD, not only the standard deviation.

    This is the exact failure that was shipped: std fell while MAD rose. Assert
    on MAD, because that is the statistic the noise floors are built from.
    """
    elev, disp = _field(slope_mm_per_km=20.0, n=500, noise=2.0)
    rng = np.random.default_rng(3)
    bad = rng.choice(disp.size, size=50, replace=False)
    disp[bad] += rng.normal(0.0, 400.0, bad.size)

    fit = fit_elevation_trend(elev, disp)
    after = remove_trend(elev, disp, fit)
    assert robust_sigma(after) < robust_sigma(disp)


def test_leverage_is_flagged_when_the_fit_is_extrapolated():
    """
    Most pixels in a narrow band, a few far above: the slope is set by the few
    and applied to all. Range over IQR names it, and the tool must exceed the
    warning threshold here and stay under it on a well-spread field.
    """
    spread = np.linspace(2000.0, 6000.0, 400)
    clustered = np.concatenate([np.linspace(3800.0, 4100.0, 380),
                                np.linspace(4500.0, 6800.0, 20)])
    for e in (spread, clustered):
        f = fit_elevation_trend(e, 10.0 * (e / 1000.0))
        assert f["usable"]

    even = fit_elevation_trend(spread, 10.0 * (spread / 1000.0))
    lev = fit_elevation_trend(clustered, 10.0 * (clustered / 1000.0))
    assert even["leverage"] < LEVERAGE_WARN
    assert lev["leverage"] > LEVERAGE_WARN


def test_intercept_is_not_subtracted():
    """
    Only the gradient is removed. The constant is arbitrary for a relative
    measurement and taking it out would shift the mean, making any before/after
    scatter comparison meaningless.
    """
    elev, disp = _field(slope_mm_per_km=10.0, noise=0.0)
    disp = disp + 500.0                     # an arbitrary reference offset
    fit = fit_elevation_trend(elev, disp)
    after = remove_trend(elev, disp, fit)
    assert abs(np.mean(after) - 500.0) < 1.0


def test_too_few_samples_is_refused_not_guessed():
    elev = np.linspace(2000.0, 6000.0, MIN_SAMPLES - 1)
    fit = fit_elevation_trend(elev, 10.0 * (elev / 1000.0))
    assert not fit["usable"]
    assert np.isnan(fit["slope_mm_per_km"])


def test_flat_terrain_is_refused():
    """No relief, nothing to regress against - and no slope invented."""
    elev = np.full(200, 4000.0)
    rng = np.random.default_rng(1)
    fit = fit_elevation_trend(elev, rng.normal(0.0, 5.0, 200))
    assert not fit["usable"]


def test_nans_are_excluded_from_the_fit_population():
    elev, disp = _field(n=300, noise=0.5)
    disp[::3] = np.nan
    elev[1::7] = np.nan
    fit = fit_elevation_trend(elev, disp)
    assert fit["n"] == int((np.isfinite(elev) & np.isfinite(disp)).sum())
    assert abs(fit["slope_mm_per_km"] - 10.0) < 0.5


def test_scatter_reports_both_statistics():
    """
    std and MAD are returned together because their disagreement is the signal.
    On a heavy-tailed field std must be well above 1.48x the MAD.
    """
    rng = np.random.default_rng(11)
    core = rng.normal(0.0, 10.0, 1000)
    tails = np.concatenate([core, rng.normal(0.0, 400.0, 100)])
    sd, mad = scatter(tails)
    assert sd > 3.0 * mad
    sd_g, mad_g = scatter(core)
    assert abs(sd_g / mad_g - 1.0) < 0.2


# ---------------------------------------------------------------------------
# Sign reversals are a within-track statement, and must be scored against chance
# ---------------------------------------------------------------------------
def _row(track, ref, slope, proc="PR"):
    return {"file": f"GUNW_{ref}_x_{proc}.tif", "track": track,
            "reference": ref, "proc": proc, "slope_mm_per_km": slope}


def test_reversals_are_counted_within_a_track_not_across_the_file_list():
    """
    Rows arrive in `sorted(glob("*.tif"))` order, which is by date across ALL
    tracks - so the "consecutive 12-day pairs" being compared ran descending,
    ascending, descending, ascending. Two geometries project the same delay
    field differently; reading them as one sequence measures nothing.

    Here each track is individually perfectly stable, and interleaving them
    would report a reversal at every step.
    """
    from troposphere import sign_reversals
    rows = []
    for i, ref in enumerate(("20260101", "20260113", "20260125", "20260206")):
        rows.append(_row("A098", ref, +5.0))
        rows.append(_row("D048", ref, -5.0))

    flips, transitions, seqs = sign_reversals(rows)
    assert seqs == {"A098": "++++", "D048": "----"}
    assert flips == 0, "each track is stable; only the interleaving reverses"
    assert transitions == 6


def test_duplicate_processings_of_one_pair_count_once():
    """PR and UR are the same two images. Two rows, one observation."""
    from troposphere import sign_reversals
    rows = [_row("A098", "20260101", +5.0, "PR"),
            _row("A098", "20260101", +6.0, "UR"),
            _row("A098", "20260113", -5.0, "PR")]
    flips, transitions, seqs = sign_reversals(rows)
    assert seqs["A098"] == "+-"
    assert transitions == 1 and flips == 1


def test_a_perfectly_alternating_track_reverses_at_every_transition():
    from troposphere import sign_reversals
    refs = ("20260101", "20260113", "20260125", "20260206", "20260218")
    rows = [_row("A098", r, (+5.0 if i % 2 == 0 else -5.0))
            for i, r in enumerate(refs)]
    flips, transitions, _ = sign_reversals(rows)
    assert (flips, transitions) == (4, 4)


def test_random_signs_land_near_half_the_transitions():
    """
    The number the claim has to beat. Six reversals in fifteen pairs - the
    figure this project published as evidence of alternation - is fourteen
    transitions against seven expected, i.e. BELOW chance.
    """
    from troposphere import sign_reversals
    rng = np.random.default_rng(0)
    refs = [f"2026{m:02d}01" for m in range(1, 13)]
    ratios = []
    for _ in range(200):
        rows = [_row("A098", r, float(rng.choice([-1.0, 1.0]))) for r in refs]
        f, t, _ = sign_reversals(rows)
        ratios.append(f / t)
    assert 0.45 < float(np.mean(ratios)) < 0.55


def test_an_undefined_correlation_is_not_reported_as_significant():
    """
    p_value fell back to 0.0 when r was NaN, so an undefined fit came out as
    maximally significant and would have been starred *** in the table.
    """
    from troposphere import fit_elevation_trend

    # No relief at all: the fit is refused before a correlation is attempted.
    flat = fit_elevation_trend(np.full(200, 3000.0), np.arange(200, dtype=float))
    assert not flat["usable"]
    assert not np.isfinite(flat["p_value"])

    # Real relief, but a perfectly constant displacement field. The fit runs and
    # is marked usable, and corrcoef is 0/0 - so this is the path that actually
    # reaches the significance branch, and where p fell back to 0.0.
    elev = np.linspace(3000.0, 7000.0, 400)
    const = fit_elevation_trend(elev, np.full(400, 12.0))
    assert const["usable"], "fixture must reach the significance branch"
    assert not np.isfinite(const["r"])
    assert not np.isfinite(const["p_value"]), (
        f"an undefined correlation was scored p={const['p_value']!r}; "
        "0.0 would star it *** in the table")
