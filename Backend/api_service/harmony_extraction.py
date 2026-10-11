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

from harmony_preflight import get_harmony_capabilities, HarmonyLookupError

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
        # Safe diagnostics only: expose the host, never the target path or signed query.
        self.rejected_host: str | None = None

    @staticmethod
    def _approved(url: str, source_url: str | None = None) -> bool:
        """Validate an HTTPS redirect target against a narrow host allowlist.

        Harmony result links can redirect from the trusted /service-results/ path
        to a CloudFront distribution used for staged output. Permit only a normal
        13-character CloudFront distribution hostname, and only when the redirect
        originates directly from the trusted Harmony result URL. Do not permit a
        CloudFront URL to redirect onward to another host.
        """
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None:
            return False
        try:
            if parsed.port not in (None, 443):
                return False
        except ValueError:
            return False

        if (
            host == "harmony.earthdata.nasa.gov"
            or host == "amazonaws.com"
            or host.endswith(".amazonaws.com")
        ):
            return True

        # Harmony has been observed returning result links through a dedicated
        # CloudFront distribution (e.g. dqzb5lcolc8fj.cloudfront.net). A strict
        # distribution-ID pattern is safer than accepting every *.cloudfront.net
        # redirect from arbitrary sources.
        source = urllib.parse.urlparse(source_url or "")
        source_host = (source.hostname or "").lower().rstrip(".")
        source_path = source.path or ""
        is_harmony_result_source = (
            source.scheme == "https"
            and source_host == "harmony.earthdata.nasa.gov"
            and source_path.startswith("/service-results/")
            and source.username is None
            and source.password is None
            and not source.fragment
        )
        return bool(
            is_harmony_result_source
            and re.fullmatch(r"[a-z0-9]{13}\.cloudfront\.net", host)
        )

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not self._approved(newurl, source_url=req.full_url):
            parsed_target = urllib.parse.urlparse(newurl)
            self.rejected_host = (parsed_target.hostname or "unknown").lower().rstrip(".")
            self.rejected_reason = (
                "Redirect target host was outside the approved HTTPS Harmony/AWS S3/validated CloudFront hosts; "
                "it was not contacted. Only the hostname is reported for diagnosis."
            )
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


def build_single_layer_request_url(*, collection_id: str, granule_name: str, variable: str, layer_label: str = "sarguardian-gunw-pilot") -> str:
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
        ("label", layer_label),
        ("variable", variable),
    ])
    return f"{path}?{query}"


def submit_single_layer_job(
    *, granule_name: str, maturity: str, polarization: str,
    layer_role: str, confirm_full_granule_layer: bool,
) -> dict[str, Any]:
    """Start one GUNW variable-layer job. No TIFF is downloaded here."""
    supported_roles = {
        "unwrapped_phase",
        "coherence",
        "connected_components",
        "ionosphere_phase_screen",
        "ionosphere_phase_screen_uncertainty",
        "mask",
    }
    if layer_role not in supported_roles:
        raise HarmonyJobError(
            "Unsupported GUNW layer role. Choose unwrapped_phase, coherence, connected_components, "
            "ionosphere_phase_screen, ionosphere_phase_screen_uncertainty, or mask."
        )
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
    url = build_single_layer_request_url(
        collection_id=collection_id, granule_name=granule_name, variable=variable,
        layer_label=f"sarguardian-gunw-{layer_role.replace('_', '-')}",
    )
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
            "message": "Harmony job submitted. This service only queued one variable layer; SARGuardian has not downloaded or scientifically analyzed the result.",
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
        # The initial /service-results/ endpoint can require Earthdata auth.
        # _SafeResultRedirect intentionally strips this header on every cross-host
        # redirect, so the bearer token is never forwarded to S3 or another host.
        "Authorization": f"Bearer {_token()}",
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
    probe_redirect_handler: _SafeResultRedirect | None = None
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
        rejected_host = redirect_handler.rejected_host or (probe_redirect_handler.rejected_host if probe_redirect_handler else None)
        if rejected_host:
            note = f"Redirect target host '{rejected_host}' was not contacted because it is not on the approved HTTPS Harmony/AWS S3 allowlist. The request included Earthdata authorization only to the original Harmony host; authorization is stripped on redirects. No raster body was read."
    return {
        "http_status": head_status,
        "range_probe_status": probe_status,
        "redirects_followed": redirect_handler.followed,
        "redirect_rejected_host": (
            redirect_handler.rejected_host
            or (probe_redirect_handler.rejected_host if probe_redirect_handler else None)
        ),
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


def _resolve_result_url_for_window_read(
    job_id: str, *, expected_granule_name: str | None = None, expected_variable: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Resolve a completed Harmony result to its temporary storage URL.

    The signed URL is kept only in memory and must never be returned or logged.
    A HEAD request follows the already-validated Harmony -> S3/CloudFront path;
    no raster body is read here.
    """
    status_payload = get_harmony_job_status(job_id)
    job = status_payload.get("job") if isinstance(status_payload.get("job"), dict) else {}
    state = str(job.get("status", "unknown")).lower()
    if state not in {"successful", "completed", "complete"}:
        raise HarmonyJobError(f"Harmony job is not complete (status={state}); no raster pixels were read.")

    if expected_granule_name or expected_variable:
        request_url = str(job.get("request") or "")
        request_parts = urllib.parse.urlparse(request_url)
        request_params = urllib.parse.parse_qs(request_parts.query)
        request_granule = (request_params.get("granuleName") or [None])[0]
        request_variable = (request_params.get("variable") or [None])[0]
        if expected_granule_name and request_granule != expected_granule_name:
            raise HarmonyJobError("Harmony job granule does not match the requested analysis granule.")
        if expected_variable and request_variable != expected_variable:
            raise HarmonyJobError("Harmony job variable does not match the expected GUNW layer; refusing to combine layers.")

    links = job.get("links") if isinstance(job.get("links"), list) else []
    link = next((item for item in links if isinstance(item, dict) and str(item.get("rel", "")).lower() == "data" and isinstance(item.get("href"), str)), None)
    if not link:
        raise HarmonyJobError("Completed Harmony job has no result data link.")
    source_url = _trusted_harmony_result_url(link.get("href"))

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "image/tiff, application/json;q=0.8, */*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": USER_AGENT,
    }
    opener = urllib.request.build_opener(_SafeResultRedirect())
    request = urllib.request.Request(source_url, headers=headers, method="HEAD")
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            status = int(getattr(response, "status", 200))
            content_type = str(response.headers.get("Content-Type", "")).lower()
            accept_ranges = str(response.headers.get("Accept-Ranges", "")).lower()
            if status < 200 or status >= 300:
                raise HarmonyJobError(f"Result storage HEAD request returned HTTP {status}; no raster body was read.")
            if "tiff" not in content_type and "geotiff" not in content_type:
                raise HarmonyJobError("Resolved result is not served as a TIFF/GeoTIFF; no raster pixels were read.")
            if "bytes" not in accept_ranges:
                raise HarmonyJobError("Result storage does not advertise byte-range access. Windowed COG reading was stopped to avoid a full-file transfer.")
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308} and getattr(opener, "handlers", None):
            raise HarmonyJobError("Result redirect could not be resolved to an approved storage host; no raster pixels were read.") from exc
        raise HarmonyJobError(f"Result storage could not be resolved (HTTP {exc.code}); no raster pixels were read.") from exc
    except HarmonyJobError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HarmonyJobError("Could not resolve the temporary result URL due to a network/timeout issue; no raster pixels were read.") from exc

    # Must end at an approved storage host after _SafeResultRedirect validated
    # every hop. Do not permit GDAL to follow a new, unvalidated redirect from
    # the original Harmony endpoint. The final URL may contain a signed query;
    # keep it only in memory and never include it in logs or the response.
    parsed = urllib.parse.urlparse(final_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    valid_cloudfront = bool(re.fullmatch(r"[a-z0-9]{13}\.cloudfront\.net", host))
    valid_s3 = host == "s3.amazonaws.com" or host.endswith(".s3.amazonaws.com") or bool(re.fullmatch(r"s3[.-][a-z0-9-]+\.amazonaws\.com", host)) or bool(re.fullmatch(r"[a-z0-9.-]+\.s3[.-][a-z0-9-]+\.amazonaws\.com", host))
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.fragment or not (valid_cloudfront or valid_s3):
        raise HarmonyJobError("Resolved result is not on an approved HTTPS CloudFront/S3 host; windowed read refused.")
    return final_url, link


def inspect_cog_aoi(source: str, coordinates: list[tuple[float, float]], max_window_pixels: int = 1_000_000) -> dict[str, Any]:
    """Read only the COG window intersecting a WGS84 polygon; never persist raster data.

    `source` may be a local GeoTIFF path for tests, or a GDAL /vsicurl/ URL in
    production. The returned values are descriptive statistics for raw phase,
    not displacement or landslide conclusions.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.errors import WindowError
        from rasterio.features import geometry_mask, geometry_window
        from rasterio.warp import transform_geom, transform_bounds
    except ImportError as exc:
        raise HarmonyJobError("AOI window inspection requires rasterio and numpy; install Backend API requirements and redeploy.") from exc

    ring = list(coordinates)
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(ring[0])
    polygon = {"type": "Polygon", "coordinates": [[list(point) for point in ring]]}

    try:
        with rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_USE_HEAD="YES",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
            GDAL_HTTP_MAX_RETRY="2",
            GDAL_HTTP_TIMEOUT="25",
            CPL_CURL_VERBOSE="NO",
        ):
            # A production source begins with /vsicurl/ and reaches the final
            # approved CloudFront/S3 URL, allowing GDAL to use HTTP Range reads.
            with rasterio.open(source, sharing=False) as dataset:
                if dataset.driver != "GTiff" or dataset.count < 1:
                    raise HarmonyJobError("Result is not a readable single/multiband GeoTIFF.")
                if dataset.crs is None:
                    raise HarmonyJobError("GeoTIFF has no CRS; refusing to apply a geographic AOI to an unknown grid.")
                native_polygon = transform_geom("EPSG:4326", dataset.crs, polygon, precision=8)
                try:
                    window = geometry_window(dataset, [native_polygon], pad_x=0, pad_y=0)
                except WindowError as exc:
                    raise HarmonyJobError("The selected AOI does not intersect the result raster extent.") from exc
                width, height = int(window.width), int(window.height)
                if width <= 0 or height <= 0:
                    raise HarmonyJobError("The AOI intersects no raster pixels.")
                window_pixels = width * height
                if window_pixels > max_window_pixels:
                    raise HarmonyJobError(
                        f"AOI window is {window_pixels:,} pixels, above the safe cap of {max_window_pixels:,}. "
                        "Use a smaller AOI before reading raster pixels."
                    )
                data = dataset.read(1, window=window, masked=True)
                inside = geometry_mask(
                    [native_polygon],
                    out_shape=(height, width),
                    transform=dataset.window_transform(window),
                    invert=True,
                    all_touched=False,
                )
                values = np.asarray(data.data, dtype=np.float64)
                data_mask = np.ma.getmaskarray(data)
                valid = inside & ~data_mask & np.isfinite(values)
                count = int(valid.sum())
                raw_stats = None
                if count:
                    sample = values[valid]
                    raw_stats = {
                        "min": float(np.min(sample)),
                        "max": float(np.max(sample)),
                        "mean": float(np.mean(sample)),
                        "std": float(np.std(sample)),
                        "unit": "raw unwrapped-phase value; treated as radians by the product variable name, not converted to displacement",
                    }
                bounds = transform_bounds(dataset.crs, "EPSG:4326", *dataset.window_bounds(window), densify_pts=21)
                return {
                    "source_driver": dataset.driver,
                    "source_crs": str(dataset.crs),
                    "source_width": int(dataset.width),
                    "source_height": int(dataset.height),
                    "source_nodata": float(dataset.nodata) if dataset.nodata is not None else None,
                    "aoi_window": {"width": width, "height": height, "window_pixels": window_pixels},
                    "aoi_window_bounds_wgs84": [float(v) for v in bounds],
                    "pixels_inside_polygon": int(inside.sum()),
                    "valid_unmasked_pixels": count,
                    "valid_fraction_of_polygon_pixels": round(count / max(1, int(inside.sum())), 6),
                    "raw_phase_diagnostic_statistics": raw_stats,
                    "aoi_window_read_from_remote_cog": source.startswith("/vsicurl/"),
                    "full_raster_downloaded": False,
                    "temporary_raster_file_created": False,
                    "persistent_storage_used": False,
                    "scientific_displacement_analysis_performed": False,
                    "warning": (
                        "This is an AOI-clipped raw phase diagnostic only. It is not LOS displacement or landslide detection. "
                        "The current one-layer output lacks the paired coherence, connected-components, ionosphere and source-product metadata required by the original GUNW scientific reader."
                    ),
                }
    except HarmonyJobError:
        raise
    except Exception as exc:
        # Do not return raw GDAL errors because they can contain a signed URL.
        raise HarmonyJobError(
            f"Could not read the requested AOI window from the GeoTIFF ({type(exc).__name__}). "
            "No full raster was intentionally downloaded or persisted. Check result URL expiry and remote byte-range support."
        ) from exc


def crop_harmony_job_result_aoi(job_id: str, coordinates: list[tuple[float, float]]) -> dict[str, Any]:
    """Read a bounded AOI window from a successful remote COG without saving it."""
    final_url, link = _resolve_result_url_for_window_read(job_id)
    # GDAL receives only the resolved temporary result URL. Do not return or log it.
    remote_source = "/vsicurl/" + final_url
    crop = inspect_cog_aoi(remote_source, coordinates)
    return {
        "service": "SARGuardian remote COG AOI window inspector",
        "job_id": job_id,
        "job_status": "successful",
        "result_filename": str(link.get("title", "")),
        "aoi_cropped": True,
        "crop_representation": "in-memory pixel window and polygon mask; no output TIFF persisted",
        "result_file_downloaded": False,
        "raster_window_pixels_read": True,
        "scientific_analysis_performed": False,
        **crop,
    }

# ---------------------------------------------------------------------------
# Multi-layer AOI diagnostics, designed to match the core GUNW reader gates.
# No output raster is persisted and no displacement is reported without a
# source-product wavelength plus a valid stable reference area.
# ---------------------------------------------------------------------------
GUNW_LAYER_ROLES = (
    "unwrapped_phase",
    "coherence",
    "connected_components",
    "ionosphere_phase_screen",
    "mask",
    "ionosphere_phase_screen_uncertainty",
)


def _expected_gunw_variable(role: str, polarization: str) -> str:
    pol = polarization.upper()
    if pol not in {"HH", "VV"}:
        raise HarmonyJobError("Polarization must be HH or VV.")
    base = "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram"
    suffixes = {
        "unwrapped_phase": f"{pol}/unwrappedPhase",
        "coherence": f"{pol}/coherenceMagnitude",
        "connected_components": f"{pol}/connectedComponents",
        "ionosphere_phase_screen": f"{pol}/ionospherePhaseScreen",
        "ionosphere_phase_screen_uncertainty": f"{pol}/ionospherePhaseScreenUncertainty",
        "mask": "mask",
    }
    if role not in suffixes:
        raise HarmonyJobError(f"Unsupported GUNW layer role: {role}.")
    return f"{base}/{suffixes[role]}"


def start_gunw_quality_layer_jobs(
    *, granule_name: str, phase_job_id: str, maturity: str, polarization: str,
    confirm_full_granule_layers: bool, include_iono_uncertainty: bool = True,
) -> dict[str, Any]:
    """Reuse a completed phase job and queue required companion quality layers.

    This queues separate one-variable jobs because the current net2cog service
    advertises one-variable output, not multiple-variable output. All outputs
    stay on Harmony staging; SARGuardian does not download raster files here.
    """
    if not confirm_full_granule_layers:
        raise HarmonyJobError(
            "The current net2cog service cannot crop the AOI server-side. Set "
            "confirm_full_granule_layers=true only after accepting that each queued layer covers the full granule."
        )
    match = re.match(r"^NISAR_L2_(PR|UR)_GUNW_", granule_name)
    if not match:
        raise HarmonyJobError("Provide an exact NISAR L2 PR/UR GUNW granule name.")
    pol = polarization.upper()
    if pol not in {"HH", "VV"}:
        raise HarmonyJobError("Polarization must be HH or VV.")

    # Validate that the supplied phase job belongs to this exact granule and
    # expected polarization/layer, rather than mixing unrelated job IDs.
    phase_status = get_harmony_job_status(phase_job_id)
    phase_job = phase_status.get("job") if isinstance(phase_status.get("job"), dict) else {}
    phase_state = str(phase_job.get("status", "unknown")).lower()
    if phase_state not in {"successful", "completed", "complete"}:
        raise HarmonyJobError(f"Phase job must be successful before companion layers can be queued (status={phase_state}).")
    request_url = str(phase_job.get("request") or "")
    parsed_request = urllib.parse.urlparse(request_url)
    request_qs = urllib.parse.parse_qs(parsed_request.query)
    actual_granule = (request_qs.get("granuleName") or [None])[0]
    actual_variable = (request_qs.get("variable") or [None])[0]
    expected_phase_variable = _expected_gunw_variable("unwrapped_phase", pol)
    if actual_granule != granule_name or actual_variable != expected_phase_variable:
        raise HarmonyJobError(
            "The supplied phase job does not match the requested granule and polarization. "
            "Use the successful unwrapped-phase job for the exact granule/polarization."
        )

    roles = ["coherence", "connected_components", "ionosphere_phase_screen", "mask"]
    if include_iono_uncertainty:
        roles.append("ionosphere_phase_screen_uncertainty")
    jobs: dict[str, Any] = {
        "unwrapped_phase": {
            "job_id": phase_job_id,
            "reused_existing_job": True,
            "status": "successful",
            "variable": expected_phase_variable,
        }
    }
    errors: dict[str, str] = {}
    for role in roles:
        try:
            result = submit_single_layer_job(
                granule_name=granule_name,
                maturity=maturity.upper(),
                polarization=pol,
                layer_role=role,
                confirm_full_granule_layer=True,
            )
            jobs[role] = {
                "job_id": result["job_id"],
                "reused_existing_job": False,
                "status": "submitted",
                "variable": result.get("variable"),
                "job_status_url": result.get("job_status_url"),
            }
        except (HarmonyJobError, HarmonyLookupError) as exc:
            # Avoid raising after some jobs have already been accepted; return
            # partial submission IDs so the user can continue without silently
            # queuing duplicates. Error strings are sanitized by the helpers.
            errors[role] = str(exc)
    return {
        "service": "SARGuardian GUNW companion-layer jobs",
        "mode": "multi-layer-jobs-submitted",
        "granule_name": granule_name,
        "polarization": pol,
        "maturity": maturity.upper(),
        "jobs": jobs,
        "submission_errors": errors,
        "all_required_layers_submitted": all(
            role in jobs for role in ("unwrapped_phase", "coherence", "connected_components", "ionosphere_phase_screen", "mask")
        ),
        "aoi_subset_requested": False,
        "result_rasters_downloaded_by_sarguardian": False,
        "scientific_analysis_performed": False,
        "next_step": "Wait for all required jobs to succeed, then call the AOI quality-analysis endpoint with these job IDs and the same polygon.",
        "warning": "Companion layers are full-granule GeoTIFF outputs staged by Harmony. SARGuardian reads only the requested AOI window remotely and does not persist the full TIFFs.",
    }


def _read_remote_cog_aoi_array(
    source: str,
    coordinates: list[tuple[float, float]],
    max_window_pixels: int = 1_000_000,
) -> dict[str, Any]:
    """Read one bounded COG window and its polygon mask into memory only."""
    try:
        import numpy as np
        import rasterio
        from rasterio.errors import WindowError
        from rasterio.features import geometry_mask, geometry_window
        from rasterio.warp import transform_geom, transform_bounds
    except ImportError as exc:
        raise HarmonyJobError("AOI analysis requires rasterio and numpy; install Backend API requirements and redeploy.") from exc

    ring = list(coordinates)
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(ring[0])
    polygon = {"type": "Polygon", "coordinates": [[list(point) for point in ring]]}
    try:
        with rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_USE_HEAD="YES",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
            GDAL_HTTP_MAX_RETRY="2",
            GDAL_HTTP_TIMEOUT="25",
            CPL_CURL_VERBOSE="NO",
        ):
            with rasterio.open(source, sharing=False) as dataset:
                if dataset.driver != "GTiff" or dataset.count != 1:
                    raise HarmonyJobError("Each GUNW layer must resolve to a single-band GeoTIFF.")
                if dataset.crs is None:
                    raise HarmonyJobError("A layer GeoTIFF has no CRS; refusing to combine grids without georeferencing.")
                native_polygon = transform_geom("EPSG:4326", dataset.crs, polygon, precision=8)
                try:
                    window = geometry_window(dataset, [native_polygon], pad_x=0, pad_y=0)
                except WindowError as exc:
                    raise HarmonyJobError("The selected AOI does not intersect one of the required layer rasters.") from exc
                width, height = int(window.width), int(window.height)
                if width <= 0 or height <= 0 or width * height > max_window_pixels:
                    raise HarmonyJobError("AOI raster window is empty or exceeds the safe pixel limit.")
                data = dataset.read(1, window=window, masked=True)
                values = np.asarray(data.data, dtype=np.float64)
                valid_data = ~np.ma.getmaskarray(data) & np.isfinite(values)
                inside = geometry_mask(
                    [native_polygon],
                    out_shape=(height, width),
                    transform=dataset.window_transform(window),
                    invert=True,
                    all_touched=False,
                )
                bounds = transform_bounds(dataset.crs, "EPSG:4326", *dataset.window_bounds(window), densify_pts=21)
                transform = tuple(float(x) for x in dataset.transform)
                window_transform = tuple(float(x) for x in dataset.window_transform(window))
                return {
                    "values": values,
                    "valid_data": valid_data,
                    "inside": inside,
                    "grid": {
                        "crs": str(dataset.crs),
                        "width": int(dataset.width),
                        "height": int(dataset.height),
                        "transform": transform,
                        "window_transform": window_transform,
                        "window_width": width,
                        "window_height": height,
                        "window_pixels": width * height,
                        "window_bounds_wgs84": [float(v) for v in bounds],
                    },
                    "nodata": float(dataset.nodata) if dataset.nodata is not None else None,
                }
    except HarmonyJobError:
        raise
    except Exception as exc:
        raise HarmonyJobError(
            f"Could not read a required layer from the remote COG ({type(exc).__name__}); no full raster was intentionally downloaded or saved."
        ) from exc


def _read_job_aoi_layer(
    *, job_id: str, granule_name: str, polarization: str, role: str,
    coordinates: list[tuple[float, float]],
) -> dict[str, Any]:
    expected_variable = _expected_gunw_variable(role, polarization)
    final_url, link = _resolve_result_url_for_window_read(
        job_id,
        expected_granule_name=granule_name,
        expected_variable=expected_variable,
    )
    remote_source = "/vsicurl/" + final_url
    layer = _read_remote_cog_aoi_array(remote_source, coordinates)
    layer["result_filename"] = str(link.get("title") or "")
    layer["job_id"] = job_id
    layer["role"] = role
    layer["expected_variable"] = expected_variable
    return layer


def _array_stats(values: Any, valid: Any) -> dict[str, Any] | None:
    import numpy as np
    sample = np.asarray(values, dtype=np.float64)[np.asarray(valid, dtype=bool)]
    if not sample.size:
        return None
    return {
        "min": float(np.min(sample)),
        "max": float(np.max(sample)),
        "mean": float(np.mean(sample)),
        "std": float(np.std(sample)),
        "count": int(sample.size),
    }


def analyze_gunw_aoi_layers(
    *, granule_name: str, polarization: str, coordinates: list[tuple[float, float]],
    job_ids: dict[str, str], coherence_threshold: float = 0.3,
) -> dict[str, Any]:
    """Read required GUNW COG layer windows and apply reader-compatible gates.

    It mirrors the core gates in src/gunw_reader.py but does not invoke its
    HDF5-only read_gunw() function: these are separate GeoTIFF layers. It does
    not produce referenced LOS displacement, landslide detection, or a risk
    score. Arrays are held in memory for the request and then discarded.
    """
    import numpy as np
    if not 0.0 <= coherence_threshold <= 1.0:
        raise HarmonyJobError("coherence_threshold must be between 0 and 1.")
    required_roles = ("unwrapped_phase", "coherence", "connected_components", "ionosphere_phase_screen", "mask")
    missing = [role for role in required_roles if not job_ids.get(role)]
    if missing:
        raise HarmonyJobError("Missing completed Harmony job IDs for required layers: " + ", ".join(missing))

    layers: dict[str, dict[str, Any]] = {}
    for role in (*required_roles, "ionosphere_phase_screen_uncertainty"):
        job_id = job_ids.get(role)
        if not job_id:
            continue
        layers[role] = _read_job_aoi_layer(
            job_id=job_id,
            granule_name=granule_name,
            polarization=polarization,
            role=role,
            coordinates=coordinates,
        )

    reference = layers["unwrapped_phase"]["grid"]
    for role, layer in layers.items():
        grid = layer["grid"]
        if grid["crs"] != reference["crs"] or grid["width"] != reference["width"] or grid["height"] != reference["height"]:
            raise HarmonyJobError(f"Layer grid mismatch for {role}; refusing to combine rasters from different grids.")
        if len(grid["transform"]) != len(reference["transform"]) or any(
            abs(a - b) > 1e-8 for a, b in zip(grid["transform"], reference["transform"])
        ):
            raise HarmonyJobError(f"Layer geotransform mismatch for {role}; scientific layer alignment is not safe.")
        if grid["window_width"] != reference["window_width"] or grid["window_height"] != reference["window_height"]:
            raise HarmonyJobError(f"AOI window shape mismatch for {role}; refusing to combine unaligned arrays.")
        if not np.array_equal(layer["inside"], layers["unwrapped_phase"]["inside"]):
            raise HarmonyJobError(f"Polygon rasterization mismatch for {role}; refusing to combine unaligned arrays.")

    phase_layer = layers["unwrapped_phase"]
    coh_layer = layers["coherence"]
    comp_layer = layers["connected_components"]
    iono_layer = layers["ionosphere_phase_screen"]
    mask_layer = layers["mask"]
    inside = phase_layer["inside"]
    phase = phase_layer["values"]
    coherence = coh_layer["values"]
    components = comp_layer["values"]
    iono = iono_layer["values"]
    mask_values = mask_layer["values"]

    # Match the core reader's gates: raw zero is treated as no phase; coherence
    # must meet threshold; connected component IDs must be positive; and the
    # GUNW mask is decoded as water/subswath bits, not treated as a boolean mask.
    phase_valid = inside & phase_layer["valid_data"] & np.isfinite(phase) & (phase != 0)
    coherence_valid = inside & coh_layer["valid_data"] & np.isfinite(coherence)
    coherence_pass = coherence_valid & (coherence >= coherence_threshold)
    components_valid = inside & comp_layer["valid_data"] & np.isfinite(components)
    components_pass = components_valid & (components > 0)
    iono_valid = inside & iono_layer["valid_data"] & np.isfinite(iono)
    mask_valid = inside & mask_layer["valid_data"] & np.isfinite(mask_values)
    mask_int = np.where(mask_valid, mask_values, 255).astype(np.int64)
    water = mask_int // 100
    ref_subswath = (mask_int // 10) % 10
    sec_subswath = mask_int % 10
    mask_pass = mask_valid & (water == 0) & (ref_subswath > 0) & (sec_subswath > 0) & (mask_int != 255)

    jointly_valid = phase_valid & coherence_pass & components_pass & iono_valid & mask_pass
    corrected_phase = phase - np.where(iono_valid, iono, 0.0)
    corrected_phase_stats = _array_stats(corrected_phase, jointly_valid)

    comp_sample = components[phase_valid & coherence_pass & components_pass]
    dominant_component = None
    dominant_component_share = None
    component_count = 0
    if comp_sample.size:
        component_values, component_counts = np.unique(comp_sample.astype(np.int64), return_counts=True)
        best = int(np.argmax(component_counts))
        dominant_component = int(component_values[best])
        dominant_component_share = round(float(component_counts[best] / component_counts.sum()), 6)
        component_count = int(component_values.size)

    layer_stats: dict[str, Any] = {}
    for role, key, valid_mask in (
        ("unwrapped_phase", "raw_phase", phase_valid),
        ("coherence", "coherence", coherence_valid),
        ("connected_components", "connected_components_values", components_valid),
        ("ionosphere_phase_screen", "ionosphere_phase_screen", iono_valid),
        ("mask", "mask_values", mask_valid),
    ):
        layer_stats[role] = _array_stats(layers[role]["values"], valid_mask)
    if "ionosphere_phase_screen_uncertainty" in layers:
        unc = layers["ionosphere_phase_screen_uncertainty"]
        layer_stats["ionosphere_phase_screen_uncertainty"] = _array_stats(
            unc["values"], unc["inside"] & unc["valid_data"] & np.isfinite(unc["values"])
        )

    return {
        "service": "SARGuardian GUNW AOI quality-gate diagnostics",
        "mode": "multi-layer-aoi-quality-diagnostics",
        "granule_name": granule_name,
        "polarization": polarization.upper(),
        "job_statuses": {role: "successful" for role in layers},
        "aoi_cropped_in_memory": True,
        "full_raster_downloaded_by_sarguardian": False,
        "persistent_storage_used": False,
        "aoi": {
            "window_width_px": int(reference["window_width"]),
            "window_height_px": int(reference["window_height"]),
            "window_pixels": int(reference["window_pixels"]),
            "polygon_pixels": int(inside.sum()),
            "window_bounds_wgs84": reference["window_bounds_wgs84"],
            "crs": reference["crs"],
        },
        "layers": layer_stats,
        "quality_gates": {
            "phase_nonzero_finite_pixels": int(phase_valid.sum()),
            "coherence_valid_pixels": int(coherence_valid.sum()),
            "coherence_threshold": coherence_threshold,
            "coherence_pass_pixels": int(coherence_pass.sum()),
            "connected_components_positive_pixels": int(components_pass.sum()),
            "ionosphere_phase_screen_valid_pixels": int(iono_valid.sum()),
            "mask_valid_land_subswath_pixels": int(mask_pass.sum()),
            "jointly_qualified_pixels": int(jointly_valid.sum()),
            "jointly_qualified_fraction_of_polygon": round(float(jointly_valid.sum() / max(1, int(inside.sum()))), 6),
            "dominant_connected_component_id": dominant_component,
            "dominant_component_share_among_phase_coherence_pixels": dominant_component_share,
            "distinct_positive_components_in_phase_coherence_pixels": component_count,
        },
        "ionosphere_corrected_phase_statistics": corrected_phase_stats,
        "physical_displacement": {
            "computed": False,
            "unit": None,
            "reason": "The phase remains unreferenced and a stable reference area plus verified source-product metadata are required before reporting LOS displacement.",
        },
        "original_scientific_engine": {
            "source_module": "Backend/src/gunw_reader.py",
            "hdf5_read_gunw_invoked": False,
            "reader_compatible_core_gates_applied_to_aligned_aoi_arrays": True,
            "gates": [
                "nonzero finite unwrapped phase",
                f"coherence magnitude >= {coherence_threshold}",
                "connected component ID > 0",
                "GUNW mask decoded for water and reference/secondary subswath validity",
                "ionosphere phase screen subtracted where valid",
            ],
            "note": "This is an array adapter aligned with the original reader's quality gates, not a call to its HDF5-only read_gunw function. No reference was selected and no LOS displacement was computed.",
        },
        "warnings": [
            "These are AOI-only diagnostics from layers of one interferometric pair, not a temporal change map.",
            "No landslide detection, prediction, or emergency warning has been performed.",
            "Layer grids were checked for exact alignment before any combined quality gate was calculated.",
        ],
    }
