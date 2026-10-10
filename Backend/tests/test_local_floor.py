"""
A floor measured over the AOI is not the floor at the point.

Why this exists
===============
Every detection floor this project quoted was the MAD scatter of one product
over the whole 82 km2 source polygon. The failure was one hillside inside it.
Nobody had checked whether those two numbers agree, and they do not: at the
failure point the ascending floor is 33.4 mm/day against 19.8 over the AOI.

The pair that exposes it is the last ascending interval before the collapse.
Over the AOI it has the LOWEST floor in the archive - 8.9 mm/day, the number
that made the published bound look strongest. In a 1 km window on the failure
point that same pair has a floor of 34.3 mm/day and 26 of 169 valid pixels.

These tests hold the properties that finding depends on: that both sides use
the same formula so the comparison means something, that a quiet window inside
a noisy scene is reported as quiet rather than inheriting the scene, that the
window is clipped at the raster edge instead of padded, and that a window with
too few valid pixels is refused rather than given a MAD of two numbers.
"""

from __future__ import annotations

import numpy as np

from local_floor import (MIN_PX, compare, detection_floor, robust_sigma,
                         span_from_name, window)


def test_floor_is_three_sigma_over_the_span():
    """
    Same definition as goff_reader's AOI-wide detect_floor_mm_day. If these
    two ever diverge the comparison this module makes is meaningless, so the
    formula is pinned here.
    """
    rng = np.random.default_rng(0)
    v = rng.normal(0.0, 100.0, 20000)
    assert abs(robust_sigma(v) - 100.0) < 3.0
    assert abs(detection_floor(v, 12.0) - 3 * robust_sigma(v) / 12.0) < 1e-9
    # A longer span sees a slower velocity: same scatter, half the floor.
    assert abs(detection_floor(v, 24.0) - detection_floor(v, 12.0) / 2) < 1e-9


def test_a_noisy_point_inside_a_quiet_scene_is_reported_as_noisy():
    """The finding itself, in miniature."""
    rng = np.random.default_rng(1)
    aoi = rng.normal(0.0, 20.0, (200, 200))
    win = rng.normal(0.0, 80.0, (13, 13))
    c = compare(aoi, win, 12.0)
    assert c["usable"]
    assert c["ratio"] > 3.0
    assert c["local_floor_mm_day"] > c["aoi_floor_mm_day"]


def test_a_quiet_point_inside_a_noisy_scene_is_reported_as_quiet():
    """
    The converse must also work, or the tool would only ever confirm what it
    was built to find. One real pair does behave this way - 20260723_20260816
    is quieter at the point than over the AOI - so this is not hypothetical.
    """
    rng = np.random.default_rng(2)
    aoi = rng.normal(0.0, 200.0, (200, 200))
    win = rng.normal(0.0, 20.0, (13, 13))
    c = compare(aoi, win, 12.0)
    assert c["usable"]
    assert c["ratio"] < 0.5


def test_too_few_valid_pixels_is_refused_not_averaged():
    """
    The last pre-event ascending pair has 2 valid pixels in a 500 m window. A
    MAD of two numbers is not a noise floor, and reporting one would have hidden
    exactly the gap this module exists to show.
    """
    rng = np.random.default_rng(3)
    aoi = rng.normal(0.0, 50.0, (100, 100))
    win = np.full((7, 7), np.nan)
    win[0, 0], win[1, 1] = 10.0, -10.0
    c = compare(aoi, win, 12.0)
    assert not c["usable"]
    assert np.isnan(c["local_floor_mm_day"])
    assert c["window_px"] == 2


def test_valid_pixel_count_is_reported_alongside_the_floor():
    """
    A floor without its sample count is not interpretable - the whole finding
    turns on 26 of 169, not on the 34.3 mm/day by itself.
    """
    rng = np.random.default_rng(4)
    aoi = rng.normal(0.0, 50.0, (100, 100))
    win = rng.normal(0.0, 50.0, (13, 13))
    win[np.triu_indices(13)] = np.nan
    c = compare(aoi, win, 12.0)
    assert c["window_total_px"] == 169
    assert c["window_px"] == int(np.isfinite(win).sum())
    assert c["window_px"] < c["window_total_px"]


def test_window_is_clipped_at_the_edge_not_padded():
    """
    A target near the raster edge must yield fewer pixels, never a window
    filled with invented nodata that would depress the scatter.
    """
    a = np.arange(100.0).reshape(10, 10)
    w = window(a, 0, 0, 3)
    assert w.shape == (4, 4)
    assert np.isfinite(w).all()
    assert window(a, 5, 5, 2).shape == (5, 5)
    assert window(a, 9, 9, 3).shape == (4, 4)


def test_window_centres_on_the_requested_pixel():
    a = np.zeros((21, 21))
    a[10, 10] = 1.0
    assert window(a, 10, 10, 2).sum() == 1.0
    assert window(a, 4, 4, 2).sum() == 0.0


def test_span_is_read_from_the_filename():
    assert span_from_name("GOFF_20260726_20260819_PR_HH-layer2.tif") == 24.0
    assert span_from_name("GOFF_20251128_20251210_PR_HH-layer2.tif") == 12.0
    assert np.isnan(span_from_name("no_dates_here.tif"))


def test_a_zero_span_yields_no_floor():
    """Dividing by zero days would report an infinite sensitivity."""
    assert np.isnan(detection_floor(np.arange(100.0), 0.0))


def test_min_px_guard_applies_to_both_sides():
    """An AOI with too few valid pixels is as unusable as a window with too few."""
    tiny = np.array([1.0, 2.0, 3.0])
    big = np.random.default_rng(5).normal(0.0, 10.0, 500)
    assert tiny.size < MIN_PX
    assert not compare(tiny, big, 12.0)["usable"]
    assert not compare(big, tiny, 12.0)["usable"]


# ---------------------------------------------------------------------------
# A floor without an interval asserts a precision the pixels do not carry.
# ---------------------------------------------------------------------------
def test_the_bootstrap_interval_brackets_the_point_estimate():
    """
    The published bound is 40.4 mm/day from 49 valid pixels. A MAD from that
    many samples carries roughly 15% relative error, so the point estimate
    needs an interval around it or the third significant figure is invented.
    """
    from local_floor import floor_ci, detection_floor
    rng = np.random.default_rng(7)
    v = rng.normal(0.0, 100.0, size=49)
    point = detection_floor(v, 12.0)
    lo, hi = floor_ci(v, 12.0)
    assert lo < point < hi
    assert hi > lo


def test_fewer_pixels_widen_the_interval():
    """
    The pairs that matter most here are the ones with the fewest valid pixels
    in the window. The interval must say so rather than staying flat.
    """
    from local_floor import floor_ci
    rng = np.random.default_rng(11)
    big = rng.normal(0.0, 100.0, size=400)
    small = rng.normal(0.0, 100.0, size=20)
    wb = float(np.subtract(*reversed(floor_ci(big, 12.0))))
    ws = float(np.subtract(*reversed(floor_ci(small, 12.0))))
    assert ws > wb


def test_a_window_below_the_minimum_gets_no_interval():
    """MIN_PX gates the interval exactly as it gates the floor."""
    from local_floor import floor_ci
    lo, hi = floor_ci(np.array([1.0, 2.0, 3.0]), 12.0)
    assert not np.isfinite(lo) and not np.isfinite(hi)


def test_compare_reports_the_interval_alongside_the_floor():
    """A caller must not have to recompute the uncertainty separately."""
    rng = np.random.default_rng(3)
    aoi = rng.normal(0.0, 80.0, size=2000)
    win = rng.normal(0.0, 160.0, size=169)
    c = compare(aoi, win, 12.0)
    assert c["usable"]
    assert c["local_floor_ci_lo"] < c["local_floor_mm_day"] < c["local_floor_ci_hi"]


def test_local_floors_round_trip_into_the_detector(tmp_path):
    """
    A series measured at a point must be gated on the floor AT the point.
    --as-floors writes exactly what inverse_velocity.load_floors reads.
    """
    from datetime import date
    from inverse_velocity import load_floors
    from local_floor import write_as_floors
    rows = [{"file": "GOFF_20260702_20260714_PR_HH-layer2.tif", "usable": True,
             "target_id": "4", "local_floor_mm_day": 32.01},
            {"file": "GOFF_20260629_20260711_PR_HH-layer2.tif", "usable": True,
             "target_id": "4", "local_floor_mm_day": 255.3}]
    out = tmp_path / "f.csv"
    assert write_as_floors(rows, out, "layer2") == 2
    f = load_floors(out, "layer2")
    assert f[(date(2026, 7, 2), date(2026, 7, 14))] == 32.01
    assert f[(date(2026, 6, 29), date(2026, 7, 11))] == 255.3


def test_floors_from_two_targets_are_refused(tmp_path):
    """One slope must never be gated on another's noise."""
    import pytest
    from local_floor import write_as_floors
    rows = [{"file": "GOFF_20260702_20260714_x.tif", "usable": True,
             "target_id": t, "local_floor_mm_day": 1.0} for t in ("1", "4")]
    with pytest.raises(ValueError):
        write_as_floors(rows, tmp_path / "f.csv", "layer2")
