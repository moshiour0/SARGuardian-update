from urllib.parse import parse_qs, urlparse
import urllib.error

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


def test_inspect_harmony_result_is_metadata_only_for_completed_job(monkeypatch):
    job_id = "ea93e3fa-cffb-422a-91d6-953d6f3498cf"
    result_url = (
        "https://harmony.earthdata.nasa.gov/service-results/harmony-prod-staging/public/"
        + job_id + "/401715863/test_reformatted.tif"
    )
    monkeypatch.setattr(harmony_extraction, "get_harmony_job_status", lambda _: {
        "job": {
            "status": "successful",
            "links": [{"rel": "data", "href": result_url, "title": "test.tif", "type": "image/tiff; profile=cloud-optimized", "bbox": [82.5, 27.4, 85.7, 30.1]}],
        }
    })
    monkeypatch.setattr(harmony_extraction, "_inspect_result_headers", lambda _: {
        "http_status": 200,
        "content_length_bytes": 123456,
        "content_length_mb": 0.118,
        "size_known": True,
        "note": "Headers only; no raster read.",
    })
    body = harmony_extraction.inspect_harmony_job_result(job_id)
    assert body["ready"] is True
    assert body["result_file_downloaded"] is False
    assert body["scientific_analysis_performed"] is False
    assert body["result"]["content_length_bytes"] == 123456
    assert body["aoi_cropped"] is False


def test_inspect_harmony_result_not_ready_does_not_probe_result(monkeypatch):
    monkeypatch.setattr(harmony_extraction, "get_harmony_job_status", lambda _: {
        "job": {"status": "running", "progress": 65}
    })
    monkeypatch.setattr(harmony_extraction, "_inspect_result_headers", lambda _: pytest.fail("Should not inspect pending job"))
    body = harmony_extraction.inspect_harmony_job_result("ea93e3fa-cffb-422a-91d6-953d6f3498cf")
    assert body["ready"] is False
    assert body["progress"] == 65


def test_inspect_result_rejects_untrusted_url():
    with pytest.raises(HarmonyJobError, match="outside the approved public result path"):
        harmony_extraction._trusted_harmony_result_url("https://example.com/a.tif")


def test_safe_result_redirect_allows_https_aws_and_preserves_range_without_auth():
    handler = harmony_extraction._SafeResultRedirect()
    req = harmony_extraction.urllib.request.Request(
        "https://harmony.earthdata.nasa.gov/service-results/harmony-prod-staging/public/job/file.tif",
        headers={"Range": "bytes=0-0", "Authorization": "Bearer do-not-forward"},
        method="GET",
    )
    redirected = handler.redirect_request(
        req, None, 303, "See Other", {},
        "https://harmony-prod-staging.s3.us-west-2.amazonaws.com/public/job/file.tif?X-Amz-Signature=fake",
    )
    assert redirected is not None
    assert redirected.full_url.startswith("https://harmony-prod-staging.s3.us-west-2.amazonaws.com/")
    assert any(k.lower() == "range" and v == "bytes=0-0" for k, v in redirected.headers.items())
    assert not any(k.lower() == "authorization" for k in redirected.headers)
    assert handler.followed == [{"status": 303, "host": "harmony-prod-staging.s3.us-west-2.amazonaws.com"}]


def test_safe_result_redirect_rejects_unapproved_host_and_http():
    handler = harmony_extraction._SafeResultRedirect()
    req = harmony_extraction.urllib.request.Request(
        "https://harmony.earthdata.nasa.gov/service-results/harmony-prod-staging/public/job/file.tif",
        method="HEAD",
    )
    assert handler.redirect_request(req, None, 303, "See Other", {}, "https://example.com/file.tif") is None
    assert handler.rejected_reason

    handler2 = harmony_extraction._SafeResultRedirect()
    assert handler2.redirect_request(req, None, 303, "See Other", {}, "http://bucket.s3.amazonaws.com/file.tif") is None
    assert handler2.rejected_reason


def test_inspect_result_authenticates_only_initial_harmony_request(monkeypatch):
    job_id = "ea93e3fa-cffb-422a-91d6-953d6f3498cf"
    result_url = (
        "https://harmony.earthdata.nasa.gov/service-results/harmony-prod-staging/public/"
        + job_id + "/test_reformatted.tif"
    )
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token-never-forward")
    captured = {}

    class FakeOpener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["authorization"] = req.get_header("Authorization")
            raise urllib.error.HTTPError(
                req.full_url, 303, "See Other",
                {"Location": "https://urs.earthdata.nasa.gov/oauth/authorize?secret=not-logged"},
                None,
            )

    monkeypatch.setattr(harmony_extraction.urllib.request, "build_opener", lambda *args: FakeOpener())
    result = harmony_extraction._inspect_result_headers(result_url)
    assert captured["url"] == result_url
    assert captured["authorization"] == "Bearer test-token-never-forward"
    assert result["size_known"] is False
    assert result["redirect_rejected_host"] is None  # Fake opener bypasses the redirect handler itself.


def test_rejected_redirect_exposes_host_only_not_path_or_query():
    handler = harmony_extraction._SafeResultRedirect()
    req = harmony_extraction.urllib.request.Request(
        "https://harmony.earthdata.nasa.gov/service-results/harmony-prod-staging/public/job/file.tif",
        headers={"Authorization": "Bearer do-not-forward"}, method="HEAD",
    )
    target = "https://urs.earthdata.nasa.gov/oauth/authorize?client_secret=never-report"
    assert handler.redirect_request(req, None, 303, "See Other", {}, target) is None
    assert handler.rejected_host == "urs.earthdata.nasa.gov"
    assert "never-report" not in handler.rejected_reason
