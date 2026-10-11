# SARGuardian ASF + Harmony Preflight API

This API now provides two conservative preflight stages before any NISAR file is requested:

1. `POST /api/catalog/search` — searches the public ASF catalogue and reports matching granules and any sizes ASF exposes.
2. `GET /api/harmony/capabilities?product_type=GUNW&production=PR` — queries NASA Harmony's live collection-capabilities API and reports exact collection identifiers, service capabilities, available variables, and matches for the layer names expected by SARGuardian's original GUNW reader.
3. `POST /api/harmony/plan` — validates one exact GUNW granule and the AOI, then returns a single-granule extraction plan based on the live capabilities.

**No endpoint in this version downloads satellite data or performs scientific analysis.** That is intentional: the first step is to discover the authoritative collection and layer identifiers and check whether the Harmony service actually supports AOI subsetting. The next integration step must use those verified values to request only appropriate layers, crop/window GeoTIFFs, adapt their georeferencing, and then call the preserved original `Backend/src/gunw_reader.py`. Do not represent a plan response as an analysis result.

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

`GET /api/harmony/capabilities?product_type=GUNW&production=PR`

Also supported: `product_type=GOFF`, or `production=UR` if the corresponding collection exists and is configured. Collection short names can be overridden using `HARMONY_GUNW_PR_SHORT_NAME`, `HARMONY_GUNW_UR_SHORT_NAME`, `HARMONY_GOFF_PR_SHORT_NAME`, and `HARMONY_GOFF_UR_SHORT_NAME` environment variables. The endpoint uses the default short name pattern only to discover capabilities; the returned collection ID and layer names are what must be validated before extraction is implemented.

The no-download planner uses the exact name returned by the ASF catalogue:

`POST /api/harmony/plan`

```json
{
  "granule_name": "NISAR_L2_PR_GUNW_...",
  "product_type": "GUNW",
  "coordinates": [[85.45, 28.25], [85.55, 28.25], [85.55, 28.35], [85.45, 28.35]]
}
```

The above granule is a pattern illustration, not a valid concrete product. Use the exact complete granule name returned from a real ASF search. Coordinates are `[longitude, latitude]` pairs in WGS84. The polygon must contain at least three vertices and remain under the existing AOI cap.

## Why this is not extraction yet

The preserved scientific reader in `../src/gunw_reader.py` expects a structured HDF5 product. Harmony's NISAR dataset extraction service can return individual 2D layers as GeoTIFFs, so a correct adapter must preserve the geotransform/CRS and deliver the matching phase, coherence, connected-component, optional mask, and ionosphere layers to the existing scientific logic. We must also discover if spatial subsetting is supported by the live service before claiming that only the drawn AOI is transferred. This version deliberately stops at live capability discovery and an extraction plan.

## Free-hosting safety

The current API remains lightweight and uses no Earthdata secret, does not create Harmony jobs, and does not store raster files. Set `FRONTEND_ORIGINS` to the exact Vercel origin before public deployment. The built-in per-process rate limiter is best-effort only.

## Tests

```bash
pytest -q
```

Tests use mocked ASF/Harmony responses. They check parsing, safety limits, ambiguous variables, and truthful no-download semantics; they do not make live NASA requests.
