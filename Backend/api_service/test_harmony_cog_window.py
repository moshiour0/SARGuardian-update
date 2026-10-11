from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

import harmony_extraction


def _write_test_raster(path):
    data = np.arange(100, dtype=np.float32).reshape(10, 10)
    profile = {
        "driver": "GTiff",
        "height": 10,
        "width": 10,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_origin(0.0, 1.0, 0.1, 0.1),
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def test_inspect_cog_aoi_reads_only_polygon_window(tmp_path):
    raster_path = tmp_path / "phase.tif"
    _write_test_raster(raster_path)
    # Six-by-six pixel polygon inside the 10x10 test raster.
    coords = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
    result = harmony_extraction.inspect_cog_aoi(str(raster_path), coords)

    assert result["source_driver"] == "GTiff"
    assert result["aoi_window"]["width"] == 6
    assert result["aoi_window"]["height"] == 7  # geometry window includes the boundary row
    assert result["pixels_inside_polygon"] == 36
    assert result["valid_unmasked_pixels"] == 36
    assert result["raw_phase_diagnostic_statistics"]["min"] == 22.0
    assert result["raw_phase_diagnostic_statistics"]["max"] == 77.0
    assert result["aoi_window_read_from_remote_cog"] is False
    assert result["full_raster_downloaded"] is False
    assert result["persistent_storage_used"] is False
    assert result["scientific_displacement_analysis_performed"] is False


def test_inspect_cog_aoi_rejects_too_large_window(tmp_path):
    raster_path = tmp_path / "phase.tif"
    _write_test_raster(raster_path)
    coords = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    try:
        harmony_extraction.inspect_cog_aoi(str(raster_path), coords, max_window_pixels=50)
    except harmony_extraction.HarmonyJobError as exc:
        assert "above the safe cap" in str(exc)
    else:
        raise AssertionError("Expected safe pixel cap to reject the 100-pixel window")


def test_resolved_result_url_is_never_returned_in_public_payload(monkeypatch):
    # Test the in-memory result handoff; signed URL must not appear in the response.
    signed_url = "https://dqzb5lcolc8fj.cloudfront.net/test.tif?signature=SECRET_VALUE"
    monkeypatch.setattr(
        harmony_extraction,
        "_resolve_result_url_for_window_read",
        lambda job_id: (signed_url, {"title": "phase.tif"}),
    )
    monkeypatch.setattr(
        harmony_extraction,
        "inspect_cog_aoi",
        lambda source, coordinates: {
            "aoi_window": {"width": 1, "height": 1, "window_pixels": 1},
            "aoi_window_read_from_remote_cog": source.startswith("/vsicurl/"),
            "full_raster_downloaded": False,
            "persistent_storage_used": False,
            "scientific_displacement_analysis_performed": False,
        },
    )
    result = harmony_extraction.crop_harmony_job_result_aoi(
        "ea93e3fa-cffb-422a-91d6-953d6f3498cf",
        [(82.6, 27.5), (82.7, 27.5), (82.7, 27.6)],
    )
    assert result["aoi_cropped"] is True
    assert result["result_filename"] == "phase.tif"
    assert "SECRET_VALUE" not in str(result)
    assert signed_url not in str(result)
