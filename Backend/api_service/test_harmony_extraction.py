from urllib.parse import parse_qs, urlparse

import pytest

import harmony_extraction
from harmony_extraction import HarmonyJobError, build_single_layer_request_url, submit_single_layer_job


PHASE = "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/unwrappedPhase"
GRANULE = "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001"


def test_builder_requests_only_one_variable_and_one_granule():
    url = build_single_layer_request_url(
        collection_id="C2854335566-ASF", granule_name=GRANULE, variable=PHASE
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "harmony.earthdata.nasa.gov"
    assert parsed.path.endswith("/collections/parameter_vars/coverage/rangeset")
    assert query["granuleName"] == [GRANULE]
    assert query["variable"] == [PHASE]
    assert query["format"] == ["image/tiff"]
    assert query["maxResults"] == ["1"]
    assert query["forceAsync"] == ["true"]
    assert "subset" not in query


def test_builder_rejects_wildcard_granule():
    with pytest.raises(HarmonyJobError):
        build_single_layer_request_url(
            collection_id="C2854335566-ASF", granule_name=GRANULE + "*", variable=PHASE
        )


def test_job_submit_requires_explicit_full_granule_confirmation(monkeypatch):
    monkeypatch.setattr(harmony_extraction, "get_harmony_capabilities", lambda *a: pytest.fail("Must fail before lookup"))
    with pytest.raises(HarmonyJobError, match="confirm_full_granule_layer=true"):
        submit_single_layer_job(
            granule_name=GRANULE, maturity="PROVISIONAL", polarization="HH",
            layer_role="unwrapped_phase", confirm_full_granule_layer=False,
        )


def test_job_submit_requires_backend_earthdata_token(monkeypatch):
    monkeypatch.setattr(harmony_extraction, "get_harmony_capabilities", lambda *a: {
        "scientific_layers": {"unwrapped_phase": PHASE},
        "capabilities": {"variable_subset": True, "bbox_subset": False, "shape_subset": False},
        "supports_geotiff_output": True,
        "collection": {"concept_id": "C2854335566-ASF"},
    })
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    with pytest.raises(HarmonyJobError, match="EARTHDATA_TOKEN is not configured"):
        submit_single_layer_job(
            granule_name=GRANULE, maturity="PROVISIONAL", polarization="HH",
            layer_role="unwrapped_phase", confirm_full_granule_layer=True,
        )
