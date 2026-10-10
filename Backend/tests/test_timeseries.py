"""
Network and inversion tests.

The faults here are subtler than a wrong number: they are cases where least
squares returns something confident and meaningless. A rank-deficient system
does not raise - it returns the minimum-norm solution, which for a split
network is an average across a gap that carries no information at all.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from timeseries import (Pair, connected_components, invert_component,
                        pairs_from_stats)


def _chain(dates, values, direction="D", path=48):
    return [Pair(path=path, direction=direction, ref=a, sec=b, value=v)
            for (a, b), v in zip(zip(dates, dates[1:]), values)]


D = [date(2026, 6, 29), date(2026, 7, 11), date(2026, 7, 23),
     date(2026, 8, 4), date(2026, 8, 16)]


# ---------------------------------------------------------------------------
def test_inversion_recovers_a_known_cumulative_series():
    """Three 10 mm steps must come back as 0, 10, 20, 30 - the whole point."""
    pairs = _chain(D[:4], [10.0, 10.0, 10.0])
    r = invert_component(pairs, D[:4])
    assert r["cumulative_mm"] == pytest.approx([0, 10, 20, 30], abs=1e-6)
    assert r["velocity_mm_per_day"] == pytest.approx(10.0 / 12, abs=1e-6)


def test_disconnected_epochs_never_join():
    """
    Winter and summer share no interferogram. Their displacements have separate
    zeros and are not comparable; the network must say so rather than produce a
    single series spanning the gap.
    """
    winter = _chain([date(2025, 11, 25), date(2025, 12, 7)], [5.0])
    summer = _chain(D[:3], [10.0, 10.0])
    comps = connected_components(winter + summer)
    assert len(comps) == 2
    assert all(len(c) >= 2 for c in comps)


def test_redundancy_is_reported_honestly():
    """
    A chain has as many pairs as unknowns. That is not a good fit, it is no fit
    - there is no residual and no error estimate to be had, and reporting one
    would be inventing information.
    """
    pairs = _chain(D[:4], [10.0, 10.0, 10.0])
    r = invert_component(pairs, D[:4])
    assert r["dof"] == 0
    assert not np.isfinite(r["sigma_mm"])
    assert not np.any(np.isfinite(r["error_mm"][1:]))


def test_a_loop_gives_redundancy_and_a_real_residual():
    """
    Add one 24-day pair spanning two 12-day steps and the network gains a
    closure. The residual becomes measurable - which is exactly the redundancy
    the real archive cannot supply, and worth a test so we recognise it if it
    ever arrives.
    """
    pairs = _chain(D[:3], [10.0, 10.0])
    pairs.append(Pair(path=48, direction="D", ref=D[0], sec=D[2], value=21.0))
    r = invert_component(pairs, D[:3])
    assert r["dof"] == 1
    assert np.isfinite(r["sigma_mm"]) and r["sigma_mm"] > 0


def test_removing_an_interior_link_splits_the_chain():
    """
    The jackknife fault. Dropping a middle pair does not weaken the network, it
    breaks it in two - and lstsq will still answer, bridging the gap with a
    minimum-norm guess. Refitting that and calling it a robustness check
    invents agreement or disagreement at random.
    """
    pairs = _chain(D, [10.0, 10.0, 10.0, 10.0])
    assert len(connected_components(pairs)) == 1
    without_middle = pairs[:1] + pairs[2:]
    assert len(connected_components(without_middle)) == 2, (
        "removing an interior link must disconnect a chain")


def test_stats_csv_round_trips_through_the_network(tmp_path):
    """
    --from-stats reproduces the series from a 200-byte row instead of a 2.4 GB
    product. If the parse drifts, the whole team workflow silently changes
    answer.
    """
    import csv
    import synth
    p = tmp_path / "stats.csv"
    rows = [{"file": synth.granule_name(ref="20260629", sec="20260711"),
             "reference": "20260629", "secondary": "20260711",
             "median": 10.0, "mean_coherence": 0.6, "valid_px": 1000},
            {"file": synth.granule_name(ref="20260711", sec="20260723"),
             "reference": "20260711", "secondary": "20260723",
             "median": 10.0, "mean_coherence": 0.6, "valid_px": 1000}]
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    pairs = pairs_from_stats(p)
    assert len(pairs) == 2
    assert all(q.direction == "D" and q.path == 48 for q in pairs)
    epochs = sorted({e for q in pairs for e in (q.ref, q.sec)})
    assert invert_component(pairs, epochs)["cumulative_mm"] == pytest.approx(
        [0, 10, 20], abs=1e-6)


def test_duplicate_products_are_resolved_not_averaged(tmp_path):
    """
    PR and UR of the same acquisitions can differ by whole fringes. Averaging
    them produces a number that is neither; the branch consistent with the rest
    of the geometry must be kept and the other dropped.
    """
    import csv
    import synth
    p = tmp_path / "dupes.csv"
    common = dict(reference="20260816", secondary="20260828",
                  mean_coherence=0.64, valid_px=1000)
    rows = [
        {"file": synth.granule_name(proc="PR", ref="20260816", sec="20260828"),
         "median": -14.66, **common},
        {"file": synth.granule_name(proc="UR", ref="20260816", sec="20260828"),
         "median": -279.80, **common},
        {"file": synth.granule_name(proc="PR", ref="20260629", sec="20260711"),
         "median": 10.0, "reference": "20260629", "secondary": "20260711",
         "mean_coherence": 0.6, "valid_px": 1000},
    ]
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "reference", "secondary",
                                           "median", "mean_coherence", "valid_px"])
        w.writeheader(); w.writerows(rows)

    pairs = pairs_from_stats(p)
    assert len(pairs) == 2, "the duplicate was not resolved to one pair"
    kept = [q for q in pairs if q.ref == date(2026, 8, 16)][0]
    assert kept.value == pytest.approx(-14.66), (
        "kept the fringe-offset branch, or averaged the two")


# ---------------------------------------------------------------------------
# One datum per geometry
# ---------------------------------------------------------------------------
def _lattice_stack(errs, target_mm=40.0, shape=(60, 60), block=slice(10, 40)):
    """
    Pairs on one lattice, each carrying its own datum error, with a moving
    target covering a large block - large enough that a datum allowed onto
    it would be dragged by its motion.
    """
    from rasterio.transform import from_origin
    rng = np.random.default_rng(7)
    grid = {"transform": from_origin(0.0, 60 * 80.0, 80.0, 80.0), "res_x": 80.0}
    pairs, lat = [], {}
    for k, e in enumerate(errs):
        a = rng.normal(0, 2, shape)
        a[block, block] += target_mm
        from datetime import timedelta
        ref = date(2026, 7, 2) + timedelta(days=12 * k)
        p = Pair(path=98, direction="A", ref=ref, sec=ref + timedelta(days=12),
                 source=f"p{k}")
        pairs.append(p)
        lat[id(p)] = (a + e, grid, 4326)
    return pairs, lat


def _run(pairs, lat, buffer_km):
    from timeseries import apply_common_datum
    # target at the block centre: row 25, col 25 -> x = 25.5*80, y = (60-25.5)*80
    apply_common_datum(pairs, lat, target_lat=(60 - 25.5) * 80.0,
                       target_lon=25.5 * 80.0, radius_px=3, buffer_km=buffer_km)


def test_common_datum_removes_each_pairs_private_error():
    """Three pairs, three different datum errors; the target reads 40 in all."""
    pairs, lat = _lattice_stack([-46.0, 21.0, -12.0])
    _run(pairs, lat, buffer_km=1.6)
    for p in pairs:
        assert p.value == pytest.approx(40.0, abs=2.0)


def test_common_datum_is_not_set_on_the_moving_target():
    """
    With the target excluded, the datum is the stable ground and the target
    reads its true 40 mm. With no buffer the moving block drags the datum.
    """
    pairs, lat = _lattice_stack([0.0, 0.0, 0.0], block=slice(0, 50))
    _run(pairs, lat, buffer_km=2.0)
    good = [p.value for p in pairs]
    pairs2, lat2 = _lattice_stack([0.0, 0.0, 0.0], block=slice(0, 50))
    _run(pairs2, lat2, buffer_km=0.0)
    assert all(v == pytest.approx(40.0, abs=2.0) for v in good)
    assert all(p.value < 20.0 for p in pairs2)
