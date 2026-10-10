"""
The headline has to compare like with like, and it has to be computed.

Why this exists
===============
The published gap - "180x to 360x too insensitive" - divided a NISAR floor
expressed as DOWNSLOPE motion by a Sentinel-1 rate the reports never call
downslope, and used a floor for ONE PIXEL against a rate averaged over a
slope. It also carried an upper end from a candidate with no valid offsets at
all, so that floor had never been measured there.

These tests hold the corrected construction: line of sight against line of
sight, both ends of the estimator bracket, the pre-event window closed at the
collapse, the product maturity said out loud, and an unobserved candidate
reported as unobserved rather than given a number.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

import bound
from common_ref import buffer_mask, common_mask, rereference
from gunw_reader import crid_of, product_maturity
from local_floor import block_median_floor, compare, detection_floor

BETA = ("NISAR_L2_PR_GOFF_006_098_A_016_007_4000_SH_20251128T233919_20251128T233954_"
        "20251210T233920_20251210T233955_X05010_N_F_J_001.h5")
PROV = ("NISAR_L2_PR_GOFF_024_098_A_016_025_4000_SH_20260702T233920_20260702T233955_"
        "20260714T233920_20260714T233954_P05023_F_F_J_001.h5")


# ---------------------------------------------------------------------------
# Product maturity
# ---------------------------------------------------------------------------
def test_winter_pairs_are_beta_and_summer_pairs_provisional():
    assert product_maturity(BETA) == "BETA"
    assert product_maturity(PROV) == "PROVISIONAL"
    assert crid_of(BETA) == "X05010" and crid_of(PROV) == "P05023"


def test_a_pair_straddling_the_release_gap_is_not_rounded_to_either():
    """Jan 2026 to Jul 2026 is neither window. Calling it BETA would hide that."""
    gap = BETA.replace("20251210T233920_20251210T233955", "20260714T233920_20260714T233954")
    assert product_maturity(gap) == "UNKNOWN"


# ---------------------------------------------------------------------------
# The window-median floor
# ---------------------------------------------------------------------------
def test_block_floor_sees_error_the_window_shares():
    """
    A field made of 13x13 blocks, each offset by its own constant, with almost
    no scatter inside a block. One window looks quiet; the block medians do
    not. A floor from inside the window alone would call it quiet.
    """
    rng = np.random.default_rng(1)
    field = np.kron(rng.normal(0, 100, (10, 10)), np.ones((13, 13)))
    field += rng.normal(0, 1, field.shape)
    win = field[:13, :13]
    inside = detection_floor(win.ravel(), 12.0)
    blk, n = block_median_floor(field, 13, 12.0)
    assert n == 100
    assert blk > 20 * inside


def test_block_floor_is_below_the_pixel_floor_for_independent_noise():
    """With nothing shared, averaging 169 pixels must help, by about sqrt(n)."""
    rng = np.random.default_rng(2)
    field = rng.normal(0, 100, (130, 130))
    blk, _ = block_median_floor(field, 13, 12.0)
    pix = detection_floor(field.ravel(), 12.0)
    assert blk < pix / 5


def test_compare_reports_the_block_floor_and_refuses_it_on_a_1d_field():
    rng = np.random.default_rng(3)
    aoi = rng.normal(0, 50, (91, 91))
    c = compare(aoi, aoi[:13, :13], 12.0)
    assert math.isfinite(c["block_median_floor_mm_day"]) and c["n_blocks"] == 49
    c1 = compare(aoi.ravel(), aoi[:13, :13].ravel(), 12.0)
    assert math.isnan(c1["block_median_floor_mm_day"])


# ---------------------------------------------------------------------------
# The common datum
# ---------------------------------------------------------------------------
def test_rereference_recovers_each_pairs_datum_error():
    """Known constants added per pair come back out, and the target is untouched."""
    rng = np.random.default_rng(4)
    base = [rng.normal(0, 5, (60, 60)) for _ in range(3)]
    for b in base:
        b[25:35, 25:35] += 40.0                     # the target moves 40 mm
    errs = [-46.0, 21.0, -12.0]
    stack = [b + e for b, e in zip(base, errs)]
    stable = common_mask(stack) & ~buffer_mask((60, 60), [(30, 30)], 12)
    fixed, consts = rereference(stack, stable)
    for k, e in zip(consts, errs):
        assert k == pytest.approx(e, abs=1.0)
    for f in fixed:
        assert np.median(f[25:35, 25:35]) == pytest.approx(40.0, abs=2.0)


def test_the_datum_is_never_set_on_the_target():
    """Without the buffer the target's own motion would drag the datum with it."""
    m = buffer_mask((50, 50), [(25, 25)], 10)
    assert m[25, 25] and m[25, 34] and not m[25, 36]


def test_too_few_stable_pixels_is_refused():
    stack = [np.full((10, 10), 1.0), np.full((10, 10), 2.0)]
    with pytest.raises(ValueError):
        rereference(stack, np.ones((10, 10), bool))


# ---------------------------------------------------------------------------
# The headline construction
# ---------------------------------------------------------------------------
def test_the_pre_event_window_closes_at_the_collapse():
    """An interval ending after 26 Aug carries the event itself."""
    assert bound.covers_pre_event("GOFF_20260726_20260819_PR_HH-layer2.tif")
    assert not bound.covers_pre_event("GOFF_20260819_20260831_PR_HH-layer2.tif")
    assert not bound.covers_pre_event("GOFF_20251128_20251210_PR_HH-layer2.tif")


def _cand(cid="4", elev="5370"):
    return {"id": cid, "lat": "28.27799", "lon": "85.52983", "elev_m": elev,
            "slope_deg": "36.7", "aspect_deg": "350.7", "reading": "DETACHMENT-like"}


def _goff(cid, pair, px, blk):
    return {"target_id": cid, "usable": "True", "file": f"GOFF_{pair}_PR_HH-layer2.tif",
            "local_floor_mm_day": str(px), "block_median_floor_mm_day": str(blk)}


def test_the_ratio_is_line_of_sight_against_line_of_sight():
    """
    The precursor is compared with the LOS floor, never with the downslope
    bound. Dividing the downslope bound by it doubled the published gap.
    """
    goff = [_goff("4", "20260702_20260714", 32.0, 9.2),
            _goff("4", "20260714_20260726", 26.0, 6.2),
            _goff("4", "20260819_20260831", 999.0, 999.0)]   # post-event, ignored
    r = bound.assess(_cand(), goff, [], [])
    assert r["los_floor_pixel_mm_day"] == 32.0
    assert r["ratio_los_pixel"] == pytest.approx(32.0 / bound.PRECURSOR_MM_DAY, abs=0.1)
    assert r["ratio_los_block"] == pytest.approx(9.2 / bound.PRECURSOR_MM_DAY, abs=0.1)
    assert r["downslope_bound_mm_day"] > r["los_floor_pixel_mm_day"]
    assert r["matches_published_elevation"] is True


def test_an_unobserved_candidate_gets_no_bound():
    """Candidate 9 has no valid offsets. Its 'bound' was never measured."""
    r = bound.assess(_cand("9", "6099"), [], [], [])
    assert r["goff_intervals"] == 0
    assert math.isnan(r["ratio_los_pixel"]) and math.isnan(r["downslope_bound_mm_day"])
    assert r["matches_published_elevation"] is False


def test_the_committed_bound_table_matches_what_bound_computes():
    """The table the demo and the site read must be the one this code writes."""
    import csv
    rows = {r["id"]: r for r in csv.DictReader(open(bound.OUT / "bound_source.csv"))}
    cands = [c for c in bound.load("scar_candidates_source.csv")
             if c["reading"] == "DETACHMENT-like"]
    goff = bound.load("local_floor_candidates.csv")
    phase = bound.load("local_floor_phase_candidates.csv")
    cref = bound.load("common_ref_summer.csv")
    for c in cands:
        fresh = bound.assess(c, goff, phase, cref)
        assert str(fresh["ratio_los_pixel"]) == rows[c["id"]]["ratio_los_pixel"]
        assert str(fresh["los_floor_block_mm_day"]) == rows[c["id"]]["los_floor_block_mm_day"]
