"""
Regression tests for the GUNW reader.

Each test names the fault it exists to catch and asserts against a
displacement chosen before the file was written, never against a previous run
of this code. Comparing new output to old output is how a wrong answer
survives four rounds of "I re-ran it and it looks the same".
"""

from __future__ import annotations

import numpy as np
import pytest

import synth
from gunw_reader import check_consistency, read_gunw, verify_products

TOL_MM = 1.5


def _median_in_aoi(result) -> float:
    v = result["valid"]
    assert v.any(), "no valid pixels survived gating"
    return float(np.median(result["displacement_mm"][v]))


# ---------------------------------------------------------------------------
# The test that would have caught six of the seven silent faults
# ---------------------------------------------------------------------------
def test_known_displacement_round_trips(tmp_path):
    """50 mm in, 50 mm out. Sign, wavelength, scaling and gating, in one line."""
    f = tmp_path / synth.granule_name()
    truth = synth.write_gunw(f, truth_mm=50.0)
    got = _median_in_aoi(read_gunw(f, auto_ref=True))
    assert got == pytest.approx(truth["truth_mm"], abs=TOL_MM)


@pytest.mark.parametrize("truth_mm", [-120.0, -7.5, 0.0, 7.5, 120.0])
def test_round_trip_holds_across_sign_and_magnitude(tmp_path, truth_mm):
    """A sign error passes a single positive case; it does not pass this one."""
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=truth_mm)
    assert _median_in_aoi(read_gunw(f, auto_ref=True)) == pytest.approx(
        truth_mm, abs=TOL_MM)


def test_wavelength_is_read_from_the_file(tmp_path):
    """
    A hardcoded L-band constant is right for NISAR and silently wrong for
    anything else. Writing a different centre frequency must change the answer
    by exactly the wavelength ratio - or, correctly, not change it at all.
    """
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0, wavelength_m=0.0555)      # C-band
    r = read_gunw(f, auto_ref=True)
    assert r["wavelength_m"] == pytest.approx(0.0555, abs=1e-4)
    assert _median_in_aoi(r) == pytest.approx(50.0, abs=TOL_MM)


# ---------------------------------------------------------------------------
# The faults themselves
# ---------------------------------------------------------------------------
def test_reference_never_crosses_a_connected_component(tmp_path):
    """
    Ground far from the AOI sits in component 2 and is offset by two whole
    fringes. Referencing there injects 244 mm.

    This is the fault that produced summer medians of +2.34, +2.03, -2.44,
    +3.88 and -2.29 fringes, and it is invisible in the output: the numbers
    look like large displacements rather than like an error.
    """
    f = tmp_path / synth.granule_name()
    t = synth.write_gunw(f, truth_mm=50.0, aoi_component=1,
                         far_component=2, far_fringes=2.0)
    got = _median_in_aoi(read_gunw(f, auto_ref=True))
    assert got == pytest.approx(50.0, abs=TOL_MM)
    for k in (1, 2, -1, -2):
        assert abs(got - (50.0 + k * t["fringe_mm"])) > 10.0, (
            f"answer is one fringe multiple off: {got:.1f} mm")


def test_coherence_comes_from_the_unwrapped_grid(tmp_path):
    """
    The fixture carries a wrappedInterferogram coherence array four times
    finer, at a shorter path. A search ranking by path length matches it, and
    every quality gate is then applied to the wrong pixels.
    """
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0, coherence=0.9, include_wrapped_decoy=True)
    r = read_gunw(f, auto_ref=True)
    assert r["coherence"] is not None
    assert r["coherence"].shape == r["displacement_mm"].shape
    assert float(np.median(r["coherence"])) == pytest.approx(0.9, abs=0.02), \
        "picked up the decoy's 0.99, so it read the wrapped grid"


def test_mask_polarity_rejects_water_and_missing_subswaths(tmp_path):
    """
    The three-digit code again: `mask == 0` keeps exactly the pixels invalid in
    both acquisitions. Both stripes must be gone and the rest must survive.
    """
    f = tmp_path / synth.granule_name()
    t = synth.write_gunw(f, truth_mm=50.0, water_stripe=True, unusable_stripe=True)
    r = read_gunw(f, auto_ref=True)
    assert not r["valid"][:12, :].any(), "water pixels survived"
    assert not r["valid"][-12:, :].any(), "zero-subswath pixels survived"
    kept = int((r["valid"] & t["aoi"]).sum())
    assert kept > 0.5 * t["n_aoi_usable"], (
        f"only {kept} of {t['n_aoi_usable']} usable AOI pixels survived - "
        "polarity is likely inverted")


def test_ionosphere_screen_is_subtracted(tmp_path):
    """A 30 mm screen is added to the phase and must not reach the answer."""
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0, iono_mm=30.0)
    with_corr = _median_in_aoi(read_gunw(f, auto_ref=True, apply_iono=True))
    assert with_corr == pytest.approx(50.0, abs=TOL_MM)


def test_reference_lands_outside_the_aoi_when_it_can(tmp_path):
    """
    Referencing inside the AOI subtracts part of what is being measured and
    drives any AOI-wide signal toward zero - which is how a real 48 mm summer
    displacement was reported as -4.89 mm, and how a non-detection was made to
    look stronger than the data supports.
    """
    f = tmp_path / synth.granule_name()
    t = synth.write_gunw(f, truth_mm=50.0)
    r = read_gunw(f, auto_ref=True)
    assert r["ref_grid"] is not None, "no reference chosen at all"
    i, j = r["ref_grid"]
    assert not t["aoi"][i, j], f"reference landed inside the AOI at ({i}, {j})"
    assert _median_in_aoi(r) == pytest.approx(50.0, abs=TOL_MM)


def test_reference_survives_a_partly_invalid_scene(tmp_path):
    """
    Demanding a fully-valid block is too strict once coherence drops: six of
    fifteen real pairs found none outside the AOI and fell back inside it. With
    holes punched in the stable ground the answer must still come back right.
    """
    f = tmp_path / synth.granule_name()
    t = synth.write_gunw(f, truth_mm=50.0, hole_fraction=0.06)
    r = read_gunw(f, auto_ref=True)
    assert r["ref_grid"] is not None, (
        "no reference found - the search is too strict for a realistic scene")
    i, j = r["ref_grid"]
    assert not t["aoi"][i, j], "fell back to referencing inside the AOI"
    assert _median_in_aoi(r) == pytest.approx(50.0, abs=TOL_MM)


# ---------------------------------------------------------------------------
# Integrity and cross-product checks
# ---------------------------------------------------------------------------
def test_truncated_product_is_reported_not_skipped(tmp_path):
    """
    A short download raises only on open. In a batch that was one ERROR among
    hundreds of INFO lines, the loop continued, the run exited zero, and an
    epoch vanished from the network without comment.
    """
    good = tmp_path / synth.granule_name(ref="20260101", sec="20260113")
    bad = tmp_path / synth.granule_name(ref="20260113", sec="20260125")
    synth.write_gunw(good, truth_mm=50.0)
    synth.write_gunw(bad, truth_mm=50.0)
    with open(bad, "r+b") as fh:                       # lose the last 40%
        fh.truncate(int(bad.stat().st_size * 0.6))

    ok, broken = verify_products([good, bad])
    assert [p.name for p in ok] == [good.name]
    assert [p.name for p, _ in broken] == [bad.name]


def test_duplicate_products_flag_a_whole_fringe_disagreement():
    """
    PR and UR builds of identical acquisitions cannot disagree about ground
    motion, so any gap is processing error - and a gap landing on a fringe
    multiple names its own cause.
    """
    rows = [
        {"reference": "20260816", "secondary": "20260828", "median": -14.66,
         "mean_coherence": 0.64, "file": "NISAR_L2_PR_GUNW_x.h5",
         "wavelength_m": synth.WAVELENGTH_M},
        {"reference": "20260816", "secondary": "20260828", "median": -279.80,
         "mean_coherence": 0.64, "file": "NISAR_L2_UR_GUNW_x.h5",
         "wavelength_m": synth.WAVELENGTH_M},
    ]
    flagged = check_consistency(rows)
    assert len(flagged) == 1
    assert flagged[0]["fringes"] == pytest.approx(2.17, abs=0.05)


def test_a_duplicate_with_no_valid_pixels_is_reported_not_crashed_on():
    """
    report() returns early without a 'median' when a pair has nothing valid, so
    check_consistency raised KeyError on any duplicate group containing one.

    That is where duplicates actually live: monsoon pairs run at 1-3% AOI
    coverage and the co-event pair ships both a routine and an urgent product.
    A batch would die after the last product and before the CSV was written -
    the same failure mode, and the same silence, as the key mismatch above.
    """
    rows = [
        {"reference": "20260816", "secondary": "20260828", "valid_px": 0,
         "file": "NISAR_L2_PR_GUNW_x.h5"},                       # no 'median'
        {"reference": "20260816", "secondary": "20260828", "median": -14.66,
         "mean_coherence": 0.64, "file": "NISAR_L2_UR_GUNW_x.h5",
         "wavelength_m": synth.WAVELENGTH_M},
    ]
    assert check_consistency(rows) == []                # must not raise, must not flag


def test_consistency_sizes_a_fringe_from_the_products_own_wavelength():
    """
    report() never emitted wavelength_m, so the fringe test silently fell back
    to the module constant. A product on a different centre frequency would
    then be judged against the wrong cycle length.
    """
    rows = [
        {"reference": "20260816", "secondary": "20260828", "median": 0.0,
         "mean_coherence": 0.6, "file": "NISAR_L2_PR_GUNW_x.h5",
         "wavelength_m": 0.4},
        {"reference": "20260816", "secondary": "20260828", "median": 200.0,
         "mean_coherence": 0.6, "file": "NISAR_L2_UR_GUNW_x.h5",
         "wavelength_m": 0.4},
    ]
    flagged = check_consistency(rows)
    assert len(flagged) == 1
    # one fringe at lambda = 0.4 m is 200 mm, so this gap is exactly one cycle
    assert flagged[0]["fringes"] == pytest.approx(1.0, abs=0.01)


def test_consistency_survives_a_product_with_no_coherence_layer():
    rows = [
        {"reference": "20260816", "secondary": "20260828", "median": 0.0,
         "mean_coherence": None, "file": "NISAR_L2_PR_GUNW_x.h5"},
        {"reference": "20260816", "secondary": "20260828", "median": 5.0,
         "mean_coherence": None, "file": "NISAR_L2_UR_GUNW_x.h5"},
    ]
    check_consistency(rows)                             # must not raise


def test_nodata_is_decided_on_raw_phase_not_the_ionosphere_corrected_array(tmp_path):
    """
    GUNW fills nodata with exact zeros and the ionosphere screen is not zero
    there, so subtracting the screen first turns every nodata pixel into a
    small non-zero number and the `phase != 0` test stops finding any of them.

    Reading with and without the screen must therefore gate the SAME pixels.
    It survived only because connectedComponents happens to reject the same
    cells, and that layer is optional.
    """
    f = tmp_path / synth.granule_name()
    truth = synth.write_gunw(f, truth_mm=50.0, iono_mm=40.0, nodata_fraction=0.20)
    assert truth["n_nodata"] > 500, "fixture must actually contain unfilled cells"

    r = read_gunw(f, auto_ref=True, apply_iono=True)

    # Not one unfilled cell may be counted as a measurement, screen or no screen.
    leaked = int((r["valid"] & truth["nodata"]).sum())
    assert leaked == 0, (
        f"{leaked} of {truth['n_nodata']} nodata cells survived gating - the "
        "phase == 0 test is running on the ionosphere-corrected array")

    # And the decision must not depend on whether the screen was removed.
    without = read_gunw(f, auto_ref=True, apply_iono=False)
    assert np.array_equal(r["valid"], without["valid"])


def test_consistency_check_runs_on_reader_output_keys(tmp_path):
    """
    It once looked up "reference_date" while report() returned "reference".
    Every batch raised KeyError after the last product and before the CSV was
    written; piping stderr through grep hid it, and a stale CSV was read as
    current for two rounds. The keys must match what the reader actually emits.
    """
    from gunw_reader import report
    f = tmp_path / synth.granule_name()
    synth.write_gunw(f, truth_mm=50.0)
    row = report(read_gunw(f, auto_ref=True))
    check_consistency([row, dict(row)])                # must not raise
    for key in ("reference", "secondary", "median", "mean_coherence", "file"):
        assert key in row, f"report() no longer emits {key!r}"


# ---------------------------------------------------------------------------
# Track has to survive into the derived statistics.
# ---------------------------------------------------------------------------
def test_track_is_recovered_from_the_granule_name():
    """
    Export filenames carry only dates, so nothing downstream can recover the
    geometry - and geometry is the largest single control on data quality
    here, with floors differing about fivefold between the two tracks. Pooling
    them gives a median that describes neither, which is the trap the GOFF
    season analysis documents and local_floor.py fell into.
    """
    from gunw_reader import track_of
    assert track_of("NISAR_L2_PR_GOFF_006_098_A_016_007_4000_SH_20251128T233919") == "ASC 098"
    assert track_of("NISAR_L2_PR_GUNW_006_048_D_074_007_2000_SH_20251125T125813") == "DESC 048"
    assert track_of("NISAR_L2_UR_GOFF_028_098_A_016_029_4000_SH_20260819T233918") == "ASC 098"


def test_a_name_that_is_not_a_granule_yields_no_track():
    """Silence beats a wrong label: an empty track is visibly missing."""
    from gunw_reader import track_of
    assert track_of("GOFF_20251128_20251210_PR_HH-layer2.tif") == ""
    assert track_of("") == ""
