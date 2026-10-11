"""Lightweight SARGuardian metadata preflight API.

This service searches ASF's public catalogue only. It does not download NISAR
products, store granules, or claim to perform scientific change analysis.
It is intentionally small enough for a low-resource free web service.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from typing import Any

from harmony_preflight import get_harmony_capabilities, build_extraction_plan, HarmonyLookupError
from harmony_extraction import submit_single_layer_job, get_harmony_job_status, HarmonyJobError

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ASF_API = "https://api.daac.asf.alaska.edu/services/search/param"
EARTH_RADIUS_KM = 6371.0088
MAX_DAYS = int(os.getenv("MAX_TIME_WINDOW_DAYS", "90"))
MAX_AOI_AREA_KM2 = float(os.getenv("MAX_AOI_AREA_KM2", "250"))
MAX_VERTICES = int(os.getenv("MAX_VERTICES", os.getenv("MAX_AOI_VERTICES", "25")))
MAX_GRANULES = int(os.getenv("MAX_GRANULES_PER_SEARCH", "50"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "6"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("ASF_HTTP_TIMEOUT_SECONDS", "35"))

app = FastAPI(
    title="SARGuardian ASF + Harmony Pilot API",
    version="0.3.0",
    description=(
        "Searches ASF metadata, discovers NASA Harmony capabilities, and can submit an explicitly "
        "confirmed one-granule/one-variable Harmony extraction job. SARGuardian does not download "
        "the output raster or run scientific analysis in this stage."
    ),
)

_origins_raw = os.getenv("FRONTEND_ORIGINS", "*").strip()
_allowed_origins = [item.strip() for item in _origins_raw.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Best-effort per-process rate limiter. Use a platform-level limiter for a
# multi-instance deployment; this in-memory limiter is not a distributed quota.
_rate_lock = threading.Lock()
_request_times: dict[str, deque[float]] = defaultdict(deque)


class CatalogSearchRequest(BaseModel):
    coordinates: list[tuple[float, float]] = Field(
        min_length=3,
        description="Area-of-interest coordinates as [longitude, latitude] pairs.",
    )
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    product_type: str = Field(description="Exactly one product family: GUNW or GOFF.")



class HarmonyPlanRequest(BaseModel):
    granule_name: str = Field(min_length=20, max_length=300)
    coordinates: list[tuple[float, float]] = Field(
        min_length=3,
        description="Area-of-interest coordinates as [longitude, latitude] pairs.",
    )
    product_type: str = Field(default="GUNW", description="Pilot currently supports GUNW only.")
    maturity: str = Field(default="PROVISIONAL", description="CMR collection maturity: PROVISIONAL or BETA.")
    polarization: str = Field(default="HH", description="GUNW polarization to resolve consistently: HH or VV.")


class HarmonyExtractStartRequest(BaseModel):
    granule_name: str = Field(min_length=20, max_length=300)
    coordinates: list[tuple[float, float]] | None = Field(
        default=None,
        description="Optional AOI coordinates for provenance/limits. Current GUNW net2cog service does not apply this AOI on the server.",
    )
    product_type: str = Field(default="GUNW")
    maturity: str = Field(default="PROVISIONAL")
    polarization: str = Field(default="HH")
    layer_role: str = Field(default="unwrapped_phase")
    confirm_full_granule_layer: bool = Field(
        default=False,
        description="Must be true to explicitly authorize one full-granule single-layer output, because AOI subsetting is not advertised.",
    )


def _close_ring(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ring = list(coords)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _wkt_polygon(coords: list[tuple[float, float]]) -> str:
    ring = _close_ring(coords)
    joined = ",".join(f"{lon:.7f} {lat:.7f}" for lon, lat in ring)
    return f"POLYGON(({joined}))"


def _polygon_area_km2(coords: list[tuple[float, float]]) -> float:
    """Approximate spherical polygon area, suitable for a safety cap."""
    ring = _close_ring(coords)
    if len(ring) < 4:
        return 0.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(ring[:-1], ring[1:]):
        dlon = math.radians(lon2 - lon1)
        if dlon > math.pi:
            dlon -= 2 * math.pi
        elif dlon < -math.pi:
            dlon += 2 * math.pi
        total += dlon * (
            math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total) * EARTH_RADIUS_KM**2 / 2


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    with _rate_lock:
        times = _request_times[key]
        while times and now - times[0] > RATE_LIMIT_WINDOW_SECONDS:
            times.popleft()
        if len(times) >= RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many archive searches. Limit: {RATE_LIMIT_REQUESTS} "
                    f"requests per {RATE_LIMIT_WINDOW_SECONDS} seconds."
                ),
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )
        times.append(now)


def _parse_mb(value: Any) -> float | None:
    try:
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    except (TypeError, ValueError):
        pass
    return None


def _product_dates(granule_name: str) -> list[str]:
    stamps = re.findall(r"(\d{8})T\d{6}", granule_name)
    if len(stamps) >= 4:
        selected = [stamps[0], stamps[2]]
    else:
        selected = stamps[:2]
    return [datetime.strptime(item, "%Y%m%d").date().isoformat() for item in selected]


def _query_asf(params: dict[str, str]) -> list[dict[str, Any]]:
    url = f"{ASF_API}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SARGuardian/0.1 (metadata preflight; NASA Space Apps project)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="The ASF catalogue could not be reached. Please retry shortly.",
        ) from exc

    if isinstance(payload, dict):
        rows = payload.get("results", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _validate_search(search: CatalogSearchRequest) -> tuple[list[tuple[float, float]], date, date, float]:
    if search.product_type not in {"GUNW", "GOFF"}:
        raise HTTPException(status_code=422, detail="Choose one product type: GUNW or GOFF.")

    coords = list(search.coordinates)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 3 or len(coords) > MAX_VERTICES:
        raise HTTPException(
            status_code=422,
            detail=f"Draw an area with 3–{MAX_VERTICES} vertices.",
        )
    if any(
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or lon < -180
        or lon > 180
        or lat < -90
        or lat > 90
        for lon, lat in coords
    ):
        raise HTTPException(status_code=422, detail="Every point must be valid WGS84 longitude/latitude.")

    try:
        start = date.fromisoformat(search.start_date)
        end = date.fromisoformat(search.end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Use valid YYYY-MM-DD dates.") from exc
    today = datetime.now(timezone.utc).date()
    if end < start:
        raise HTTPException(status_code=422, detail="End date must not precede start date.")
    if end > today:
        raise HTTPException(status_code=422, detail="End date cannot be in the future (UTC).")
    if (end - start).days > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Time window cannot exceed {MAX_DAYS} days.")

    area = _polygon_area_km2(coords)
    if area <= 0.001:
        raise HTTPException(status_code=422, detail="Selected area is too small or invalid.")
    if area > MAX_AOI_AREA_KM2:
        raise HTTPException(
            status_code=422,
            detail=f"Selected area is about {area:.1f} km²; the free demo limit is {MAX_AOI_AREA_KM2:.0f} km².",
        )
    return coords, start, end, area


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "SARGuardian ASF + Harmony preflight",
        "mode": "metadata-only",
        "data_downloaded": False,
        "scientific_analysis_performed": False,
        "harmony_capability_discovery": True,
        "single_granule_extraction": "guarded-one-layer-job-submission-available",
        "earthdata_token_configured": bool(os.getenv("EARTHDATA_TOKEN", "").strip()),
        "original_scientific_engine_present": True,
        "max_time_window_days": MAX_DAYS,
        "max_aoi_area_km2": MAX_AOI_AREA_KM2,
        "max_granules_per_search": MAX_GRANULES,
    }


@app.post("/api/catalog/search")
def search_catalog(search: CatalogSearchRequest, request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    # Honor the first forwarded IP when behind a trusted hosting proxy. On a
    # production multi-proxy setup, configure the platform's rate limiting too.
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    limiter_key = forwarded or client_ip
    _check_rate_limit(limiter_key)

    coords, start, end, area_km2 = _validate_search(search)
    params = {
        "platform": "NISAR",
        "processingLevel": search.product_type,
        "start": f"{start.isoformat()}T00:00:00Z",
        "end": f"{end.isoformat()}T23:59:59Z",
        "intersectsWith": _wkt_polygon(coords),
        "output": "jsonlite",
        "maxResults": str(MAX_GRANULES + 1),
    }
    rows = _query_asf(params)
    rows.sort(key=lambda row: str(row.get("startTime") or row.get("granuleName") or ""))
    truncated = len(rows) > MAX_GRANULES
    rows = rows[:MAX_GRANULES]

    products = []
    known_total_mb = 0.0
    unknown_sizes = 0
    for row in rows:
        granule = str(row.get("granuleName") or row.get("fileName") or "Unknown granule")
        size_mb = _parse_mb(row.get("sizeMB", row.get("sizeMb", row.get("size_mb"))))
        if size_mb is None:
            unknown_sizes += 1
        else:
            known_total_mb += size_mb
        product_type = str(row.get("productType") or search.product_type).upper()
        products.append(
            {
                "granule_name": granule,
                "product_type": product_type,
                "observation_dates": _product_dates(granule),
                "start_time": row.get("startTime"),
                "stop_time": row.get("stopTime"),
                "direction": row.get("flightDirection"),
                "path": row.get("path"),
                "size_mb": round(size_mb, 2) if size_mb is not None else None,
                "processing_date": row.get("processingDate"),
                "processing_level": row.get("processingLevel"),
            }
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "mode": "catalog-preflight-only",
        "checked_at": now,
        "data_downloaded": False,
        "scientific_analysis_performed": False,
        "query": {
            "platform": "NISAR",
            "product_type": search.product_type,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "aoi_area_km2": round(area_km2, 3),
        },
        "product_count": len(products),
        "truncated": truncated,
        "products_with_unknown_size": unknown_sizes,
        "estimated_full_granule_volume_gib": round(known_total_mb / 1024, 3),
        "estimated_full_granule_volume_is_complete": unknown_sizes == 0,
        "products": products,
        "next_step": (
            "No products were downloaded. Review the catalogue list and volume first. "
            "The next pipeline stage should request only the required GUNW/GOFF layer(s) "
            "through NASA Harmony, process one granule at a time, and retain only small AOI results."
        ),
        "warnings": [
            "The volume is the sum of ASF's reported full-granule sizes, not an estimate of a Harmony subset.",
            "This response only identifies available catalogue items; it does not detect land movement.",
            "NISAR Level-2 products may be PROVISIONAL or have other maturity labels; verify product metadata before combining results.",
        ],
    }


@app.get("/api/harmony/capabilities")
def harmony_capabilities(
    product_type: str = "GUNW",
    production: str = "PR",
    maturity: str = "PROVISIONAL",
    polarization: str = "HH",
) -> dict[str, Any]:
    """Inspect Harmony support for an official NISAR collection short name.

    ``production`` is the granule processing type (PR/UR/OD); ``maturity`` is
    the CMR collection family (PROVISIONAL/BETA). These must not be conflated.
    This endpoint is metadata-only and never downloads science data.
    """
    if product_type not in {"GUNW", "GOFF"}:
        raise HTTPException(status_code=422, detail="product_type must be GUNW or GOFF.")
    if production not in {"PR", "UR", "OD"}:
        raise HTTPException(status_code=422, detail="production must be PR, UR, or OD.")
    if maturity.upper() not in {"PROVISIONAL", "BETA"}:
        raise HTTPException(status_code=422, detail="maturity must be PROVISIONAL or BETA.")
    if polarization.upper() not in {"HH", "VV"}:
        raise HTTPException(status_code=422, detail="polarization must be HH or VV.")
    try:
        return get_harmony_capabilities(product_type, production, maturity.upper(), polarization.upper())
    except HarmonyLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/harmony/plan")
def harmony_extraction_plan(plan: HarmonyPlanRequest) -> dict[str, Any]:
    """Build a single-granule, layer-only plan; this route does not download data."""
    if plan.product_type != "GUNW":
        raise HTTPException(
            status_code=422,
            detail="The first scientific integration pilot is GUNW only; GOFF will be added after this path is validated.",
        )
    name = plan.granule_name.strip()
    match = re.match(r"^NISAR_L2_(PR|UR)_GUNW_", name)
    if not match:
        raise HTTPException(
            status_code=422,
            detail="Provide the exact granule name of a NISAR L2 PR/UR GUNW product from the ASF results.",
        )
    today_utc = datetime.now(timezone.utc).date().isoformat()
    coords, _unused_start, _unused_end, area_km2 = _validate_search(
        CatalogSearchRequest(
            coordinates=plan.coordinates,
            start_date=today_utc,
            end_date=today_utc,
            product_type="GUNW",
        )
    )
    if plan.maturity.upper() not in {"PROVISIONAL", "BETA"}:
        raise HTTPException(status_code=422, detail="maturity must be PROVISIONAL or BETA.")
    try:
        result = build_extraction_plan(
            granule_name=name,
            product_type="GUNW",
            production=match.group(1),
            aoi_area_km2=area_km2,
            coordinates=coords,
            maturity=plan.maturity.upper(),
            polarization=plan.polarization.upper(),
        )
    except HarmonyLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@app.post("/api/harmony/extract/start")
def harmony_start_single_layer_extraction(request: HarmonyExtractStartRequest) -> dict[str, Any]:
    """Start a guarded one-granule GUNW phase-only Harmony job.

    This does not download a raster or run SARGuardian's scientific engine.
    Because the current GUNW service advertises no AOI subset support, callers
    must explicitly confirm that one full-granule layer may be produced.
    """
    if request.product_type != "GUNW":
        raise HTTPException(status_code=422, detail="The first extraction pilot supports GUNW only.")
    if request.polarization.upper() not in {"HH", "VV"}:
        raise HTTPException(status_code=422, detail="polarization must be HH or VV.")
    if request.maturity.upper() not in {"PROVISIONAL", "BETA"}:
        raise HTTPException(status_code=422, detail="maturity must be PROVISIONAL or BETA.")
    aoi_area_km2 = None
    if request.coordinates is not None:
        today_utc = datetime.now(timezone.utc).date().isoformat()
        # Validate AOI using the same 250 km² / vertex / coordinate safety checks as catalogue search.
        _coords, _unused_start, _unused_end, aoi_area_km2 = _validate_search(
            CatalogSearchRequest(
                coordinates=request.coordinates,
                start_date=today_utc,
                end_date=today_utc,
                product_type="GUNW",
            )
        )
    try:
        result = submit_single_layer_job(
            granule_name=request.granule_name.strip(),
            maturity=request.maturity.upper(),
            polarization=request.polarization.upper(),
            layer_role=request.layer_role,
            confirm_full_granule_layer=request.confirm_full_granule_layer,
        )
        result["aoi_coordinates_received"] = len(request.coordinates) if request.coordinates else 0
        result["aoi_area_km2"] = round(aoi_area_km2, 3) if aoi_area_km2 is not None else None
        result["note"] = "The AOI is recorded only for later client-side/server-side cropping; current Harmony extraction is for the full granule layer."
        return result
    except HarmonyLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HarmonyJobError as exc:
        message = str(exc)
        status_code = 503 if "EARTHDATA_TOKEN is not configured" in message else 409 if "confirm_full_granule_layer=true" in message else 502
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.get("/api/harmony/jobs/{job_id}")
def harmony_job_status(job_id: str) -> dict[str, Any]:
    """Read Harmony job status metadata only. This route never downloads outputs."""
    try:
        return get_harmony_job_status(job_id)
    except HarmonyJobError as exc:
        message = str(exc)
        status_code = 503 if "EARTHDATA_TOKEN is not configured" in message else 502
        raise HTTPException(status_code=status_code, detail=message) from exc
