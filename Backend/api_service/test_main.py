from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def square(area_scale=1.0):
    # ~12.3 km² at the equator for 0.1 x 0.1 degrees; scale for area tests.
    delta = 0.1 * area_scale
    return [[0.0, 0.0], [delta, 0.0], [delta, delta], [0.0, delta]]


def test_health_is_metadata_only():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "metadata-plus-on-demand-window-read"
    assert body["full_raster_downloaded_or_persisted"] is False


def test_catalog_preflight_returns_metadata_and_volume_without_downloading(monkeypatch):
    monkeypatch.setattr(
        main,
        "_query_asf",
        lambda params: [
            {
                "granuleName": "NISAR_L2_PR_GUNW_001_A_20260801T000000_20260813T000000.h5",
                "productType": "GUNW",
                "sizeMB": "2048",
                "flightDirection": "ASCENDING",
                "path": 98,
            },
            {
                "granuleName": "NISAR_L2_PR_GUNW_002_A_20260813T000000_20260825T000000.h5",
                "productType": "GUNW",
                "sizeMB": 1024,
                "flightDirection": "ASCENDING",
                "path": 98,
            },
        ],
    )
    response = client.post(
        "/api/catalog/search",
        json={
            "coordinates": square(),
            "start_date": "2026-08-01",
            "end_date": "2026-09-01",
            "product_type": "GUNW",
        },
        headers={"x-forwarded-for": "test-preflight-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "catalog-preflight-only"
    assert body["data_downloaded"] is False
    assert body["scientific_analysis_performed"] is False
    assert body["product_count"] == 2
    assert body["estimated_full_granule_volume_gib"] == 3.0
    assert body["products"][0]["size_mb"] == 2048.0


def test_rejects_more_than_90_days():
    response = client.post(
        "/api/catalog/search",
        json={
            "coordinates": square(),
            "start_date": "2026-01-01",
            "end_date": "2026-05-01",
            "product_type": "GUNW",
        },
        headers={"x-forwarded-for": "test-preflight-2"},
    )
    assert response.status_code == 422
    assert "cannot exceed 90 days" in response.text


def test_rejects_wrong_product_type():
    response = client.post(
        "/api/catalog/search",
        json={
            "coordinates": square(),
            "start_date": "2026-08-01",
            "end_date": "2026-08-20",
            "product_type": "GUWN",
        },
        headers={"x-forwarded-for": "test-preflight-3"},
    )
    assert response.status_code == 422


def test_rejects_area_above_limit():
    response = client.post(
        "/api/catalog/search",
        json={
            "coordinates": square(area_scale=4),
            "start_date": "2026-08-01",
            "end_date": "2026-08-20",
            "product_type": "GOFF",
        },
        headers={"x-forwarded-for": "test-preflight-4"},
    )
    assert response.status_code == 422
    assert "free demo limit" in response.text


def test_crop_aoi_endpoint_validates_polygon_and_returns_window_result(monkeypatch):
    captured = {}

    def fake_crop(job_id, coords):
        captured["job_id"] = job_id
        captured["coords"] = coords
        return {
            "service": "SARGuardian remote COG AOI window inspector",
            "aoi_cropped": True,
            "full_raster_downloaded": False,
            "scientific_analysis_performed": False,
            "aoi_window": {"width": 20, "height": 18, "window_pixels": 360},
        }

    monkeypatch.setattr(main, "crop_harmony_job_result_aoi", fake_crop)
    response = client.post(
        "/api/harmony/jobs/ea93e3fa-cffb-422a-91d6-953d6f3498cf/crop-aoi",
        json={"coordinates": square()},
        headers={"x-forwarded-for": "test-crop-window-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["aoi_cropped"] is True
    assert body["aoi_area_km2"] > 0
    assert captured["job_id"] == "ea93e3fa-cffb-422a-91d6-953d6f3498cf"
    assert len(captured["coords"]) == 4


def test_crop_aoi_endpoint_rejects_invalid_polygon(monkeypatch):
    monkeypatch.setattr(
        main, "crop_harmony_job_result_aoi",
        lambda *args: (_ for _ in ()).throw(AssertionError("Should reject before remote read")),
    )
    response = client.post(
        "/api/harmony/jobs/ea93e3fa-cffb-422a-91d6-953d6f3498cf/crop-aoi",
        json={"coordinates": [[0, 0], [0.1, 0.1]]},
        headers={"x-forwarded-for": "test-crop-window-2"},
    )
    assert response.status_code == 422


def test_gunw_quality_start_endpoint_passes_valid_request_to_bundle_helper(monkeypatch):
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {
            "service": "SARGuardian GUNW companion-layer jobs",
            "mode": "multi-layer-jobs-submitted",
            "jobs": {
                "unwrapped_phase": {"job_id": kwargs["phase_job_id"], "status": "successful"},
                "coherence": {"job_id": "coherence-123456", "status": "submitted"},
                "connected_components": {"job_id": "components-123456", "status": "submitted"},
                "ionosphere_phase_screen": {"job_id": "ionosphere-123456", "status": "submitted"},
                "mask": {"job_id": "mask-job-123456", "status": "submitted"},
            },
            "submission_errors": {},
            "all_required_layers_submitted": True,
        }

    monkeypatch.setattr(main, "start_gunw_quality_layer_jobs", fake_start)
    response = client.post(
        "/api/harmony/gunw-quality/start",
        json={
            "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
            "phase_job_id": "phase-job-123456",
            "coordinates": square(),
            "maturity": "PROVISIONAL",
            "polarization": "HH",
            "confirm_full_granule_layers": True,
            "include_iono_uncertainty": False,
        },
        headers={"x-forwarded-for": "test-quality-start-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["all_required_layers_submitted"] is True
    assert body["aoi_coordinates_received"] == 4
    assert captured["phase_job_id"] == "phase-job-123456"
    assert captured["confirm_full_granule_layers"] is True


def test_gunw_quality_analyze_endpoint_passes_job_ids_and_aoi(monkeypatch):
    captured = {}

    def fake_analyze(**kwargs):
        captured.update(kwargs)
        return {
            "service": "SARGuardian GUNW AOI quality-gate diagnostics",
            "mode": "multi-layer-aoi-quality-diagnostics",
            "physical_displacement": {"computed": False, "unit": None, "reason": "Reference required."},
            "quality_gates": {"jointly_qualified_pixels": 42, "jointly_qualified_fraction_of_polygon": 0.6, "coherence_threshold": 0.3},
        }

    monkeypatch.setattr(main, "analyze_gunw_aoi_layers", fake_analyze)
    response = client.post(
        "/api/harmony/gunw-quality/analyze-aoi",
        json={
            "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
            "polarization": "HH",
            "coordinates": square(),
            "phase_job_id": "phase-job-123456",
            "coherence_job_id": "coherence-123456",
            "connected_components_job_id": "components-123456",
            "ionosphere_phase_screen_job_id": "ionosphere-123456",
            "mask_job_id": "mask-job-123456",
            "ionosphere_phase_screen_uncertainty_job_id": "uncertainty-123456",
            "coherence_threshold": 0.3,
        },
        headers={"x-forwarded-for": "test-quality-analyze-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quality_gates"]["jointly_qualified_pixels"] == 42
    assert body["physical_displacement"]["computed"] is False
    assert captured["job_ids"]["unwrapped_phase"] == "phase-job-123456"
    assert captured["job_ids"]["ionosphere_phase_screen_uncertainty"] == "uncertainty-123456"
