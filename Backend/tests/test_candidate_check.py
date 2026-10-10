"""
The geometry cross-check, which used to point the wrong way.

Why these tests exist
=====================
candidate_check.py once declared a signal real when both geometries agreed in
sign. That is backwards. Downslope motion has one magnitude projected onto two
different lines of sight, so where the sensitivities have opposite signs - most
of this terrain - real motion appears with OPPOSITE LOS signs. Matching signs
are the signature of a path delay, which adds the same extra path length
whichever direction the radar looks from.

The bug never crashed and never produced a wrong number. It printed a confident
sentence about a phase artefact. On the Langtang candidate (sens_asc -0.732,
sens_desc +0.472, both rates negative) it said "a geometry-specific artefact is
ruled out: this is a real phase signal". The final verdict survived only because
a later seasonality test caught what this one had waved through.

So the first test below is the historical fault, stated as a number. The rest
guard the two ways the test can be honest about not knowing.
"""

from __future__ import annotations

from candidate_check import classify_geometry


# The measured Langtang candidate: 28.27484 N, 85.47405 E.
LANGTANG = dict(sens_asc=-0.732, sens_desc=+0.472)


def test_same_signed_rates_are_a_path_delay_where_sensitivities_oppose():
    """The historical fault. Both rates negative, sensitivities opposed."""
    r = classify_geometry(v_asc=-3.49, v_desc=-1.47, **LANGTANG)
    assert r["verdict"] == "delay"
    assert r["conclusive"]
    # and it must be decided on the ratio, not on the signs alone
    assert r["ratio_motion"] < 0 < r["ratio_measured"]


def test_opposite_signed_rates_matching_the_geometry_are_motion():
    """One downslope rate seen from both sides gives ratio sens_desc/sens_asc."""
    d = 4.77                                    # mm/day downslope
    r = classify_geometry(v_asc=LANGTANG["sens_asc"] * d,
                          v_desc=LANGTANG["sens_desc"] * d, **LANGTANG)
    assert r["verdict"] == "motion"
    assert abs(r["implied_downslope_mm_day"] - d) < 1e-6


def test_a_blind_track_yields_no_verdict():
    """
    Near-zero sensitivity means dividing by nearly nothing. The ratio is then
    noise, and a verdict drawn from it is worse than no verdict.
    """
    r = classify_geometry(sens_asc=-0.05, sens_desc=+0.472,
                          v_asc=-3.49, v_desc=-1.47)
    assert r["verdict"] == "undetermined"
    assert not r["conclusive"]


def test_no_verdict_when_the_two_hypotheses_predict_the_same_ratio():
    """
    Where sens_desc/sens_asc is near +1, motion and delay make the same
    prediction and no measurement separates them. Saying so is the only honest
    answer available.
    """
    r = classify_geometry(sens_asc=-0.60, sens_desc=-0.62,
                          v_asc=-3.0, v_desc=-3.1)
    assert r["verdict"] == "undetermined"
    assert not r["conclusive"]


def test_verdict_follows_the_ratio_and_not_the_sign():
    """
    Two same-signed rates can still be motion, when the geometry says so. This
    is the case the sign-based rule got right by accident and the ratio rule
    gets right on purpose.
    """
    d = 2.0
    r = classify_geometry(sens_asc=-0.80, sens_desc=-0.30,
                          v_asc=-0.80 * d, v_desc=-0.30 * d)
    assert r["verdict"] == "motion"


def test_gentle_terrain_yields_no_verdict():
    """
    Measured at the Langtang candidate: widening the DEM stencil from 60 m to
    300 m moves the aspect by 74 degrees and takes sens_asc from +0.020 to
    -0.642, sign included. A 62-degree slope 3 km away holds its aspect to 4
    degrees over the same range. Sensitivity on gentle ground is not a small
    number, it is an undetermined one, and a ratio built on it decides nothing.
    """
    r = classify_geometry(sens_asc=-0.642, sens_desc=+0.495,
                          v_asc=-3.49, v_desc=-1.47, slope_deg=5.0)
    assert r["verdict"] == "undetermined"
    assert not r["conclusive"]
    # the same numbers on ground steep enough to trust do decide
    r2 = classify_geometry(sens_asc=-0.642, sens_desc=+0.495,
                           v_asc=-3.49, v_desc=-1.47, slope_deg=38.0)
    assert r2["verdict"] == "delay"
