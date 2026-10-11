# SARGuardian ASF + Harmony Preflight API

This API now provides two conservative preflight stages before any NISAR file is requested:

1. `POST /api/catalog/search` — searches the public ASF catalogue and reports matching granules and any sizes ASF exposes.
2. `GET /api/harmony/capabilities?product_type=GUNW&production=PR` — queries NASA Harmony's live collection-capabilities API and reports exact collection identifiers, service capabilities, available variables, and matches for the layer names expected by SARGuardian's original GUNW reader.
3. `POST /api/harmony/plan` — validates one exact GUNW granule and AOI, then returns a single-granule layer plan based on live capabilities.
4. `POST /api/harmony/extract/start` — after an explicit confirmation, submits **one GUNW unwrapped-phase variable from one granule** to NASA Harmony as an asynchronous GeoTIFF job. It does not download the raster into SARGuardian.
5. `GET /api/harmony/jobs/{job_id}` — reads the asynchronous job status metadata only. It never follows result links or downloads raster files.

The current GUNW Harmony service advertises variable subsetting and GeoTIFF output, but does **not** advertise bounding-box or shape/AOI subsetting. Therefore the pilot job can create one full-granule layer output, not just the drawn 9.52 km² polygon. The job-start route requires `confirm_full_granule_layer=true` so this limitation is explicit. After a successful job, a later adapter must inspect result metadata, fetch/crop only a window from the Cloud-Optimized GeoTIFF if range access is supported, preserve georeferencing, and then feed compatible data into the preserved original `Backend/src/gunw_reader.py`. No displacement analysis is currently performed by the extraction endpoint.

## Run locally

```bash
python -m venv .venv
# Activate the virtual environment, then:
pip install -r requirements.txt
pip install pytest httpx
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000/docs` for the API.

## Harmony capabilities endpoint

`GET /api/harmony/capabilities?product_type=GUNW&production=PR&maturity=PROVISIONAL`

`production` describes the granule processing type (PR=Production, UR=Urgent Response, OD=Science On-Demand); it is **not** the CMR collection short-name family. `maturity` selects the official collection family `PROVISIONAL` or `BETA`. Official default short names are `NISAR_L2_GUNW_PROVISIONAL_V1`, `NISAR_L2_GUNW_BETA_V1`, `NISAR_L2_GOFF_PROVISIONAL_V1`, and `NISAR_L2_GOFF_BETA_V1`. Use `maturity=BETA` for a Beta collection. These names are based on the ASF NISAR data guide. Collection short names can be explicitly overridden using `HARMONY_GUNW_PROVISIONAL_SHORT_NAME`, `HARMONY_GUNW_BETA_SHORT_NAME`, `HARMONY_GOFF_PROVISIONAL_SHORT_NAME`, or `HARMONY_GOFF_BETA_SHORT_NAME`.

The no-download planner uses the exact name returned by the ASF catalogue:

`POST /api/harmony/plan`

```json
{
  "granule_name": "NISAR_L2_PR_GUNW_...",
  "product_type": "GUNW",
  "maturity": "PROVISIONAL",
  "coordinates": [[85.45, 28.25], [85.55, 28.25], [85.55, 28.35], [85.45, 28.35]]
}
```

The above granule is a pattern illustration, not a valid concrete product. Use the exact complete granule name returned from a real ASF search. Coordinates are `[longitude, latitude]` pairs in WGS84. The polygon must contain at least three vertices and remain under the existing AOI cap.

## Guarded extraction pilot

The capabilities parser understands Harmony capability response version 3 nested subsetting fields. For a GUNW collection, request the explicit polarization you plan to analyze:

`GET /api/harmony/capabilities?product_type=GUNW&production=PR&maturity=PROVISIONAL&polarization=HH`

For the current collection, expect the variable-only `net2cog` capability and GeoTIFF output to be reported separately from spatial subsetting. The available variable list commonly contains both HH and VV versions; the API resolves one explicit polarization instead of mixing them.

After setting `EARTHDATA_TOKEN` on the Backend deployment, a controlled pilot can be started through `POST /api/harmony/extract/start`:

```json
{
  "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
  "product_type": "GUNW",
  "maturity": "PROVISIONAL",
  "polarization": "HH",
  "layer_role": "unwrapped_phase",
  "confirm_full_granule_layer": true
}
```

`coordinates` is optional for this extraction request because the current net2cog service does not spatially crop to the AOI. Add the actual AOI coordinates if you want them recorded in the response. The required `confirm_full_granule_layer: true` explicitly acknowledges that the output is a full-granule layer, not a 9.52 km² AOI subset. This pilot only submits the job; it does not download output or perform displacement analysis. Poll `GET /api/harmony/jobs/{job_id}` for metadata. Never share the bearer token.

## Why this is not extraction yet

The preserved scientific reader in `../src/gunw_reader.py` expects a structured HDF5 product. Harmony's NISAR dataset extraction service can return individual 2D layers as GeoTIFFs, so a correct adapter must preserve the geotransform/CRS and deliver the matching phase, coherence, connected-component, optional mask, and ionosphere layers to the existing scientific logic. We must also discover if spatial subsetting is supported by the live service before claiming that only the drawn AOI is transferred. This version deliberately stops at live capability discovery and an extraction plan.

## Starting the one-layer pilot

Before calling `/api/harmony/extract/start`, add `EARTHDATA_TOKEN` as a **Secret in the Backend's Render environment only**. Use an Earthdata Login bearer token that is currently valid. Never put it in a `VITE_` variable, Frontend configuration, GitHub, or a public issue, and never send it in chat.

The request body must include the exact granule name returned by ASF, AOI coordinates as `[longitude, latitude]`, `product_type: "GUNW"`, `maturity: "PROVISIONAL"` (for the August 2026 products in the current test), `polarization: "HH"`, `layer_role: "unwrapped_phase"`, and `confirm_full_granule_layer: true`. Use `true` only after accepting that AOI subsetting is not available in the detected Harmony service. The first pilot requests exactly one variable and uses `maxResults=1`, `forceAsync=true`, and `format=image/tiff`.

Once a job ID is returned, poll `GET /api/harmony/jobs/{job_id}`. The endpoint returns status metadata only; do not assume the raster has been downloaded or analyzed. Expired Earthdata tokens must be replaced in Render. Any future download/crop route must enforce response-size limits and preserve the original scientific reader's expected georeferencing.

## Free-hosting safety

The metadata preflight is lightweight. The job-start route requires an Earthdata token and an explicit full-granule layer confirmation. It does not store raster files or execute the scientific engine. Set `FRONTEND_ORIGINS` to the exact Vercel origin before public deployment. The built-in per-process rate limiter is best-effort only.

## Tests

```bash
pytest -q
```

Tests use mocked ASF/Harmony responses. They check parsing, safety limits, ambiguous variables, and truthful no-download semantics; they do not make live NASA requests.
