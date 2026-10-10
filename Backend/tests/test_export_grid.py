"""
Exports must land on one grid, or they cannot be differenced.

Why this exists
===============
Each export used to be clipped to the bounding box of its own VALID pixels,
which is a different rectangle in every product - 179x220 for one GUNW pair
over this AOI and 181x142 for the next. Nothing failed. The files opened, the
georeferencing was correct, every pixel held the right number. They simply could
not be subtracted from each other, so coherence-change mapping - the standard
way to map a landslide after the fact, and the method this project's own method
notes recommend for exactly that - was unreachable from the pipeline. Every
comparison had to crop to a common extent by hand.

The grid is now defined by the AOI and anchored to absolute multiples of the
pixel size, so any two products at the same posting produce the same raster
shape on the same lattice. These tests hold that: two products covering
different parts of the AOI must come out identical in shape and origin, a value
at a given map coordinate must land in the same cell in both, and a product that
does NOT share the lattice must be refused rather than quietly resampled.
"""

from __future__ import annotations

import numpy as np

from gunw_reader import aoi_grid, place_on_grid

RES = 80.0
# A square AOI in projected metres. epsg=None keeps the ring as-is, so the test
# needs no pyproj and no network.
RING = [(1000.0, 2000.0), (1400.0, 2000.0), (1400.0, 2400.0), (1000.0, 2400.0)]


def _centres(x_start, y_start, nx, ny):
    """Pixel centres on the lattice, north-up."""
    xs = x_start + RES * np.arange(nx)
    ys = y_start - RES * np.arange(ny)
    return xs, ys


def test_two_products_covering_different_ground_get_the_same_grid():
    a = aoi_grid(*_centres(1000.0, 2440.0, 3, 3), RING, None)
    b = aoi_grid(*_centres(1160.0, 2280.0, 4, 4), RING, None)
    assert a is not None and b is not None
    assert (a["height"], a["width"]) == (b["height"], b["width"])
    assert a["transform"] == b["transform"]


def test_the_same_map_coordinate_lands_in_the_same_cell():
    xs_a, ys_a = _centres(1000.0, 2440.0, 3, 3)
    xs_b, ys_b = _centres(1160.0, 2280.0, 4, 4)
    ga = aoi_grid(xs_a, ys_a, RING, None)
    gb = aoi_grid(xs_b, ys_b, RING, None)

    # Put a marker at the one map coordinate both products contain.
    A = np.full((len(ys_a), len(xs_a)), np.nan)
    B = np.full((len(ys_b), len(xs_b)), np.nan)
    A[int(np.argmin(abs(ys_a - 2280.0))), int(np.argmin(abs(xs_a - 1160.0)))] = 42.0
    B[int(np.argmin(abs(ys_b - 2280.0))), int(np.argmin(abs(xs_b - 1160.0)))] = 42.0

    oa, ob = place_on_grid(A, ga), place_on_grid(B, gb)
    assert oa.shape == ob.shape
    assert np.array_equal(np.argwhere(oa == 42.0), np.argwhere(ob == 42.0))


def test_the_two_rasters_can_simply_be_subtracted():
    """The whole point: no cropping, no alignment step, no half-pixel guess."""
    ga = aoi_grid(*_centres(1000.0, 2440.0, 3, 3), RING, None)
    gb = aoi_grid(*_centres(1160.0, 2280.0, 4, 4), RING, None)
    oa = place_on_grid(np.ones((3, 3)) * 5.0, ga)
    ob = place_on_grid(np.ones((4, 4)) * 3.0, gb)
    d = ob - oa                       # would have raised before this fix
    assert d.shape == oa.shape
    assert np.nanmin(d) == -2.0


def test_a_product_off_the_lattice_is_refused_not_resampled():
    """
    Half a pixel of offset. Resampling would invent values and hide the problem;
    returning None sends the caller down the per-product path with a warning.
    """
    xs, ys = _centres(1000.0 + RES / 2, 2440.0, 3, 3)
    assert aoi_grid(xs, ys, RING, None) is None


def test_a_degenerate_product_grid_is_refused():
    assert aoi_grid(None, None, RING, None) is None
    assert aoi_grid(np.array([1000.0]), np.array([2440.0]), RING, None) is None
