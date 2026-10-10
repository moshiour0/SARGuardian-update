"""
Mapping the scar, and the arithmetic that places it.

Why this exists
===============
The aspect of the failing surface decides every sensitivity number in this
project, and it came from an SRTM pixel at a failure point read off a report.
Sentinel-2 says that point is not on the scar: it is observed in both epochs,
it was never glaciated, and its NDSI went UP. The real detachment is 1.09 km
away on a NNW face, and moving there changes the downslope bound from 45 to
68 mm/day.

These tests hold the parts of that chain which can fail silently: the grid
georeferencing, which if taken from the wrong band puts the scar 600 m out;
the direction of the snow index, which is the whole discriminant; and the
region grow, which must not leak across unrelated ground.
"""

from __future__ import annotations

import numpy as np
import pytest

from scar_map import AOI, GRID, UNUSABLE, grid_transform, grow, measure, probe


def test_the_grid_transform_comes_from_the_aoi_not_a_band():
    """
    Bands have different native postings - green 10 m, swir16 20 m - and are
    resampled onto one grid. A transform lifted from the 10 m band would carry
    10 m pixels for a 20 m array and place everything at half scale, which put
    the scar about 600 m from where it is.
    """
    tf = grid_transform()
    assert tf.a == pytest.approx(20.0, abs=0.5), "pixel width is not 20 m"
    assert tf.e == pytest.approx(-20.0, abs=0.5), "pixel height is not 20 m"
    # and the grid must span the AOI, not a fraction of it
    width_m = tf.a * GRID[1]
    assert width_m == pytest.approx(9140, rel=0.02)


def test_snow_is_not_treated_as_unusable():
    """
    Snow is the thing that disappears. Masking it out with the clouds would
    remove the entire signal and leave the scar invisible by construction.
    """
    assert 11 not in UNUSABLE
    for cloud_class in (8, 9, 10, 3):
        assert cloud_class in UNUSABLE


def test_region_grow_does_not_leak_across_a_gap():
    """
    Two separate patches must stay separate. A grow that leaks would merge the
    detachment with unrelated seasonal snow loss and inflate the extent, which
    is one of the four checks the identification rests on.
    """
    m = np.zeros((20, 20), bool)
    m[2:6, 2:6] = True          # patch A, seeded
    m[14:18, 14:18] = True      # patch B, disconnected
    cells = grow(m, (3, 3))
    assert len(cells) == 16
    assert all(r < 10 and c < 10 for r, c in cells)


def test_region_grow_refuses_a_seed_outside_the_mask():
    m = np.zeros((10, 10), bool)
    m[5, 5] = True
    assert grow(m, (0, 0)) == []


def test_a_brightening_surface_is_not_reported_as_a_scar():
    """
    The assumed failure point got BRIGHTER: NDSI +0.379, and not one pixel
    fell. Fresh snow moves the index the opposite way from a scar, which is
    what makes the discriminant robust - so a positive change must never be
    counted as scar-like.
    """
    # Full-size grid: probe() locates the point with the grid transform, so a
    # toy array would index outside it and look like missing data.
    d = np.full(GRID, +0.38, np.float32)
    both = np.ones(GRID, bool)
    pre = np.zeros(GRID, np.float32)          # was not snow
    r = probe(28.28771, 85.52809, d, both, pre, grid_transform(), radius=5)
    assert r["observed"] is True
    assert r["n_usable"] > 0, "must report that it was looked at, not skipped"
    assert r["scar_like_pct"] == 0.0
    assert r["was_snow_pct"] == 0.0


def test_an_unobserved_point_is_reported_as_untested_not_as_clean():
    """
    "No scar here" and "no data here" are different claims. Conflating them
    would let cloud masquerade as evidence of absence - the same error as
    reporting a non-detection without its floor.
    """
    d = np.full(GRID, np.nan, np.float32)
    both = np.zeros(GRID, bool)
    pre = np.zeros(GRID, np.float32)
    r = probe(28.28771, 85.52809, d, both, pre, grid_transform(), radius=5)
    assert r["observed"] is False
    assert r["n_usable"] == 0


def test_measure_reports_extent_and_a_change_weighted_centroid():
    """
    The centroid must follow the deepest part of the change, not the outline
    of the mask, or a ragged edge drags the reported location off the scar.
    """
    cells = [(r, c) for r in range(100, 110) for c in range(200, 210)]
    d = np.zeros((GRID[0], GRID[1]), np.float32)
    for r, c in cells:
        d[r, c] = -0.30
    for r in range(100, 103):                 # a strong core in the north
        for c in range(200, 203):
            d[r, c] = -0.90
    pre = np.full(GRID, 0.9, np.float32)
    s = measure(cells, d, pre, grid_transform())
    assert s["n_px"] == 100
    assert s["area_km2"] == pytest.approx(0.04, abs=1e-6)
    assert s["was_snow_pct"] == 100.0
    # weighted centroid pulled north of the plain one (north = larger latitude)
    assert s["weighted_lat"] > s["centroid_lat"]
