from fastapi.testclient import TestClient

import harmony_preflight
import main

client = TestClient(main.app)

SAMPLE_CAPABILITIES = {
    "conceptId": "C1234567890-ASF",
    "shortName": "NISAR_L2_GUNW_PROVISIONAL_V1",
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


def test_explicit_polarization_resolves_hh_and_does_not_mix_vv(monkeypatch):
    with_both_polarizations = dict(SAMPLE_CAPABILITIES)
    with_both_polarizations["variables"] = list(SAMPLE_CAPABILITIES["variables"]) + [
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/VV/unwrappedPhase"},
        {"name": "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/VV/coherenceMagnitude"},
    ]
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: with_both_polarizations)
    harmony_preflight._cached_capabilities.cache_clear()

    payload = harmony_preflight.get_harmony_capabilities("GUNW", "PR", polarization="HH")
    assert payload["scientific_layers"]["unwrapped_phase"].endswith("/HH/unwrappedPhase")
    assert payload["scientific_layers"]["coherence"].endswith("/HH/coherenceMagnitude")
    assert payload["polarization_selected"] == "HH"


def test_v3_nested_service_capabilities_are_parsed(monkeypatch):
    v3 = dict(SAMPLE_CAPABILITIES)
    v3["summary"] = {"subsetting": {"variable": False, "bbox": False, "shape": False}, "outputFormats": ["image/tiff"]}
    v3["services"] = [{
        "name": "net2cog",
        "referenceUrl": "https://cmr.earthdata.nasa.gov/search/concepts/S3177988677-POCLOUD",
        "capabilities": {"subsetting": {"variable": True, "bbox": False, "shape": False, "temporal": False},
                         "outputFormats": ["image/tiff"]},
    }]
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: v3)
    harmony_preflight._cached_capabilities.cache_clear()
    payload = harmony_preflight.get_harmony_capabilities("GUNW", "PR")
    assert payload["capabilities"]["variable_subset"] is True
    assert payload["capabilities"]["bbox_subset"] is False
    assert payload["supports_geotiff_output"] is True
    assert payload["minimum_gunw_pilot_ready"] is True


def test_bad_granule_is_rejected_before_harmony_lookup(monkeypatch):
    called = {"value": False}
    monkeypatch.setattr(harmony_preflight, "_http_json", lambda _url: called.__setitem__("value", True) or SAMPLE_CAPABILITIES)
    response = client.post(
        "/api/harmony/plan",
        json={"granule_name": "something-else.h5", "coordinates": [[85, 28], [85.1, 28], [85.1, 28.1]]},
    )
    assert response.status_code == 422
    assert called["value"] is False


def test_official_collection_short_name_does_not_reuse_granule_processing_type():
    assert harmony_preflight._collection_short_name("GUNW", "PR") == "NISAR_L2_GUNW_PROVISIONAL_V1"
    assert harmony_preflight._collection_short_name("GOFF", "UR") == "NISAR_L2_GOFF_PROVISIONAL_V1"
    assert harmony_preflight._collection_short_name("GUNW", "PR", "BETA") == "NISAR_L2_GUNW_BETA_V1"


def test_capability_request_uses_official_short_name_and_separates_maturity(monkeypatch):
    captured = {}
    def fake_http_json(url):
        captured["url"] = url
        return SAMPLE_CAPABILITIES
    monkeypatch.setattr(harmony_preflight, "_http_json", fake_http_json)
    harmony_preflight._cached_capabilities.cache_clear()
    payload = harmony_preflight.get_harmony_capabilities("GUNW", "PR", "PROVISIONAL")
    from urllib.parse import parse_qs, urlparse
    assert parse_qs(urlparse(captured["url"]).query)["shortName"] == ["NISAR_L2_GUNW_PROVISIONAL_V1"]
    assert payload["collection"]["maturity_requested"] == "PROVISIONAL"
    assert payload["collection"]["granule_processing_type"] == "PR"


def test_http_404_explains_collection_name_issue(monkeypatch):
    import io
    import urllib.error
    def fail_open(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://harmony.earthdata.nasa.gov/capabilities", code=404,
            msg="Not Found", hdrs=None, fp=io.BytesIO(b"not found")
        )
    monkeypatch.setattr(harmony_preflight.urllib.request, "urlopen", fail_open)
    try:
        harmony_preflight._http_json("https://harmony.earthdata.nasa.gov/capabilities?shortName=NISAR_L2_GUNW_PROVISIONAL_V1")
    except harmony_preflight.HarmonyLookupError as exc:
        assert "HTTP 404" in str(exc)
        assert "NISAR_L2_GUNW_PROVISIONAL_V1" in str(exc)
    else:
        raise AssertionError("Expected HarmonyLookupError")
