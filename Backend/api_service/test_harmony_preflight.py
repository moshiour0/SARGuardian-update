from fastapi.testclient import TestClient

import harmony_preflight
import main

client = TestClient(main.app)

SAMPLE_CAPABILITIES = {
    "conceptId": "C1234567890-ASF",
    "shortName": "NISAR_L2_PR_GUNW",
    "summary": {"variableSubset": True, "bboxSubset": False, "shapeSubset": False},
    "services": [
        {
            "name": "asf/nisar-hdf5-subsetter",
            "href": "https://harmony.earthdata.nasa.gov/example-service",
            "capabilities": {
                "variableSubset": True,
                "bboxSubset": False,
                "shapeSubset": False,
                "outputFormats": ["image/tiff"],
            },
        }
    ],
    "variables": [
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/unwrappedPhase"},
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/coherenceMagnitude"},
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/connectedComponents"},
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/ionospherePhaseScreen"},
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/mask"},
    ],
}


def test_live_capability_parser_resolves_original_reader_layers(monkeypatch):
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: SAMPLE_CAPABILITIES)
    harmony_preflight._cached_capabilities.cache_clear()

    payload = harmony_preflight.get_harmony_capabilities("GUNW", "PR")

    assert payload["collection"]["concept_id"] == "C1234567890-ASF"
    assert payload["capabilities"]["variable_subset"] is True
    assert payload["supports_geotiff_output"] is True
    assert payload["minimum_gunw_pilot_ready"] is True
    assert payload["scientific_layers"]["unwrapped_phase"].endswith("/unwrappedPhase")
    assert payload["scientific_layers"]["coherence"].endswith("/coherenceMagnitude")
    assert payload["scientific_layers"]["connected_components"].endswith("/connectedComponents")


def test_extraction_plan_is_single_granule_and_does_not_claim_analysis(monkeypatch):
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: SAMPLE_CAPABILITIES)
    harmony_preflight._cached_capabilities.cache_clear()

    response = client.post(
        "/api/harmony/plan",
        json={
            "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
            "product_type": "GUNW",
            "coordinates": [[85.45, 28.25], [85.55, 28.25], [85.55, 28.35], [85.45, 28.35]],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready_for_extraction"] is True
    assert body["one_granule_only"] is True
    assert body["data_downloaded"] is False
    assert body["scientific_analysis_performed"] is False
    assert body["spatial_strategy"] == "full-granule-layer-extraction"
    assert body["selected_layer_count"] == 5


def test_ambiguous_phase_layers_are_not_guessed(monkeypatch):
    ambiguous = dict(SAMPLE_CAPABILITIES)
    ambiguous["variables"] = list(SAMPLE_CAPABILITIES["variables"]) + [
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/VV/unwrappedPhase"}
    ]
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: ambiguous)
    harmony_preflight._cached_capabilities.cache_clear()

    payload = harmony_preflight.get_harmony_capabilities("GUNW", "PR")
    assert payload["scientific_layers"]["unwrapped_phase"] is None
    assert payload["minimum_gunw_pilot_ready"] is False


def test_bad_granule_is_rejected_before_harmony_lookup(monkeypatch):
    called = {"value": False}
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: called.__setitem__("value", True) or SAMPLE_CAPABILITIES)
    response = client.post(
        "/api/harmony/plan",
        json={"granule_name": "something-else.h5", "coordinates": [[85, 28], [85.1, 28], [85.1, 28.1]]},
    )
    assert response.status_code == 422
    assert called["value"] is False
