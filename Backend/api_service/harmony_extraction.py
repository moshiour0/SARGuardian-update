"""Guarded one-layer NISAR GUNW extraction job starter for NASA Harmony.

This module only submits a one-granule, one-variable, GeoTIFF job and checks
its job metadata. It never downloads result rasters. Because the current GUNW
net2cog capability has variable subset but not AOI subset, users must explicitly
confirm that the first pilot may process a full-granule layer. AOI cropping and
integration with the original HDF5-based reader are a subsequent step.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harmony_preflight import get_harmony_capabilities

HARMONY_ROOT = "https://harmony.earthdata.nasa.gov"
USER_AGENT = "SARGuardian/0.3 (single-layer GUNW extraction pilot)"
TIMEOUT_SECONDS = float(os.getenv("HARMONY_JOB_TIMEOUT_SECONDS", "45"))
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class HarmonyJobError(RuntimeError):
    """Raised for a safe-to-display Harmony job request/status error."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects when submitting jobs or polling JSON status."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class _SafeResultRedirect(urllib.request.HTTPRedirectHandler):
    """Follow only HTTPS redirects from Harmony result links to approved storage hosts.

    Harmony stages output in NASA-owned S3 and gives clients redirecting result URLs.
    This handler is used only for metadata probes (HEAD or GET Range: bytes=0-0),
    never for reading a raster body. It deliberately does not forward Authorization.
    """
    def __init__(self):
        super().__init__()
        self.followed: list[dict[str, Any]] = []
        self.rejected_reason: str | None = None

    @staticmethod
    def _approved(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443):
            return False
        return (
            host == "harmony.earthdata.nasa.gov"
            or host == "amazonaws.com"
            or host.endswith(".amazonaws.com")
        )

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not self._approved(newurl):
            self.rejected_reason = "Redirect target was outside the approved HTTPS Harmony/AWS S3 hosts; it was not contacted."
            return None
        parsed = urllib.parse.urlparse(newurl)
        self.followed.append({"status": int(code), "host": (parsed.hostname or "").lower()})
        # Keep only the Range header (for the one-byte metadata probe) and neutral
        # negotiation headers. Do not propagate cookies, bearer tokens, or auth.
        safe_headers = {}
        existing = {str(k).lower(): v for k, v in req.headers.items()}
        for name in ("User-Agent", "Accept", "Accept-Encoding", "Range"):
            if name.lower() in existing:
                safe_headers[name] = existing[name.lower()]
        return urllib.request.Request(newurl, headers=safe_headers, method=req.get_method())


def _token() -> str:
    token = os.getenv("EARTHDATA_TOKEN", "").strip()
    if not token:
        raise HarmonyJobError(
            "EARTHDATA_TOKEN is not configured. Add a valid NASA Earthdata bearer token "
            "as a Backend-only Secret in Render; do not put it in the Frontend or send it in chat."
        )
    return token


def _read_bounded_json(response_or_error: Any, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    headers = getattr(response_or_error, "headers", {})
    content_type = str(headers.get("Content-Type", "")).lower()
    if "image/tiff" in content_type or "application/octet-stream" in content_type:
        raise HarmonyJobError(
            "Harmony returned a binary result instead of job metadata. The API intentionally did not read or store the raster. "
            "Retry only after confirming forceAsync is supported for this request."
        )
    raw = response_or_error.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HarmonyJobError("Harmony job metadata response exceeded the safe response-size limit.")
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"message": raw.decode("utf-8", errors="replace")[:1000]}
    return value if isinstance(value, dict) else {"response": value}


def _job_id_from_payload(payload: dict[str, Any], location: str | None = None) -> str | None:
    if location:
        parsed = urllib.parse.urlparse(location)
        # Only accept a job URL on the official Harmony host.
        if parsed.netloc and parsed.netloc.lower() not in {"harmony.earthdata.nasa.gov"}:
            return None
        m = re.search(r"/jobs/([A-Za-z0-9_-]{1,128})(?:/|$|\?)", parsed.path + ("?" + parsed.query if parsed.query else ""))
        if m:
            return m.group(1)
    candidates = ("jobID", "jobId", "job_id", "id")
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, str) and JOB_ID_RE.fullmatch(value):
            return value
    for key in ("job", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _job_id_from_payload(nested)
            if found:
                return found
    links = payload.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            href = link.get("href") or link.get("url")
            rel = str(link.get("rel", "")).lower()
            if isinstance(href, str) and ("job" in rel or "/jobs/" in href):
                found = _job_id_from_payload({}, href)
                if found:
                    return found
    return None


def build_single_layer_request_url(*, collection_id: str, granule_name: str, variable: str) -> str:
    """Build a one-granule variable-only request; no spatial subset is claimed."""
    if not re.fullmatch(r"C[A-Za-z0-9_-]{5,80}", collection_id):
        raise HarmonyJobError("The capability response did not provide a valid collection concept ID.")
    if not re.fullmatch(r"NISAR_L2_(?:PR|UR)_GUNW_[A-Za-z0-9_]+", granule_name):
        raise HarmonyJobError("Use an exact NISAR L2 PR/UR GUNW granule name from the ASF catalogue.")
    if not variable.startswith("/science/LSAR/GUNW/") or "?" in variable or "#" in variable:
        raise HarmonyJobError("Only an exact GUNW science-layer path returned by Harmony capabilities may be requested.")
    path = f"{HARMONY_ROOT}/{urllib.parse.quote(collection_id, safe='-')}/ogc-api-coverages/1.0.0/collections/parameter_vars/coverage/rangeset"
    query = urllib.parse.urlencode([
        ("forceAsync", "true"),
        ("granuleName", granule_name),
        ("format", "image/tiff"),
        ("maxResults", "1"),
        ("skipPreview", "true"),
        ("ignoreErrors", "false"),
        ("label", "sarguardian-gunw-pilot"),
        ("variable", variable),
    ])
    return f"{path}?{query}"


def submit_single_layer_job(
    *, granule_name: str, maturity: str, polarization: str,
    layer_role: str, confirm_full_granule_layer: bool,
) -> dict[str, Any]:
    """Start exactly one GUNW phase-layer job. No TIFF is downloaded here."""
    if layer_role != "unwrapped_phase":
        raise HarmonyJobError("The first extraction pilot permits only unwrapped_phase. Additional layers will be added after this path is verified.")
    if not confirm_full_granule_layer:
        raise HarmonyJobError(
            "This GUNW collection does not advertise AOI subset support. Set confirm_full_granule_layer=true only after reviewing that the pilot will request one layer across the full granule."
        )
    if not re.fullmatch(r"NISAR_L2_(?:PR|UR)_GUNW_[A-Za-z0-9_]+", granule_name):
        raise HarmonyJobError("Use the exact full granule name from the ASF catalogue; wildcards are not allowed.")
    production_match = re.match(r"^NISAR_L2_(PR|UR)_GUNW_", granule_name)
    if not production_match:
        raise HarmonyJobError("Only production or urgent-response GUNW granules are currently allowed.")
    production = production_match.group(1)

    cap = get_harmony_capabilities("GUNW", production, maturity, polarization)
    variable = cap.get("scientific_layers", {}).get(layer_role)
    if not variable:
        raise HarmonyJobError(f"Harmony did not resolve a unique {layer_role} layer for polarization {polarization.upper()}.")
    if not cap.get("capabilities", {}).get("variable_subset"):
        raise HarmonyJobError("Harmony did not confirm variable subsetting for this collection; the extraction job was not started.")
    if not cap.get("supports_geotiff_output"):
        raise HarmonyJobError("Harmony did not advertise GeoTIFF output for this collection; the extraction job was not started.")
    collection_id = cap.get("collection", {}).get("concept_id")
    if not isinstance(collection_id, str):
        raise HarmonyJobError("The capability response did not include a collection concept ID.")

    token = _token()
    url = build_single_layer_request_url(collection_id=collection_id, granule_name=granule_name, variable=variable)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, application/geo+json;q=0.9, */*;q=0.1",
        "User-Agent": USER_AGENT,
    }, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    status = None
    location = None
    payload: dict[str, Any] = {}
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            location = response.headers.get("Location")
            payload = _read_bounded_json(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = exc.headers.get("Location") if exc.headers else None
        # Do not read arbitrary bodies for redirects or binary output. Only a
        # small JSON error is useful; never follow to signed result data.
        if status not in {301, 302, 303, 307, 308}:
            try:
                payload = _read_bounded_json(exc)
            except HarmonyJobError as parse_exc:
                raise HarmonyJobError(str(parse_exc)) from exc
        else:
            payload = {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HarmonyJobError("Could not submit the Harmony job because of a network/timeout error. No raster was downloaded by SARGuardian.") from exc

    job_id = _job_id_from_payload(payload, location)
    if job_id:
        return {
            "service": "NASA Harmony single-layer extraction",
            "mode": "job-submitted",
            "harmony_http_status": status,
            "job_id": job_id,
            "job_status_url": f"{HARMONY_ROOT}/jobs/{job_id}",
            "granule_name": granule_name,
            "layer_role": layer_role,
            "variable": variable,
            "polarization": polarization.upper(),
            "output_format": "image/tiff",
            "input_granules_limit": 1,
            "variables_requested": 1,
            "full_granule_layer": True,
            "aoi_subset_requested": False,
            "data_downloaded_by_sarguardian": False,
            "scientific_analysis_performed": False,
            "message": "Harmony job submitted. This service only queued one variable layer; SARGuardian has not downloaded or analyzed the result.",
        }

    if status in {301, 302, 303, 307, 308}:
        host = urllib.parse.urlparse(location or "").netloc
        if "urs.earthdata.nasa.gov" in host:
            raise HarmonyJobError("Harmony redirected to Earthdata Login. Check that EARTHDATA_TOKEN is valid and not expired; no data was downloaded.")
        raise HarmonyJobError(f"Harmony returned HTTP redirect {status} without a recognizable job URL; no raster was downloaded.")
    detail = payload.get("detail") or payload.get("message") or payload.get("title")
    raise HarmonyJobError(f"Harmony did not return a recognizable job ID (HTTP {status}). {str(detail)[:500] if detail else 'No raster was downloaded.'}")


def get_harmony_job_status(job_id: str) -> dict[str, Any]:
    """Fetch job status metadata only; never dereference result-file links."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HarmonyJobError("Invalid Harmony job ID.")
    token = _token()
    url = f"{HARMONY_ROOT}/jobs/{urllib.parse.quote(job_id, safe='-_')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            payload = _read_bounded_json(response)
            return {
                "service": "NASA Harmony job status",
                "job_id": job_id,
                "harmony_http_status": getattr(response, "status", 200),
                "job": payload,
                "result_files_downloaded": False,
                "scientific_analysis_performed": False,
            }
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            raise HarmonyJobError("Harmony redirected the job-status request. Check EARTHDATA_TOKEN validity; no result file was downloaded.") from exc
        raise HarmonyJobError(f"Harmony job-status request failed with HTTP {exc.code}; no result file was downloaded.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HarmonyJobError("Could not query Harmony job status because of a network/timeout error.") from exc



def _trusted_harmony_result_url(value: Any) -> str:
    """Validate a result URL before making a server-side HEAD/range request."""
    if not isinstance(value, str):
        raise HarmonyJobError("Harmony did not provide a result URL.")
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "harmony.earthdata.nasa.gov"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/service-results/")
        or parsed.query
        or parsed.fragment
    ):
        raise HarmonyJobError("Harmony returned a result URL outside the approved public result path; refusing to request it.")
    return value


def _parse_content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+|\*)", value.strip(), flags=re.IGNORECASE)
    if not match or match.group(1) == "*":
        return None
    try:
        number = int(match.group(1))
        return number if number >= 0 else None
    except ValueError:
        return None


def _inspect_result_headers(url: str) -> dict[str, Any]:
    """Inspect result headers, safely following Harmony's redirect to staged S3.

    First sends HEAD. If size is absent or HEAD is unsupported, sends GET with
    Range: bytes=0-0. No response body is read or saved; if a server ignores the
    Range header, the response is immediately closed after headers are received.
    """
    url = _trusted_harmony_result_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/tiff, application/json;q=0.8, */*;q=0.1",
        "Accept-Encoding": "identity",
    }
    redirect_handler = _SafeResultRedirect()
    opener = urllib.request.build_opener(redirect_handler)
    head_status: int | None = None
    head_headers: Any = {}
    head_error: str | None = None
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            head_status = getattr(response, "status", 200)
            head_headers = response.headers
    except urllib.error.HTTPError as exc:
        head_status = exc.code
        head_headers = exc.headers or {}
        if exc.code not in {405, 501}:
            location = head_headers.get("Location")
            if location and redirect_handler.rejected_reason:
                head_error = redirect_handler.rejected_reason
            elif exc.code in {301, 302, 303, 307, 308}:
                head_error = "Harmony result URL redirected, but its destination could not be safely followed."
            elif exc.code >= 400:
                head_error = f"HEAD request returned HTTP {exc.code}."
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HarmonyJobError("Could not inspect Harmony result headers because of a network/timeout error. No TIFF body was read.") from exc

    def _header_length(h: Any) -> int | None:
        raw = h.get("Content-Length") if h is not None else None
        if raw is None:
            return None
        try:
            num = int(raw)
            return num if num >= 0 else None
        except (TypeError, ValueError):
            return None

    # A refused redirect exposes the small text/plain redirect/error response's
    # Content-Length, not the TIFF object's size. Never treat it as result size.
    redirect_statuses = {301, 302, 303, 307, 308}
    refused_redirect = head_status in redirect_statuses and not redirect_handler.followed
    total = None if refused_redirect else _header_length(head_headers)
    range_total = None if refused_redirect else _parse_content_range_total(head_headers.get("Content-Range") if head_headers is not None else None)
    probe_status: int | None = None
    probe_headers: Any = {}
    # HEAD often redirects to a signed S3 URL or lacks Content-Length. Try a
    # minimal Range probe whenever safely possible and size remains unknown.
    should_probe = total is None and not refused_redirect and (head_status in {200, 206, 405, 501} or bool(redirect_handler.followed))
    if should_probe and not head_error:
        probe_redirect_handler = _SafeResultRedirect()
        probe_opener = urllib.request.build_opener(probe_redirect_handler)
        range_headers = dict(headers)
        range_headers["Range"] = "bytes=0-0"
        try:
            req = urllib.request.Request(url, headers=range_headers, method="GET")
            with probe_opener.open(req, timeout=TIMEOUT_SECONDS) as response:
                probe_status = getattr(response, "status", 200)
                probe_headers = response.headers
                range_total = _parse_content_range_total(probe_headers.get("Content-Range")) or range_total
                # For a 206 response Content-Length refers to the one-byte segment;
                # only treat it as the full object size for a 200 response.
                if total is None and probe_status == 200:
                    total = _header_length(probe_headers)
                redirect_handler.followed.extend(probe_redirect_handler.followed)
        except urllib.error.HTTPError as exc:
            probe_status = exc.code
            probe_headers = exc.headers or {}
            range_total = _parse_content_range_total(probe_headers.get("Content-Range")) or range_total
            if probe_redirect_handler.rejected_reason:
                head_error = probe_redirect_handler.rejected_reason
        except (urllib.error.URLError, TimeoutError, OSError):
            probe_status = None

    if total is None:
        total = range_total
    size_mb = round(total / (1024 * 1024), 3) if total is not None else None
    size_gib = round(total / (1024 * 1024 * 1024), 4) if total is not None else None
    final_headers = probe_headers if probe_status in {200, 206} and (_header_length(head_headers) is None or refused_redirect) else head_headers
    content_type = final_headers.get("Content-Type") if final_headers is not None else None
    note = "Metadata headers only; no raster body was read or stored."
    if total is None:
        note = head_error or "The redirect was followed when approved, but the storage response did not expose an object size via Content-Length or Content-Range. No raster body was read."
    return {
        "http_status": head_status,
        "range_probe_status": probe_status,
        "redirects_followed": redirect_handler.followed,
        "content_length_bytes": total,
        "content_length_mb": size_mb,
        "content_length_gib": size_gib,
        "content_range_total_bytes": range_total,
        "content_type": content_type,
        "accept_ranges": final_headers.get("Accept-Ranges") if final_headers is not None else None,
        "last_modified": final_headers.get("Last-Modified") if final_headers is not None else None,
        "size_known": total is not None,
        "is_geotiff_like": "tiff" in str(content_type or "").lower(),
        "note": note,
    }


def inspect_harmony_job_result(job_id: str) -> dict[str, Any]:
    """Return one completed Harmony result's size and metadata without downloading it."""
    status_payload = get_harmony_job_status(job_id)
    job = status_payload.get("job") if isinstance(status_payload.get("job"), dict) else {}
    job_status = str(job.get("status", "unknown")).lower()
    common = {
        "service": "SARGuardian Harmony result inspector",
        "job_id": job_id,
        "job_status": job_status,
        "result_file_downloaded": False,
        "scientific_analysis_performed": False,
    }
    if job_status not in {"successful", "completed", "complete"}:
        return {
            **common,
            "ready": False,
            "message": "Harmony result is not complete yet; check the job status later.",
            "progress": job.get("progress"),
            "job_message": job.get("message"),
        }

    links = job.get("links") if isinstance(job.get("links"), list) else []
    result_link = next(
        (
            link for link in links
            if isinstance(link, dict)
            and str(link.get("rel", "")).lower() == "data"
            and isinstance(link.get("href"), str)
        ),
        None,
    )
    if not result_link:
        return {
            **common,
            "ready": True,
            "size_known": False,
            "message": "Harmony job completed but no direct data result link was found in the job metadata.",
        }

    url = _trusted_harmony_result_url(result_link.get("href"))
    metadata = _inspect_result_headers(url)
    bbox = result_link.get("bbox") if isinstance(result_link.get("bbox"), list) else None
    temporal = result_link.get("temporal") if isinstance(result_link.get("temporal"), dict) else None
    filename = str(result_link.get("title") or urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]))
    return {
        **common,
        "ready": True,
        "result": {
            "filename": filename,
            "media_type": result_link.get("type"),
            "profile": "cloud-optimized GeoTIFF" if "cloud-optimized" in str(result_link.get("type", "")).lower() else None,
            "bbox_wgs84": bbox,
            "temporal": temporal,
            **metadata,
        },
        "aoi_cropped": False,
        "message": "Result metadata inspected without downloading the GeoTIFF. This output covers the full granule layer, not only the selected AOI.",
    }
