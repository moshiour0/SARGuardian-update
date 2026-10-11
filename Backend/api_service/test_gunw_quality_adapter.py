from __future__ import annotations

from urllib.parse import urlencode

import numpy as np
import pytest

import harmony_extraction as he


GRANULE = (
    "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_"
    "20260823T130631_20260823T130706_P05023_N_F_J_001"
)


def _synthetic_layer(role, values, inside, valid_data, grid=None):
    if grid is None:
        grid = {
            "crs": "EPSG:32644",
            "width": 100,
            "height": 100,
            "transform": (80.0, 0.0, 500000.0, 0.0, -80.0, 3300000.0, 0.0, 0.0, 1.0),
            "window_transform": (80.0, 0.0, 500000.0, 0.0, -80.0, 3300000.0, 0.0, 0.0, 1.0),
            "window_width": int(values.shape[1]),
            "window_height": int(values.shape[0]),
            "window_pixels": int(values.size),
            "window_bounds_wgs84": [82.5, 27.4, 85.8, 30.1],
        }
    return {
        "role": role,
        "values": np.asarray(values),
        "inside": np.asarray(inside, dtype=bool),
        "valid_data": np.asarray(valid_data, dtype=bool),
        "grid": dict(grid),
        "nodata": None,
        "result_filename": f"{role}.tif",
        "job_id": f"job-{role}",
        "expected_variable": he._expected_gunw_variable(role, "HH"),
    }


def test_expected_gunw_variables_are_polarization_specific():
    assert he._expected_gunw_variable("unwrapped_phase", "HH").endswith("/HH/unwrappedPhase")
    assert he._expected_gunw_variable("coherence", "VV").endswith("/VV/coherenceMagnitude")
    assert he._expected_gunw_variable("ionosphere_phase_screen_uncertainty", "HH").endswith("/HH/ionospherePhaseScreenUncertainty")
    assert he._expected_gunw_variable("mask", "HH").endswith("/unwrappedInterferogram/mask")


def test_quality_analysis_mirrors_core_reader_gates(monkeypatch):
    inside = np.array([[True, True], [True, True]])
    valid = np.ones((2, 2), dtype=bool)
    phase = np.array([[1.0, 2.0], [0.0, 4.0]])
    coh = np.array([[0.8, 0.2], [0.9, 0.95]])
    comp = np.array([[1, 1], [2, 0]])
    iono = np.array([[0.1, 0.1], [0.1, 0.1]])
    # Encoded as dry land and valid ref/secondary subswaths: 11; invalid 0; water 111.
    mask = np.array([[11, 11], [11, 111]])
    layer_map = {
        "unwrapped_phase": _synthetic_layer("unwrapped_phase", phase, inside, valid),
        "coherence": _synthetic_layer("coherence", coh, inside, valid),
        "connected_components": _synthetic_layer("connected_components", comp, inside, valid),
        "ionosphere_phase_screen": _synthetic_layer("ionosphere_phase_screen", iono, inside, valid),
        "mask": _synthetic_layer("mask", mask, inside, valid),
        "ionosphere_phase_screen_uncertainty": _synthetic_layer(
            "ionosphere_phase_screen_uncertainty", np.full((2, 2), 0.05), inside, valid
        ),
    }
    monkeypatch.setattr(
        he,
        "_read_job_aoi_layer",
        lambda *, job_id, granule_name, polarization, role, coordinates: layer_map[role],
    )
    result = he.analyze_gunw_aoi_layers(
        granule_name=GRANULE,
        polarization="HH",
        coordinates=[(82.5, 27.4), (82.6, 27.4), (82.6, 27.5)],
        job_ids={
            "unwrapped_phase": "phase-job",
            "coherence": "coh-job",
            "connected_components": "comp-job",
            "ionosphere_phase_screen": "iono-job",
            "mask": "mask-job",
            "ionosphere_phase_screen_uncertainty": "unc-job",
        },
        coherence_threshold=0.3,
    )
    gates = result["quality_gates"]
    assert gates["phase_nonzero_finite_pixels"] == 3
    assert gates["coherence_pass_pixels"] == 3
    assert gates["connected_components_positive_pixels"] == 3
    assert gates["mask_valid_land_subswath_pixels"] == 3
    assert gates["jointly_qualified_pixels"] == 1
    assert result["physical_displacement"]["computed"] is False
    assert result["original_scientific_engine"]["hdf5_read_gunw_invoked"] is False
    assert result["layers"]["ionosphere_phase_screen_uncertainty"]["mean"] == pytest.approx(0.05)
    # The new diagnostics must expose categorical values and gate-by-gate drops.
    assert result["layer_diagnostics"]["connected_components"]["unique_value_count"] == 3
    assert result["layer_diagnostics"]["connected_components"]["zero_value_pixels_among_valid_data"] == 1
    assert result["quality_gates"]["phase_and_coherence_pixels"] == 2
    assert result["quality_gates"]["after_connected_components_pixels"] == 1
    assert result["quality_gates"]["after_ionosphere_validity_pixels"] == 1
    assert result["quality_gates"]["after_land_and_subswath_mask_pixels"] == 1


def test_quality_analysis_refuses_misaligned_layers(monkeypatch):
    inside = np.ones((2, 2), dtype=bool)
    valid = np.ones((2, 2), dtype=bool)
    base = {
        "unwrapped_phase": _synthetic_layer("unwrapped_phase", np.ones((2, 2)), inside, valid),
        "coherence": _synthetic_layer("coherence", np.ones((2, 2)), inside, valid),
        "connected_components": _synthetic_layer("connected_components", np.ones((2, 2)), inside, valid),
        "ionosphere_phase_screen": _synthetic_layer("ionosphere_phase_screen", np.zeros((2, 2)), inside, valid),
        "mask": _synthetic_layer("mask", np.full((2, 2), 11), inside, valid),
    }
    bad_grid = dict(base["coherence"]["grid"])
    bad_grid["transform"] = (81.0, 0.0, 500000.0, 0.0, -80.0, 3300000.0, 0.0, 0.0, 1.0)
    base["coherence"]["grid"] = bad_grid
    monkeypatch.setattr(
        he,
        "_read_job_aoi_layer",
        lambda *, job_id, granule_name, polarization, role, coordinates: base[role],
    )
    with pytest.raises(he.HarmonyJobError, match="geotransform mismatch"):
        he.analyze_gunw_aoi_layers(
            granule_name=GRANULE,
            polarization="HH",
            coordinates=[(82.5, 27.4), (82.6, 27.4), (82.6, 27.5)],
            job_ids={
                "unwrapped_phase": "phase",
                "coherence": "coh",
                "connected_components": "comp",
                "ionosphere_phase_screen": "iono",
                "mask": "mask",
            },
        )


def test_start_quality_jobs_validates_existing_phase_granule_and_starts_companions(monkeypatch):
    phase_var = he._expected_gunw_variable("unwrapped_phase", "HH")
    request = "https://harmony.earthdata.nasa.gov/C123/coverage/rangeset?" + urlencode({
        "granuleName": GRANULE,
        "variable": phase_var,
    })
    monkeypatch.setattr(he, "get_harmony_job_status", lambda job_id: {"job": {"status": "successful", "request": request}})
    submitted = []

    def fake_submit(**kwargs):
        submitted.append(kwargs["layer_role"])
        return {"job_id": f"id-{kwargs['layer_role']}", "job_status_url": "https://harmony.earthdata.nasa.gov/jobs/fake", "variable": he._expected_gunw_variable(kwargs["layer_role"], "HH")}

    monkeypatch.setattr(he, "submit_single_layer_job", fake_submit)
    result = he.start_gunw_quality_layer_jobs(
        granule_name=GRANULE,
        phase_job_id="phase-job-123456",
        maturity="PROVISIONAL",
        polarization="HH",
        confirm_full_granule_layers=True,
        include_iono_uncertainty=True,
    )
    assert result["all_required_layers_submitted"] is True
    assert result["jobs"]["unwrapped_phase"]["reused_existing_job"] is True
    assert set(submitted) == {"coherence", "connected_components", "ionosphere_phase_screen", "mask", "ionosphere_phase_screen_uncertainty"}


def test_all_zero_component_layer_is_reported_as_quality_gate_problem(monkeypatch):
    inside = np.ones((2, 2), dtype=bool)
    valid = np.ones((2, 2), dtype=bool)
    layer_map = {
        "unwrapped_phase": _synthetic_layer("unwrapped_phase", np.full((2, 2), -2.0), inside, valid),
        "coherence": _synthetic_layer("coherence", np.full((2, 2), 0.8), inside, valid),
        "connected_components": _synthetic_layer("connected_components", np.zeros((2, 2), dtype=np.uint32), inside, valid),
        "ionosphere_phase_screen": _synthetic_layer("ionosphere_phase_screen", np.zeros((2, 2)), inside, valid),
        "mask": _synthetic_layer("mask", np.full((2, 2), 11), inside, valid),
    }
    layer_map["connected_components"]["dtype"] = "uint32"
    monkeypatch.setattr(
        he,
        "_read_job_aoi_layer",
        lambda *, job_id, granule_name, polarization, role, coordinates: layer_map[role],
    )
    result = he.analyze_gunw_aoi_layers(
        granule_name=GRANULE,
        polarization="HH",
        coordinates=[(82.5, 27.4), (82.6, 27.4), (82.6, 27.5)],
        job_ids={"unwrapped_phase": "phase", "coherence": "coh", "connected_components": "comp", "ionosphere_phase_screen": "iono", "mask": "mask"},
    )
    assert result["quality_gates"]["phase_and_coherence_pixels"] == 4
    assert result["quality_gates"]["connected_components_positive_pixels"] == 0
    assert result["quality_gates"]["after_connected_components_pixels"] == 0
    assert result["layer_diagnostics"]["connected_components"]["min_valid"] == 0
    assert result["layer_diagnostics"]["connected_components"]["max_valid"] == 0
    assert any("not evidence that no ground movement occurred" in warning for warning in result["warnings"])
