"""
Look side is a property of the mission, and it decides the whole answer.

Why this exists
===============
`los_unit` used to default to left-looking and every caller took the default,
so Sentinel-1 - which looks RIGHT - was modelled as a left-looking sensor for
the entire multi-geometry analysis. Nothing crashed and no number looked odd,
because flipping the side reverses only the two horizontal components: an AOI
median is dominated by the vertical term and stays perfectly plausible while
every east-west inference comes out backwards.

The module's own docstring already carried the check that catches it - a
descending Sentinel-1 pass at 28 N must put the satellite EAST of the target,
because a descending right-looking pass images west - and the code had been
failing that check for every Sentinel-1 track. There were no tests here at all,
which is why the mutation suite could never have found it either.

At the 26 Aug 2026 failure point the error reverses the verdict on three of the
five tracks, turning "one usable look direction, and no amount of processing
recovers the other one" into three usable tracks with a genuine
ascending/descending pair. These tests pin that down.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from geometry_merge import (MIN_SLOPE_DEG, TRACKS, Track, downslope_unit,
                            heading_deg, los_unit, sensitivities)

# The reported failure point of the 26 Aug 2026 Langtang collapse, and the
# terrain the sensitivity table is quoted at.
FAIL_LAT, FAIL_LON = 28.28771, 85.52809
FAIL_SLOPE, FAIL_ASPECT = 27.4, 273.0


def track(name: str) -> Track:
    return next(t for t in TRACKS if t.name == name)


def los_for(t: Track, lat: float = FAIL_LAT) -> np.ndarray:
    return los_unit(heading_deg(t.inclination_deg, lat, t.ascending),
                    t.incidence_deg, t.left_looking)


# ---------------------------------------------------------------------------
# The look side itself
# ---------------------------------------------------------------------------
def test_look_side_cannot_be_omitted():
    """
    No default. A look side that can be omitted is a look side that gets
    omitted - that is exactly how every Sentinel-1 track ended up wrong.
    """
    with pytest.raises(TypeError):
        los_unit(350.5, 37.0)          # type: ignore[call-arg]


def test_every_track_declares_the_side_its_mission_actually_uses():
    """
    NISAR looks left (a mission choice, for full Antarctic coverage).
    Sentinel-1 looks right. If a track is added without checking this, the
    sensitivity table silently changes sign on some slopes.
    """
    assert TRACKS, "no tracks configured"
    for t in TRACKS:
        if t.mission == "NISAR":
            assert t.left_looking is True, f"{t.name}: NISAR looks LEFT"
        elif t.mission == "Sentinel-1":
            assert t.left_looking is False, f"{t.name}: Sentinel-1 looks RIGHT"
        else:
            pytest.fail(f"{t.name}: unknown mission {t.mission!r}, look side unverified")


def test_descending_sentinel1_puts_the_satellite_east_of_the_target():
    """
    The check the module docstring states, and the one the code used to fail.

    A descending right-looking pass images WEST, so the satellite sits east of
    what it is looking at and the target-to-satellite vector has E > 0. Under
    the old left-looking default this was negative for every Sentinel-1 track.
    """
    for name in ("S1 DESC 19", "S1 DESC 121"):
        t = track(name)
        assert not t.ascending
        e, _n, u = los_for(t)
        assert e > 0, f"{name}: descending S1 must look west, so E > 0 (got {e:+.3f})"
        assert u > 0, "the satellite is always above the target"


def test_ascending_sentinel1_puts_the_satellite_west_of_the_target():
    """The other half of the same statement: ascending right-looking images east."""
    t = track("S1 ASC 85")
    e, _n, _u = los_for(t)
    assert e < 0, f"ascending S1 must look east, so E < 0 (got {e:+.3f})"


def test_nisar_ascending_matches_the_look_vector_in_the_product():
    """
    Pinned against losUnitVectorX/Y read from a real GUNW at 28.275 N:

        product   E +0.6161  N +0.1531  U +0.7727

    which is the left-looking form. The right-looking form gives E -0.6261,
    N -0.1053 - same vertical, both horizontals reversed, which is why the
    error survived every sanity check that looked at an AOI median.

    Evaluated at the incidence the product itself reports (39.4 deg, from
    U = cos(incidence) = 0.7727), not the 37.0 deg nominal mid-swath value in
    TRACKS. Comparing against a different incidence would test the swath
    position rather than the look side.
    """
    t = track("NISAR ASC 98")
    h = heading_deg(t.inclination_deg, 28.275, t.ascending)
    e, n, u = los_unit(h, 39.4, t.left_looking)

    # Sign pattern first: this is what the look side actually decides.
    assert (e > 0, n > 0, u > 0) == (True, True, True)
    assert e == pytest.approx(0.6161, abs=0.03)
    assert n == pytest.approx(0.1531, abs=0.06)
    assert u == pytest.approx(0.7727, abs=0.01)

    # And the right-looking form is the one that does NOT match the product.
    e_r, n_r, _ = los_unit(h, 39.4, left_looking=False)
    assert e_r < 0 and n_r < 0


def test_flipping_the_side_reverses_only_the_horizontal_components():
    """
    The mechanism behind the silence. If this ever stops holding, the argument
    for why the bug was invisible stops holding too.
    """
    left = los_unit(350.5, 37.0, left_looking=True)
    right = los_unit(350.5, 37.0, left_looking=False)
    assert left[0] == pytest.approx(-right[0])
    assert left[1] == pytest.approx(-right[1])
    assert left[2] == pytest.approx(right[2])
    assert np.linalg.norm(left) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# What it does to the answer
# ---------------------------------------------------------------------------
def test_three_tracks_can_see_the_failure_point_not_one():
    """
    The corrected sensitivity table at 28.28771 N 85.52809 E, on the 27.4 deg
    west-facing slope the README quotes:

        S1 ASC 85       +0.188   blind      (was -0.904, "usable")
        NISAR ASC 98    -0.889   usable
        S1 DESC 19      -0.913   usable     (was +0.198, "blind")
        S1 DESC 121     -0.913   usable     (was +0.198, "blind")
        NISAR DESC 48   +0.163   blind

    Three usable tracks, and one of them descending - so the claim that this
    aspect leaves a single look direction is false, and an ascending/descending
    decomposition is available here.
    """
    rows = {r["track"]: r for r in
            sensitivities(FAIL_LAT, FAIL_LON, FAIL_SLOPE, FAIL_ASPECT, 0.3)}

    expected = {"S1 ASC 85": +0.188, "NISAR ASC 98": -0.889,
                "S1 DESC 19": -0.913, "S1 DESC 121": -0.913,
                "NISAR DESC 48": +0.163}
    for name, want in expected.items():
        got = rows[name]["sensitivity"]
        assert got == pytest.approx(want, abs=0.01), f"{name}: {got:+.3f} != {want:+.3f}"

    usable = [n for n, r in rows.items() if r["usable"]]
    assert len(usable) == 3
    assert "NISAR ASC 98" in usable
    assert any(not rows[n]["ascending"] for n in usable), (
        "a descending track must be usable here - without one there is no "
        "east/vertical decomposition and the multi-geometry argument collapses")


def test_a_blind_track_amplifies_noise_and_is_rejected():
    """Sensitivity near zero is not a small measurement, it is no measurement."""
    rows = {r["track"]: r for r in
            sensitivities(FAIL_LAT, FAIL_LON, FAIL_SLOPE, FAIL_ASPECT, 0.3)}
    blind = rows["NISAR DESC 48"]
    assert not blind["usable"]
    assert blind["amplification"] > 5
    good = rows["NISAR ASC 98"]
    assert good["usable"] and good["amplification"] < 1.2


def test_a_slope_across_the_look_direction_is_invisible_however_fast_it_moves():
    """
    The cancellation trap, stated as a property rather than an anecdote: a
    slope can be moving and still project nothing into range.
    """
    t = track("NISAR ASC 98")
    l_hat = los_for(t)
    worst = min(range(0, 360),
                key=lambda a: abs(float(np.dot(downslope_unit(35.0, a), l_hat))))
    assert abs(float(np.dot(downslope_unit(35.0, worst), l_hat))) < 0.05


# ---------------------------------------------------------------------------
# Terrain conventions the sensitivity rests on
# ---------------------------------------------------------------------------
def test_downslope_points_down_and_along_the_aspect():
    assert downslope_unit(0.0, 90.0) == pytest.approx([1, 0, 0], abs=1e-9)   # flat, east
    assert downslope_unit(90.0, 0.0) == pytest.approx([0, 0, -1], abs=1e-9)  # straight down
    west = downslope_unit(27.4, 270.0)
    assert west[0] < 0 and west[2] < 0
    assert np.linalg.norm(west) == pytest.approx(1.0)


def test_headings_are_near_polar_and_mirrored_about_north():
    """
    A retrograde near-polar orbit does not give headings 180 deg apart. The
    ascending and descending passes are mirror images about the N-S axis:
    desc = 180 - asc, so asc + desc = 180 (mod 360). At 28 N that is 350.5 and
    189.5, which is 19 deg away from antiparallel - and assuming antiparallel
    is its own way of getting the east-west geometry wrong.
    """
    asc = heading_deg(98.4, FAIL_LAT, True)
    desc = heading_deg(98.4, FAIL_LAT, False)
    assert 345 < asc < 355
    assert 185 < desc < 195
    assert (asc + desc) % 360 == pytest.approx(180.0, abs=0.5)


def test_gentle_ground_is_below_the_slope_floor():
    """
    The candidate at 28.27484 N sits on 5 deg of slope, where the aspect is DEM
    noise. The threshold that refuses a verdict there is a published limit, so
    it is pinned rather than left to drift.
    """
    assert MIN_SLOPE_DEG == 10.0
    assert 5.0 < MIN_SLOPE_DEG


def test_map_offers_the_analysis_aoi():
    """
    `--map` used to accept only langtang and lhende, so the "what area does the
    null actually cover" statistic was never computed over the source zone -
    the polygon the null is about.
    """
    import geometry_merge
    src = (geometry_merge.__file__ or "").replace(".pyc", ".py")
    text = open(src).read()
    block = text.split('mode.add_argument("--map"')[1].split(")")[0]
    assert '"source"' in block


# ---------------------------------------------------------------------------
# The aspect at the failure point is the largest open uncertainty, so it has
# to be testable rather than merely declared.
# ---------------------------------------------------------------------------
def test_a_north_facing_scar_blinds_both_nisar_tracks():
    """
    Every published account puts the 26 Aug 2026 scar on the NORTH face of
    Langtang Lirung; SRTM at the assumed failure pixel reads west-facing at
    every stencil from 60 m to 300 m. Sensitivity is a dot product with the
    downslope vector, so that disagreement decides which tracks are usable.

    On a due-north face both NISAR geometries fall below the 0.3 threshold and
    the mission has no usable look direction at the scar - only Sentinel-1
    does. The bound on downslope motion weakens by about 3x. This test holds
    the consequence so it cannot quietly stop being true.
    """
    from geometry_merge import sensitivities
    rows = sensitivities(28.28771, 85.52809, 27.4, 0.0, 0.3)
    by = {r["track"]: r for r in rows}
    nisar = [v for k, v in by.items() if "NISAR" in k]
    assert nisar and all(abs(r["sensitivity"]) < 0.3 for r in nisar)
    s1 = [v for k, v in by.items() if k.startswith("S1")]
    assert s1 and all(abs(r["sensitivity"]) > 0.3 for r in s1)


def test_the_west_facing_pixel_keeps_nisar_ascending_usable():
    """The two hypotheses must give different answers, or the flag is pointless."""
    from geometry_merge import sensitivities
    rows = sensitivities(28.28771, 85.52809, 27.4, 273.0, 0.3)
    asc = [r for r in rows if "NISAR ASC" in r["track"]][0]
    assert abs(asc["sensitivity"]) > 0.8
