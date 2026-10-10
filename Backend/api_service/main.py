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
    title="SARGuardian Catalog Preflight API",
    version="0.1.0",
    description=(
        "Searches NISAR product metadata at ASF before any large data transfer. "
        "This API does not download or process satellite data."
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
        "service": "SARGuardian catalog preflight",
        "mode": "metadata-only",
        "data_downloaded": False,
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
