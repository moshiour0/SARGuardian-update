# SARGuardian Catalog Preflight API

This small API is the first stage of an on-demand NISAR workflow. It searches the
public ASF metadata catalogue for **one** product family (`GUNW` or `GOFF`) in
a drawn area and a maximum 90-day date window. It returns product names,
acquisition dates when parsed from the name, direction/path metadata, declared
full-granule file sizes, and a total-size estimate.

**It does not download data, run raster processing, or report detected movement.**
This boundary is deliberate: users should see how many products match and the
raw volume estimate before any costly processing can start.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Health check: `GET http://localhost:8000/health`
Catalog request: `POST http://localhost:8000/api/catalog/search`

Example JSON:

```json
{
  "coordinates": [[85.47, 28.25], [85.55, 28.25], [85.55, 28.32], [85.47, 28.32]],
  "start_date": "2026-07-01",
  "end_date": "2026-09-01",
  "product_type": "GUNW"
}
```

Coordinates are `[longitude, latitude]` in WGS84. The backend closes the polygon
if needed. The public free-demo limits default to a maximum 90-day date range,
250 km² AOI, 25 vertices, 50 returned granules, and 6 catalogue requests per
minute per observed client IP. Configure `FRONTEND_ORIGINS` to the exact deployed
frontend origin before publishing. The included in-memory limiter is only a
best-effort single-process safeguard; use platform-level rate limiting before
opening the API to a broad audience.

## Deploy on Render (metadata only)

Use `api_service` as the root directory, or use the included `render.yaml`.
Set `FRONTEND_ORIGINS` to the frontend origin, e.g. `https://your-project.vercel.app`.
Render's free service can spin down while idle and has an ephemeral filesystem;
that is acceptable for this metadata-only API because it does not store product
files. Expect a cold start after idle periods. Do not move HDF5/raster processing
into this free web service.

## API response semantics

- `estimated_full_granule_volume_gib` sums `sizeMB` from ASF, converted to GiB.
- This is the sum of entire source granules; it is **not** a prediction of the
  smaller extracted variables that a future Harmony-based processing stage may use.
- `data_downloaded` and `scientific_analysis_performed` remain `false`.
- If results are truncated or any sizes are missing, the response states so.

## Tests

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

Tests stub ASF responses and do not download real satellite data.
