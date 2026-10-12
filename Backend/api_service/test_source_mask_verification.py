from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
from affine import Affine

import harmony_extraction as he


GRANULE = (
    "NISAR_L2_PR_GUNW_028_149_D_074_029_2000_SH_20260823T130631_20260823T130706_"
    "20260904T130631_20260904T130706_P05023_N_F_J_001"
)


def _make_source(path, values):
    y_len, x_len = values.shape
    x_axis = 500000.0 + 10.0 * np.arange(x_len, dtype=np.float64) + 5.0
    y_axis = 3300000.0 - 10.0 * np.arange(y_len, dtype=np.float64) - 5.0
    with h5py.File(path, "w") as h5:
        freq = h5.require_group("/science/LSAR/GUNW/grids/frequencyA")
        x_ds = freq.create_dataset("xCoordinates", data=x_axis)
        y_ds = freq.create_dataset("yCoordinates", data=y_axis)
        freq.create_dataset("projection", data=np.uint32(32644))
        unwrapped = freq.require_group("unwrappedInterferogram")
        mask_ds = unwrapped.create_dataset("mask", data=np.asarray(values, dtype=np.uint32))
        mask_ds.attrs["grid_mapping"] = "projection"
        mask_ds.dims[0].attach_scale(y_ds)
        mask_ds.dims[1].attach_scale(x_ds)
    return Affine(10, 0, 500000, 0, -10, 3300000)


def _fake_layer(values, transform):
    h, w = values.shape
    return {
        "values": np.asarray(values, dtype=np.float64),
        "inside": np.ones((h, w), dtype=bool),
        "valid_data": np.ones((h, w), dtype=bool),
        "dtype": "uint32",
        "grid": {
            "crs": "EPSG:32644",
            "window_transform": tuple(transform),
            "window_width": w,
            "window_height": h,
            "window_pixels": int(values.size),
        },
    }


@pytest.fixture
def patch_remote_source(monkeypatch, tmp_path):
    source_path = tmp_path / "synthetic-source.h5"

    class FakeHttpFileSystem:
        def open(self, _url, _mode, **_kwargs):
            return builtins.open(source_path, "rb")

    fake_earthaccess = SimpleNamespace(
        login=lambda strategy: SimpleNamespace(authenticated=(strategy == "environment")),
        get_fsspec_https_session=lambda: FakeHttpFileSystem(),
    )
    monkeypatch.setitem(sys.modules, "earthaccess", fake_earthaccess)
    monkeypatch.setattr(he, "_token", lambda: "test-token-not-real")
    # `_read_job_aoi_layer` receives a fixed signature in production. Wrap the
    # injected fake to read the layer/transform bound by each test.
    def run(values, transform, cog_values):
        _make_source(source_path, values)
        monkeypatch.setattr(
            he,
            "_read_job_aoi_layer",
            lambda *, job_id, granule_name, polarization, role, coordinates: _fake_layer(cog_values, transform),
        )
        return he.verify_gunw_source_mask(
            granule_name=GRANULE,
            mask_job_id="mask-job-1234",
            maturity="PROVISIONAL",
            polarization="HH",
            coordinates=[(82.5, 27.4), (82.6, 27.4), (82.6, 27.5)],
        )
    return run


def test_native_source_mask_exactly_matches_cog_window(patch_remote_source):
    source = np.array([[21, 21, 16777236], [21, 23, 24]], dtype=np.uint32)
    transform = _make_test_transform()
    result = patch_remote_source(source, transform, source.astype(np.float64))
    assert result["comparison_performed"] is True
    assert result["mask_values_match"] is True
    assert result["comparison"]["comparable_pixels"] == source.size
    assert result["comparison"]["exact_matches"] == source.size
    assert result["comparison"]["mismatches"] == 0
    assert result["full_source_hdf5_downloaded"] is False
    assert result["persistent_storage_used"] is False


def test_source_value_rounding_to_float32_is_reported_not_silently_accepted(patch_remote_source):
    source = np.array([[16777237, 21], [16777236, 24]], dtype=np.uint32)
    cog = np.array([[16777236, 21], [16777236, 24]], dtype=np.float64)
    result = patch_remote_source(source, _make_test_transform(), cog)
    assert result["comparison_performed"] is True
    assert result["mask_values_match"] is False
    assert result["comparison"]["mismatches"] == 1
    assert result["comparison"]["mismatches_compatible_with_float32_rounding"] == 1
    assert result["quality_gate_changed"] is False


def _make_test_transform():
    return Affine(10, 0, 500000, 0, -10, 3300000)
