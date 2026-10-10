"""
The ranking has to favour what it says it favours.

Why this exists
===============
`--rank-by efficiency` is documented as favouring "sites a small landslide
could dam", and the whole Blatten argument rests on it: the observed lake there
was about 10 m deep, so a screening tool that cannot rank small-blockage
sensitivity has not screened for the thing that happened.

It ranked on volume-per-metre alone, evaluated at the smallest height each site
happened to respond to. Those heights differ between sites and impounded volume
grows faster than linearly with depth, so dividing by a LARGER threshold can
still give the bigger ratio. In the committed source-zone run the reach 790 m
from the failure point - which holds nothing at all until a 100 m blockage -
came SECOND of twelve, above four reaches that start filling at 10 m.

That is the opposite of the stated intent, and the module had no tests, so
nothing contradicted the docstring.

The rest of these cover the hydrology the volumes rest on, on a DEM small
enough to state by hand - the module otherwise needs several thousand external
elevation queries to run at all, which is why none of it was ever exercised.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from impoundment import (d8_receivers, fill_depressions, flow_accumulation,
                         rank_sites)


def site(lat, lon, by_height, name=""):
    """A site dict shaped exactly as analyse() builds one."""
    bh = {float(h): {"volume_Mm3": v, "area_km2": v / 10, "backwater_km": 1.0,
                     "truncated": False} for h, v in by_height.items()}
    lo = min(bh)
    return {"lat": lat, "lon": lon, "name": name, "by_height": bh,
            "sill_m": 3000.0, "upstream_cells": 100, "upstream_area_km2": 1.0,
            "max_volume_Mm3": max(v["volume_Mm3"] for v in bh.values()),
            "threshold_height_m": lo,
            "efficiency_Mm3_per_m": bh[lo]["volume_Mm3"] / lo,
            "volume_at_min_height_Mm3": bh[lo]["volume_Mm3"],
            "truncated": False}


# ---------------------------------------------------------------------------
# The ranking
# ---------------------------------------------------------------------------
def test_a_site_needing_a_hundred_metres_does_not_outrank_one_that_fills_at_ten():
    """
    The exact case from the committed source-zone run, in the numbers it was
    published with: site 2 responds only at 100 m (3.23 Mm3) and site 9
    responds at 10 m (0.21 Mm3).

        volume-per-metre    3.23/100 = 0.0323   vs   0.21/10 = 0.0209

    so the old metric put the 100 m site first - while the column the README
    itself calls "the one that actually separates them" is how many reaches
    respond at 10 m.
    """
    needs_100 = site(28.2944, 85.5308, {100: 3.23, 150: 10.14}, "needs 100 m")
    fills_at_10 = site(28.3495, 85.5474, {10: 0.21, 25: 0.65, 50: 2.37,
                                          100: 7.31, 150: 14.28}, "fills at 10 m")

    assert needs_100["efficiency_Mm3_per_m"] > fills_at_10["efficiency_Mm3_per_m"], (
        "fixture must reproduce the inversion, or it is not testing anything")

    ranked = rank_sites([needs_100, fills_at_10], "efficiency",
                        top_n=12, min_separation_km=0.0)
    assert [r["name"] for r in ranked] == ["fills at 10 m", "needs 100 m"]


def test_every_small_threshold_site_outranks_every_large_threshold_site():
    """Not just the pair above - the property, across a mixed table."""
    small = [site(28.0 + i * 0.1, 85.0, {10: 0.1 + i * 0.05, 150: 20.0}, f"s{i}")
             for i in range(3)]
    large = [site(29.0 + i * 0.1, 85.0, {150: 60.0 + i}, f"L{i}") for i in range(3)]

    ranked = rank_sites(large + small, "efficiency", top_n=12, min_separation_km=0.0)
    thresholds = [r["threshold_height_m"] for r in ranked]
    assert thresholds == sorted(thresholds), (
        f"threshold heights must be non-decreasing down the table, got {thresholds}")
    assert all(r["name"].startswith("s") for r in ranked[:3])


def test_within_one_threshold_the_more_efficient_site_wins():
    """Efficiency still orders the table - it just no longer crosses thresholds."""
    a = site(28.0, 85.0, {25: 3.0}, "efficient")
    b = site(28.5, 85.0, {25: 0.5}, "less efficient")
    ranked = rank_sites([b, a], "efficiency", top_n=12, min_separation_km=0.0)
    assert [r["name"] for r in ranked] == ["efficient", "less efficient"]


def test_volume_ranking_is_unchanged_and_still_available():
    """--rank-by volume answers a different question and must keep answering it."""
    big = site(28.0, 85.0, {150: 80.0}, "big")
    small = site(28.5, 85.0, {10: 0.3, 150: 20.0}, "small but responsive")
    ranked = rank_sites([small, big], "volume", top_n=12, min_separation_km=0.0)
    assert ranked[0]["name"] == "big"


def test_near_duplicate_reaches_are_suppressed():
    """
    Consecutive channel cells describe the same pool with the dam nudged a few
    tens of metres. Without suppression one valley fills the whole table.
    """
    close = [site(28.0 + i * 0.001, 85.0, {10: 1.0 - i * 0.01}, f"c{i}")
             for i in range(5)]
    far = site(28.5, 85.0, {10: 0.5}, "far")
    ranked = rank_sites(close + [far], "efficiency", top_n=12, min_separation_km=1.5)
    assert len(ranked) == 2
    assert {r["name"] for r in ranked} == {"c0", "far"}


def test_suppression_keeps_the_better_site_of_a_cluster():
    """It must drop the neighbours of the winner, not the winner."""
    weak = site(28.000, 85.0, {25: 5.0}, "weak")
    strong = site(28.002, 85.0, {10: 1.0}, "strong")
    ranked = rank_sites([weak, strong], "efficiency", top_n=12, min_separation_km=1.5)
    assert [r["name"] for r in ranked] == ["strong"]


def test_top_n_is_respected():
    many = [site(28.0 + i * 0.1, 85.0, {10: 1.0}, f"s{i}") for i in range(20)]
    assert len(rank_sites(many, "efficiency", top_n=12, min_separation_km=0.0)) == 12


# ---------------------------------------------------------------------------
# The hydrology the volumes rest on
# ---------------------------------------------------------------------------
def _valley(ny=24, nx=24, wall=3.0, drop=2.0):
    """A V-shaped valley draining toward increasing row."""
    r = np.arange(ny)[:, None]
    c = np.arange(nx)[None, :]
    return (ny - 1 - r) * drop + np.abs(c - nx // 2) * wall


def test_filling_never_lowers_the_ground():
    z = _valley()
    z[10, 12] -= 25.0                       # a pit
    filled = fill_depressions(z)
    assert np.all(filled >= z - 1e-9)
    assert filled[10, 12] > z[10, 12], "the pit must be filled for routing"


def test_filling_leaves_terrain_without_pits_alone():
    z = _valley()
    assert np.allclose(fill_depressions(z), z, atol=1e-9)


def test_flow_accumulates_downstream_and_conserves_cells():
    z = _valley()
    filled = fill_depressions(z)
    recv = d8_receivers(filled, dx=30.0, dy=30.0)
    acc = flow_accumulation(filled, recv)

    assert acc.min() >= 1, "every cell drains at least itself"
    # Accumulation must grow toward the outlet, never shrink going downstream.
    ny, nx = z.shape
    for idx, r in enumerate(recv):
        if r >= 0:
            assert acc.ravel()[r] >= acc.ravel()[idx]
    assert acc.max() > 0.5 * z.size, "a single valley should concentrate flow"


def test_a_cell_on_the_edge_drains_off_the_grid():
    """Boundary cells have no lower neighbour inside the grid and terminate."""
    z = _valley()
    filled = fill_depressions(z)
    recv = d8_receivers(filled, dx=30.0, dy=30.0)
    ny, nx = z.shape
    outlet_row = recv.reshape(ny, nx)[-1, :]
    assert (outlet_row < 0).any(), "the downstream edge must contain a terminus"


def test_diagonal_steps_are_not_treated_as_cardinal():
    """
    A diagonal move is sqrt(2) cells long, so a diagonal drop must be steeper
    than a cardinal one to win. Getting this wrong biases every channel toward
    the diagonals.
    """
    z = np.array([[10.0, 9.4, 20.0],
                  [20.0, 20.0, 20.0],
                  [20.0, 20.0, 20.0]])
    # From (0,0): cardinal east drops 0.6 over 1 cell; there is no better move.
    recv = d8_receivers(z, dx=1.0, dy=1.0)
    assert recv[0] == 1, "steepest descent per unit LENGTH, not per step"
