"""
The site must not drift from the science.

Why this exists
===============
docs/site/ lets a visitor run the analysis in the browser, which means the
arithmetic exists twice: once in Python and once in JavaScript. Two
implementations of the same formula is exactly the arrangement that silently
diverges, so the extractor ships a block of reference values computed by this
project's own functions and the page checks itself against them.

These tests hold the shipped block honest: that it exists, that it still
matches what the functions produce today, and that the units are the ones the
rasters are actually written in - which is the mistake that put the detection
floor at 21,209 mm/day instead of 21.2.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "site" / "site_data.json"

pytestmark = pytest.mark.skipif(not SITE.exists(),
                                reason="run docs/site/build_data.py first")


@pytest.fixture(scope="module")
def site():
    return json.loads(SITE.read_text(encoding="utf-8"))


def test_the_reference_floors_match_the_project_functions(site):
    """
    The browser recomputes sigma and the floor from the shipped pixels. If the
    reference it checks against were stale, the page would happily print
    'verified' while showing a different number from the repository.
    """
    from local_floor import detection_floor, robust_sigma
    import numpy as np
    by_pair = {o["pair"]: o for o in site["offsets"]}
    assert by_pair, "no offset samples shipped"
    for pair, ref in site["reference"]["floors"].items():
        v = np.array(by_pair[pair]["sample_mm"], dtype=float)
        assert robust_sigma(v) == pytest.approx(ref["sigma_mm"], abs=0.01)
        assert detection_floor(v, by_pair[pair]["span_days"]) == \
            pytest.approx(ref["floor_mm_day"], abs=0.01)


def test_the_reference_geometry_matches_geometry_merge(site):
    """Station 6 is the one that inverted once. It checks against this."""
    from geometry_merge import sensitivities
    for aspect, expect in site["reference"]["sensitivity"].items():
        rows = {r["track"]: r["sensitivity"]
                for r in sensitivities(28.27802, 85.52963, 39.0, float(aspect), 0.3)}
        assert set(rows) == set(expect)
        for t, v in expect.items():
            assert rows[t] == pytest.approx(v, abs=0.001)


def test_offset_samples_are_in_millimetres(site):
    """
    The exports are written in mm. Treating them as metres and scaling put the
    floor three orders of magnitude out, and it looked plausible enough in a
    JSON blob that only the cross-check caught it.
    """
    stats = {r["reference"] + "_" + r["secondary"]: float(r["range_mad_sigma_mm"])
             for r in csv.DictReader(open(ROOT / "outputs" / "goff_stats_source.csv"))
             if r["layer"] == "HH/layer2" and r["processing"] == "PR"}
    for pair, ref in site["reference"]["floors"].items():
        assert ref["sigma_mm"] == pytest.approx(stats[pair], rel=0.10), (
            f"{pair}: shipped sigma {ref['sigma_mm']} against "
            f"{stats[pair]} in the stats CSV")


def test_every_shipped_cluster_carries_a_terrain_reading(site):
    """Terrain is what separates a detachment from a deposit or from melt."""
    assert site["clusters"], "no clusters shipped"
    for c in site["clusters"]:
        assert c["reading"] in {"DETACHMENT-like", "deposit / flat",
                                "south-facing -> melt", "ambiguous"}
        for k in ("elev_m", "slope_deg", "aspect_deg", "area_km2"):
            assert isinstance(c[k], (int, float))


def test_the_detachment_candidates_all_face_north(site):
    """
    This is the conclusion that does not depend on picking one candidate, and
    the site states it as such. If it ever stops being true the site is lying.
    """
    det = [c for c in site["clusters"] if c["reading"] == "DETACHMENT-like"]
    assert len(det) >= 2, "the plural claim needs more than one candidate"
    for c in det:
        off = min(abs(c["aspect_deg"]), abs(c["aspect_deg"] - 360))
        assert off <= 60, f"cluster {c['id']} faces {c['aspect_deg']}"


def test_the_reported_failure_point_is_observed_and_not_a_scar(site):
    """
    The one clean negative in the whole optical analysis. It only carries if
    the point was actually seen - otherwise it is absence of data.
    """
    p = site["probe"]
    assert p["observed"] is True
    assert p["n_usable"] >= 100, "too few pixels for the negative to mean anything"
    assert p["d_ndsi_median"] > 0, "it got brighter; that is the point"
    assert p["scar_like_pct"] == 0.0


def test_the_payload_cannot_break_out_of_its_script_tag(site):
    raw = SITE.read_text(encoding="utf-8")
    assert "</script" not in raw.lower()
