"""
Coverage is a fraction of the area of interest, never of the frame.

Why this exists
===============
`report()` divided valid pixels by the size of the whole product - tens of
millions of cells, almost all of them nowhere near the AOI - and called the
result `valid_pct`. Over an 82 km2 AOI that lands between 0.01 and 0.04, and
those numbers were then read as though they were percentages of the AOI. The
published winter ascending coverage of 3.5% is really 55%: a factor of about
sixteen, in the direction that made a usable interferogram look marginal.

Two further consequences, both tested here:

  - The ascending and descending frames are different sizes (17.7 M against
    26.6 M cells), so a frame fraction cannot compare two tracks at all. The
    "3.5x ascending advantage" was 1.5x of frame-size difference riding on a
    2.7x real one.
  - The "under 10% survived gating" warning was gated on the frame fraction,
    which is under 10% for every pair ever processed over an AOI this size.
    It fired on all fifteen, including the good winter ones, so it carried no
    information at all.

The property is simple and scale-free: put a known number of valid cells
inside the AOI, and coverage must report that number over the AOI's own size,
whatever the frame around it happens to be.
"""

from __future__ import annotations

import numpy as np
import pytest

import synth
from gunw_reader import build_aoi_mask, read_gunw


def _stats(tmp_path, truth_mm=50.0):
    from gunw_reader import report
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=truth_mm)
    return report(read_gunw(f, auto_ref=True))


# ---------------------------------------------------------------------------
def test_coverage_is_reported_against_the_aoi_not_the_frame(tmp_path):
    """
    aoi_pct must be valid / AOI cells. frame_pct is kept, clearly named, and
    is a different and much smaller number.
    """
    s = _stats(tmp_path)
    assert s["aoi_px"] is not None and s["aoi_px"] > 0
    assert s["frame_px"] > s["aoi_px"], "the AOI must be smaller than the frame"

    assert s["aoi_pct"] == pytest.approx(100 * s["valid_px"] / s["aoi_px"], abs=0.02)
    assert s["frame_pct"] == pytest.approx(100 * s["valid_px"] / s["frame_px"], abs=0.02)
    assert s["aoi_pct"] > s["frame_pct"], (
        "coverage of the AOI must exceed coverage of the frame containing it - "
        "if these are equal the AOI clip is not being applied")


def test_the_two_denominators_are_not_interchangeable(tmp_path):
    """
    The specific mistake: reading a frame fraction as an AOI fraction. On this
    fixture the gap is a factor of several; on the real products it was ~16.
    """
    s = _stats(tmp_path)
    ratio = s["aoi_pct"] / s["frame_pct"]
    assert ratio > 2, (
        f"frame and AOI fractions differ by only {ratio:.2f}x here, so this "
        "fixture cannot demonstrate the confusion it exists to prevent")


def test_frame_fraction_keeps_enough_digits_to_be_a_number(tmp_path):
    """
    Rounded to 2 decimals, every real frame fraction over this AOI collapsed to
    one significant figure (0.01 .. 0.04) - which is where the quoted
    "range 1-4%" came from.
    """
    s = _stats(tmp_path)
    assert s["frame_pct"] == round(s["frame_pct"], 4)


def test_two_tracks_are_compared_on_the_same_denominator(tmp_path):
    """
    Same valid pixels, different frame sizes, must give the same AOI coverage.

    This is the property that makes an ascending/descending ratio meaningful.
    Under the frame fraction the larger frame reports lower "coverage" for
    identical ground, which is exactly how a 2.7x difference was published as
    3.5x.
    """
    aoi_px, valid_px = 12818, 7077                       # the real source-zone numbers
    small_frame, large_frame = 17_692_500, 26_630_000    # ASC and DESC frames

    aoi_small = 100 * valid_px / aoi_px
    aoi_large = 100 * valid_px / aoi_px
    assert aoi_small == aoi_large                        # denominator is the AOI

    frame_small = 100 * valid_px / small_frame
    frame_large = 100 * valid_px / large_frame
    assert frame_small / frame_large == pytest.approx(large_frame / small_frame, rel=1e-9)
    assert frame_small / frame_large > 1.4, (
        "the frame fraction rewards the smaller frame for identical ground")


def test_the_low_coverage_warning_can_actually_stay_silent(tmp_path, capsys):
    """
    A warning that fires every time is not a safeguard. Gated on the frame
    fraction it fired on all fifteen pairs; gated on AOI coverage it must be
    able to stay quiet on a well-covered scene.
    """
    _stats(tmp_path)
    out = capsys.readouterr().out
    assert "% of the area of interest" in out
    assert "under 10% of the scene survived gating" not in out


def test_no_aoi_clip_means_no_coverage_figure(tmp_path):
    """
    Without a clip there is no AOI to be a fraction of, so aoi_pct is None
    rather than silently falling back to the frame.
    """
    from gunw_reader import report
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0)
    s = report(read_gunw(f, auto_ref=True, clip_aoi=False))
    assert s["aoi_px"] is None
    assert s["aoi_pct"] is None
    assert s["frame_pct"] is not None


def test_aoi_cell_count_matches_the_mask_it_came_from(tmp_path):
    """aoi_px is the mask's own sum, not an area estimate from the ring."""
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0)
    r = read_gunw(f, auto_ref=True)
    mask = build_aoi_mask(r["xs"], r["ys"], __import__("gunw_reader").AOI_RING, r["epsg"])
    assert r["aoi_px"] == int(np.asarray(mask).sum())


def test_valid_pixels_never_exceed_the_aoi_they_are_inside(tmp_path):
    s = _stats(tmp_path)
    assert s["valid_px"] <= s["aoi_px"]
    assert s["aoi_pct"] <= 100.0
