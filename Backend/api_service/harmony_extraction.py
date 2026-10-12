"""Guarded NISAR GUNW Harmony extraction and AOI quality diagnostics.

The module queues one-variable Harmony jobs, reads bounded AOI windows from
remote COG results, and can compare the same window against the native GUNW
HDF5 mask using authenticated HTTP range access. It does not intentionally
fetch or persist complete source/results files and does not compute validated
LOS displacement or landslide detections.
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
            role in jobs for role in (
                "unwrapped_phase", "coherence", "connected_components",
                "ionosphere_phase_screen", "ionosphere_phase_screen_uncertainty", "mask",
            )
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
                values = np.asarray(data.data)
                # Preserve native data type. In particular, GUNW mask is a packed
                # uint32 categorical word; coercing it to float can conceal upstream
                # precision loss and makes later integer decoding unsafe.
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
                    "dtype": str(dataset.dtypes[0]),
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


def _decode_h5_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
    return str(value).strip("\x00 \t\r\n")


def _h5_dataset_by_basename(h5_file: Any, basename: str, preferred: list[str], h5py_module: Any) -> tuple[str, Any]:
    """Resolve a named coordinate/projection dataset without reading raster arrays."""
    for path in preferred:
        clean = path if path.startswith("/") else "/" + path
        if clean in h5_file and isinstance(h5_file[clean], h5py_module.Dataset):
            return clean, h5_file[clean]
    candidates: list[str] = []
    def visitor(path: str, obj: Any) -> None:
        if isinstance(obj, h5py_module.Dataset) and path.rsplit("/", 1)[-1] == basename and "/science/LSAR/GUNW/grids/frequencyA/" in ("/" + path):
            candidates.append("/" + path)
    h5_file.visititems(visitor)
    if len(candidates) == 1:
        return candidates[0], h5_file[candidates[0]]
    if candidates:
        # Prefer an axis or mapping dataset closest to the mask's group; never guess if ambiguous.
        raise HarmonyJobError(f"The original GUNW HDF5 contains multiple candidate {basename} datasets; source-mask alignment cannot be verified safely.")
    raise HarmonyJobError(f"The original GUNW HDF5 does not expose a recognizable {basename} dataset in frequencyA; source-mask alignment cannot be verified safely.")


def _nearest_monotonic_indices(axis: Any, targets: Any) -> tuple[Any, Any, float]:
    """Return nearest source-axis indices, coordinate errors, and median posting."""
    import numpy as np
    values = np.asarray(axis, dtype=np.float64)
    target_values = np.asarray(targets, dtype=np.float64)
    if values.ndim != 1 or values.size < 2 or not np.isfinite(values).all():
        raise HarmonyJobError("The source HDF5 coordinate axis is empty or invalid.")
    diffs = np.diff(values)
    ascending = bool(np.all(diffs > 0))
    descending = bool(np.all(diffs < 0))
    if not (ascending or descending):
        raise HarmonyJobError("The source HDF5 coordinate axis is not strictly monotonic; exact pixel alignment cannot be verified.")
    search_axis = values if ascending else values[::-1]
    positions = np.searchsorted(search_axis, target_values, side="left")
    left = np.clip(positions - 1, 0, search_axis.size - 1)
    right = np.clip(positions, 0, search_axis.size - 1)
    use_right = np.abs(search_axis[right] - target_values) < np.abs(search_axis[left] - target_values)
    selected = np.where(use_right, right, left)
    errors = np.abs(search_axis[selected] - target_values)
    indices = selected if ascending else values.size - 1 - selected
    posting = float(np.median(np.abs(diffs)))
    return indices.astype(np.int64), errors, posting


def _safe_h5_metadata_value(value: Any, np: Any, *, max_items: int = 16) -> Any:
    """Make a bounded, JSON-safe view of HDF5 metadata values only."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00 \t\r\n")[:500]
    if isinstance(value, str):
        return value.strip("\x00 \t\r\n")[:500]
    if isinstance(value, np.generic):
        return _safe_h5_metadata_value(value.item(), np, max_items=max_items)
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return _safe_h5_metadata_value(value.item(), np, max_items=max_items)
        if value.size > max_items:
            return {"shape": [int(n) for n in value.shape], "dtype": str(value.dtype), "preview": [
                _safe_h5_metadata_value(item, np, max_items=max_items) for item in value.reshape(-1)[:max_items]
            ]}
        return [_safe_h5_metadata_value(item, np, max_items=max_items) for item in value.reshape(-1)]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        values = list(value)
        if len(values) > max_items:
            return {"length": len(values), "preview": [
                _safe_h5_metadata_value(item, np, max_items=max_items) for item in values[:max_items]
            ]}
        return [_safe_h5_metadata_value(item, np, max_items=max_items) for item in values]
    return str(value)[:500]


def _selected_h5_attributes(attrs: Any, np: Any) -> dict[str, Any]:
    """Return attribute names plus values useful for interpreting masks/QA; no raster values."""
    terms = ("long_name", "description", "standard_name", "units", "flag", "mask", "fill", "valid", "grid", "comment", "source", "version", "ionosphere", "quality", "qa")
    result: dict[str, Any] = {"attribute_names": sorted(str(key) for key in attrs.keys())}
    selected: dict[str, Any] = {}
    for raw_key in attrs.keys():
        key = str(raw_key)
        if any(term in key.lower() for term in terms):
            try:
                selected[key] = _safe_h5_metadata_value(attrs[raw_key], np)
            except Exception:
                selected[key] = "<attribute could not be decoded>"
    result["selected_attributes"] = selected
    return result


def _related_mask_qa_objects(h5_file: Any, h5py_module: Any, np: Any) -> list[dict[str, Any]]:
    """List a bounded set of related QA/mask datasets without reading their raster pixels."""
    results: list[dict[str, Any]] = []
    roots = (
        "/science/LSAR/GUNW/grids/frequencyA",
        "/science/LSAR/GUNW/metadata",
    )
    seen: set[str] = set()
    terms = ("validdatamask", "inputdataexceptionmask", "ionosphereoutlier", "ionosphere", "mask", "quality", "qa", "productversion", "softwareversion")
    for root_path in roots:
        if root_path not in h5_file:
            continue
        def visitor(relative_path: str, obj: Any) -> None:
            if len(results) >= 40:
                return
            full_path = root_path.rstrip("/") + "/" + relative_path.strip("/") if relative_path else root_path
            if full_path in seen:
                return
            basename = full_path.rsplit("/", 1)[-1].lower()
            if not any(term in basename for term in terms):
                return
            # Avoid echoing arbitrary content: include object descriptors and a small attribute allowlist only.
            item: dict[str, Any] = {"path": full_path, "object_type": "dataset" if isinstance(obj, h5py_module.Dataset) else "group"}
            if isinstance(obj, h5py_module.Dataset):
                item["dtype"] = str(obj.dtype)
                if obj.shape is not None:
                    item["shape"] = [int(n) for n in obj.shape]
            item["metadata"] = _selected_h5_attributes(obj.attrs, np)
            results.append(item)
            seen.add(full_path)
        try:
            h5_file[root_path].visititems(visitor)
        except Exception:
            # Related metadata is supplementary; the primary source/COG comparison remains valid.
            continue
    return results


def _integer_pair_counts(source_values: Any, cog_values: Any, compared: Any) -> list[dict[str, int]]:
    import numpy as np
    source = np.asarray(source_values)[compared].astype(np.uint64, copy=False)
    cog = np.asarray(cog_values)[compared].astype(np.uint64, copy=False)
    if source.size == 0:
        return []
    pairs = np.column_stack((source, cog))
    unique, counts = np.unique(pairs, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1][:10]
    return [
        {"source_value": int(unique[idx, 0]), "cog_value": int(unique[idx, 1]), "count": int(counts[idx])}
        for idx in order
    ]


def verify_gunw_source_mask(
    *, granule_name: str, mask_job_id: str, maturity: str, polarization: str,
    coordinates: list[tuple[float, float]],
) -> dict[str, Any]:
    """Compare a tiny native HDF5 mask window to its Harmony COG counterpart.

    Uses authenticated HTTP range reads through Earthaccess/fsspec and h5py's
    file-like driver. It does not download or persist the complete HDF5 product.
    A result is verified only when the native dataset is available and exact
    source/COG grid alignment has been independently established.
    """
    import numpy as np
    import rasterio
    from affine import Affine

    name = granule_name.strip()
    pol = polarization.upper()
    maturity_key = maturity.upper()
    if not re.fullmatch(r"NISAR_L2_(?:PR|UR)_GUNW_[A-Za-z0-9_]+", name):
        raise HarmonyJobError("Provide an exact NISAR L2 PR/UR GUNW granule name; arbitrary URLs are not accepted.")
    if pol not in {"HH", "VV"}:
        raise HarmonyJobError("Polarization must be HH or VV.")
    if maturity_key not in {"PROVISIONAL", "BETA"}:
        raise HarmonyJobError("Maturity must be PROVISIONAL or BETA.")
    if not JOB_ID_RE.fullmatch(mask_job_id):
        raise HarmonyJobError("Invalid Harmony mask job ID.")
    expected_mask_variable = _expected_gunw_variable("mask", pol)
    cog_mask = _read_job_aoi_layer(
        job_id=mask_job_id,
        granule_name=name,
        polarization=pol,
        role="mask",
        coordinates=coordinates,
    )
    cog_values = np.asarray(cog_mask["values"])
    inside = np.asarray(cog_mask["inside"], dtype=bool)
    cog_valid = np.asarray(cog_mask["valid_data"], dtype=bool) & inside & np.isfinite(cog_values)
    grid = cog_mask.get("grid") or {}
    if not grid or not grid.get("window_transform"):
        raise HarmonyJobError("The Harmony mask COG did not provide its AOI grid transform; native source comparison is not possible.")
    transform_values = tuple(float(value) for value in grid["window_transform"])
    if len(transform_values) < 6:
        raise HarmonyJobError("The Harmony mask COG provided an incomplete grid transform; native source comparison is not possible.")
    cog_transform = Affine(*transform_values[:6])
    height, width = cog_values.shape
    if height != int(grid.get("window_height", height)) or width != int(grid.get("window_width", width)):
        raise HarmonyJobError("The Harmony mask AOI array does not match its reported window dimensions.")
    if abs(cog_transform.b) > 1e-8 or abs(cog_transform.d) > 1e-8:
        raise HarmonyJobError("The Harmony mask COG uses a rotated grid; this diagnostic currently requires a north-up grid.")
    cog_x = cog_transform.c + (np.arange(width, dtype=np.float64) + 0.5) * cog_transform.a
    cog_y = cog_transform.f + (np.arange(height, dtype=np.float64) + 0.5) * cog_transform.e

    token = _token()
    try:
        import earthaccess
        import h5py
    except ImportError as exc:
        raise HarmonyJobError(
            "Native source verification dependencies are not installed. Redeploy the backend after installing earthaccess, h5py, and fsspec; no source mask was verified."
        ) from exc

    short_name = f"NISAR_L2_GUNW_{maturity_key}_V1"
    source_url = (
        "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/"
        f"{short_name}/{name}/{name}.h5"
    )
    try:
        # Reuse the same backend-only Earthdata token already used by Harmony.
        os.environ["EARTHDATA_TOKEN"] = token
        auth = earthaccess.login(strategy="environment")
        if not getattr(auth, "authenticated", False):
            raise RuntimeError("Earthdata authentication did not complete")
        http_fs = earthaccess.get_fsspec_https_session()
        # fsspec performs bounded HTTP range reads; the HDF5 file is never saved.
        with http_fs.open(source_url, "rb", block_size=2 * 1024 * 1024, cache_type="readahead") as remote_file:
            with h5py.File(remote_file, "r") as source_h5:
                mask_path_candidates = [
                    "/science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/mask",
                    "/science/LSAR/GUNW/grids/frequencyA/mask",
                ]
                mask_path = next((path for path in mask_path_candidates if path in source_h5 and isinstance(source_h5[path], h5py.Dataset)), None)
                if mask_path is None:
                    raise HarmonyJobError("The original HDF5 did not contain the expected native GUNW mask dataset; source verification is inconclusive.")
                source_mask_ds = source_h5[mask_path]
                if source_mask_ds.ndim != 2 or not np.issubdtype(source_mask_ds.dtype, np.integer):
                    raise HarmonyJobError(f"The native GUNW mask dataset type/shape is unexpected ({source_mask_ds.dtype}, {source_mask_ds.shape}); no policy decision was made.")
                base = "/science/LSAR/GUNW/grids/frequencyA"
                def attached_scale_paths(dimension_index: int) -> list[str]:
                    try:
                        return [str(scale.name) for scale in source_mask_ds.dims[dimension_index].values()]
                    except Exception:
                        return []
                # Prefer dimension scales actually attached to the native mask, then
                # fall back to conventional GUNW paths. This avoids choosing an axis
                # belonging to a different grid when the HDF5 contains several grids.
                x_preferred = attached_scale_paths(1) + [
                    f"{base}/xCoordinates", f"{source_mask_ds.parent.name}/xCoordinates",
                ]
                y_preferred = attached_scale_paths(0) + [
                    f"{base}/yCoordinates", f"{source_mask_ds.parent.name}/yCoordinates",
                ]
                x_path, x_ds = _h5_dataset_by_basename(source_h5, "xCoordinates", x_preferred, h5py)
                y_path, y_ds = _h5_dataset_by_basename(source_h5, "yCoordinates", y_preferred, h5py)
                x_axis = np.asarray(x_ds[...], dtype=np.float64)
                y_axis = np.asarray(y_ds[...], dtype=np.float64)
                if source_mask_ds.shape != (y_axis.size, x_axis.size):
                    raise HarmonyJobError(
                        f"Native mask shape {source_mask_ds.shape} does not match y/x coordinate lengths {(y_axis.size, x_axis.size)}; source comparison is inconclusive."
                    )
                mapping_attr = source_mask_ds.attrs.get("grid_mapping", "projection")
                mapping_name = _decode_h5_text(mapping_attr)
                projection_candidates = [
                    f"{source_mask_ds.parent.name}/{mapping_name}",
                    f"{base}/{mapping_name}",
                    f"{source_mask_ds.parent.name}/projection",
                    f"{base}/projection",
                ]
                projection_path = next((path for path in projection_candidates if path in source_h5 and isinstance(source_h5[path], h5py.Dataset)), None)
                if projection_path is None:
                    projection_path, projection_ds = _h5_dataset_by_basename(source_h5, "projection", projection_candidates, h5py)
                else:
                    projection_ds = source_h5[projection_path]
                projection_value = projection_ds[()]
                try:
                    source_epsg = int(np.asarray(projection_value).item())
                except (TypeError, ValueError):
                    raw_epsg = projection_ds.attrs.get("epsg_code") or projection_ds.attrs.get("EPSG")
                    if raw_epsg is None:
                        raise HarmonyJobError("Native GUNW projection metadata did not expose an EPSG integer; source-grid alignment is inconclusive.")
                    source_epsg = int(np.asarray(raw_epsg).item())
                source_crs = rasterio.crs.CRS.from_epsg(source_epsg)
                cog_crs = rasterio.crs.CRS.from_user_input(str(grid.get("crs", "")))
                if source_crs != cog_crs:
                    raise HarmonyJobError(
                        f"Native source CRS {source_crs} differs from Harmony COG CRS {cog_crs}; pixelwise mask comparison was stopped."
                    )
                col_indices, x_errors, x_posting = _nearest_monotonic_indices(x_axis, cog_x)
                row_indices, y_errors, y_posting = _nearest_monotonic_indices(y_axis, cog_y)
                x_tolerance = max(0.01, x_posting * 1e-4)
                y_tolerance = max(0.01, y_posting * 1e-4)
                x_aligned = bool(np.all(x_errors <= x_tolerance))
                y_aligned = bool(np.all(y_errors <= y_tolerance))
                x_steps = np.diff(col_indices)
                y_steps = np.diff(row_indices)
                x_contiguous = x_steps.size == 0 or bool(np.all(np.abs(x_steps) == 1))
                y_contiguous = y_steps.size == 0 or bool(np.all(np.abs(y_steps) == 1))
                if not (x_aligned and y_aligned and x_contiguous and y_contiguous):
                    return {
                        "service": "SARGuardian native GUNW source-mask verifier",
                        "mode": "source-mask-grid-not-aligned",
                        "verified": False,
                        "comparison_performed": False,
                        "granule_name": name,
                        "polarization": pol,
                        "native_source_file_opened": True,
                        "native_mask_dataset_path": mask_path,
                        "native_mask_dtype": str(source_mask_ds.dtype),
                        "native_crs": str(source_crs),
                        "cog_crs": str(cog_crs),
                        "max_x_center_error_m": round(float(np.max(x_errors)) if x_errors.size else 0.0, 6),
                        "max_y_center_error_m": round(float(np.max(y_errors)) if y_errors.size else 0.0, 6),
                        "full_source_hdf5_downloaded": False,
                        "aoi_source_window_read_remotely": False,
                        "persistent_storage_used": False,
                        "message": "The native source and Harmony COG pixel centres do not align within a strict tolerance. Pixelwise comparison was stopped; no mask gate was changed.",
                    }
                row_start, row_end = int(row_indices.min()), int(row_indices.max()) + 1
                col_start, col_end = int(col_indices.min()), int(col_indices.max()) + 1
                source_block = np.asarray(source_mask_ds[row_start:row_end, col_start:col_end])
                native_values = source_block[np.ix_(row_indices - row_start, col_indices - col_start)]
                if native_values.shape != cog_values.shape:
                    raise HarmonyJobError("Native source mask window and COG mask window shapes differ after coordinate alignment; comparison stopped.")
                source_fill = source_mask_ds.attrs.get("_FillValue")
                native_valid = np.ones(native_values.shape, dtype=bool)
                if source_fill is not None:
                    native_valid &= native_values != np.asarray(source_fill).item()
                comparable = inside & cog_valid & native_valid
                if not np.issubdtype(cog_values.dtype, np.integer):
                    rounded_cog = np.rint(cog_values)
                    comparable &= np.isfinite(cog_values) & (np.abs(cog_values - rounded_cog) < 1e-6) & (rounded_cog >= 0)
                    cog_int = np.where(np.isfinite(rounded_cog) & (rounded_cog >= 0), rounded_cog, 0).astype(np.uint64)
                else:
                    cog_int = cog_values.astype(np.uint64, copy=False)
                source_uint = native_values.astype(np.uint64, copy=False)
                matches = comparable & (source_uint == cog_int)
                mismatches = comparable & (source_uint != cog_int)
                mismatch_count = int(mismatches.sum())
                match_count = int(matches.sum())
                float32_rounding_compatible = 0
                if mismatch_count:
                    eligible = mismatches & (source_uint <= np.uint64(0xFFFFFFFF))
                    rounded_native = np.asarray(source_uint, dtype=np.float32).astype(np.uint64)
                    float32_rounding_compatible = int((eligible & (rounded_native == cog_int)).sum())
                pair_counts = _integer_pair_counts(source_uint, cog_int, mismatches)
                src_dtype = str(source_mask_ds.dtype)
                source_samples = source_uint[comparable]
                low_byte = source_samples & np.uint64(0xFF)
                secondary_exception_byte = (source_samples >> np.uint64(8)) & np.uint64(0xFF)
                reference_exception_byte = (source_samples >> np.uint64(16)) & np.uint64(0xFF)
                bit_24_set = (source_samples & np.uint64(1 << 24)) != 0
                upper_reserved_bits_set = (source_samples & np.uint64(0xFE000000)) != 0
                def count_values(values: Any) -> list[dict[str, int]]:
                    unique, counts = np.unique(np.asarray(values), return_counts=True)
                    order = np.argsort(counts)[::-1][:16]
                    return [{"value": int(unique[idx]), "count": int(counts[idx])} for idx in order]
                source_bit_counts = {
                    "samples": int(source_samples.size),
                    "low_byte_values": count_values(low_byte),
                    "secondary_exception_byte_nonzero_pixels": int(np.count_nonzero(secondary_exception_byte)),
                    "reference_exception_byte_nonzero_pixels": int(np.count_nonzero(reference_exception_byte)),
                    "bit_24_set_pixels": int(np.count_nonzero(bit_24_set)),
                    "bit_24_clear_pixels": int(source_samples.size - np.count_nonzero(bit_24_set)),
                    "bits_25_to_31_nonzero_pixels": int(np.count_nonzero(upper_reserved_bits_set)),
                    "full_values": count_values(source_samples),
                }
                native_mask_metadata = _selected_h5_attributes(source_mask_ds.attrs, np)
                native_mask_group_metadata = _selected_h5_attributes(source_mask_ds.parent.attrs, np)
                related_qa_objects = _related_mask_qa_objects(source_h5, h5py, np)
                src_dtype = str(source_mask_ds.dtype)
                return {
                    "service": "SARGuardian native GUNW source-mask verifier",
                    "mode": "source-mask-window-compared",
                    "verified": bool(comparable.sum() > 0),
                    "comparison_performed": bool(comparable.sum() > 0),
                    "mask_values_match": bool(mismatch_count == 0 and match_count > 0),
                    "granule_name": name,
                    "polarization": pol,
                    "harmony_mask_job_id": mask_job_id,
                    "harmony_mask_variable": expected_mask_variable,
                    "source_collection": short_name,
                    "native_source_host": "nisar.asf.earthdatacloud.nasa.gov",
                    "native_source_file_opened": True,
                    "native_mask_dataset_path": mask_path,
                    "native_mask_dtype": src_dtype,
                    "native_mask_shape": [int(source_mask_ds.shape[0]), int(source_mask_ds.shape[1])],
                    "native_mask_metadata": native_mask_metadata,
                    "native_mask_parent_group": str(source_mask_ds.parent.name),
                    "native_mask_parent_metadata": native_mask_group_metadata,
                    "source_mask_bit_counts": source_bit_counts,
                    "related_mask_qa_objects": related_qa_objects,
                    "native_x_coordinates_path": x_path,
                    "native_y_coordinates_path": y_path,
                    "native_projection_dataset_path": projection_path,
                    "native_crs": str(source_crs),
                    "cog_dtype": str(cog_mask.get("dtype", cog_values.dtype)),
                    "cog_crs": str(cog_crs),
                    "grid_alignment": {
                        "pixel_centres_aligned": True,
                        "x_coordinate_max_error_m": round(float(np.max(x_errors)) if x_errors.size else 0.0, 6),
                        "y_coordinate_max_error_m": round(float(np.max(y_errors)) if y_errors.size else 0.0, 6),
                        "x_alignment_tolerance_m": round(float(x_tolerance), 6),
                        "y_alignment_tolerance_m": round(float(y_tolerance), 6),
                    },
                    "comparison": {
                        "aoi_window_width_px": int(width),
                        "aoi_window_height_px": int(height),
                        "polygon_pixels": int(inside.sum()),
                        "comparable_pixels": int(comparable.sum()),
                        "exact_matches": match_count,
                        "mismatches": mismatch_count,
                        "exact_match_fraction": round(match_count / max(1, int(comparable.sum())), 6),
                        "mismatch_pairs_top10": pair_counts,
                        "mismatches_compatible_with_float32_rounding": float32_rounding_compatible,
                    },
                    "full_source_hdf5_downloaded": False,
                    "aoi_source_window_read_remotely": True,
                    "persistent_storage_used": False,
                    "quality_gate_changed": False,
                    "scientific_displacement_calculated": False,
                    "interpretation": (
                        "Native source mask and Harmony COG values match for every comparable AOI pixel; this window shows no mask-value conversion difference."
                        if mismatch_count == 0 and match_count > 0 else
                        "Native source and Harmony COG mask values differ at one or more aligned AOI pixels. The mismatch pairs are evidence to investigate the conversion path; they do not by themselves authorize accepting flagged pixels."
                        if mismatch_count > 0 else
                        "No valid source/COG pixel pairs were available to compare. Verification is inconclusive."
                    ),
                    "next_step": (
                        "No mask conversion mismatch was found in the compared AOI window. The response now includes native mask attributes, AOI bit/byte counts, and related source QA/mask metadata objects. Use those metadata to confirm flag semantics; do not relax the strict gate."
                        if mismatch_count == 0 and match_count > 0 else
                        "Inspect mismatch_pairs_top10 and the native dataset type. If mismatches are compatible with float32 rounding, trace the Harmony/GDAL conversion before revisiting any gate."
                        if mismatch_count > 0 else
                        "Do not change the gate; select a better-aligned valid AOI or inspect source NoData metadata."
                    ),
                }
    except HarmonyJobError:
        raise
    except Exception as exc:
        # Deliberately suppress low-level HTTP/HDF5 details; never echo credentials or signed URLs.
        raise HarmonyJobError(
            "Could not safely read the original NISAR HDF5 source mask with authenticated range access. The source mask was NOT verified and the quality gate was NOT changed. Check the Render EARTHDATA_TOKEN, native file access permission, service region, and the new backend dependencies. No full source file was intentionally downloaded or persisted."
        ) from exc

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


def _layer_value_diagnostics(layer: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Return compact value/nodata diagnostics for one AOI window.

    This is particularly important for connectedComponents: a zero count can
    mean genuinely unassigned pixels, a raster NoData encoding issue, or an
    extraction/conversion issue. The diagnostic must distinguish these cases
    rather than silently treating zero qualified pixels as a scientific result.
    """
    import numpy as np

    values = np.asarray(layer["values"], dtype=np.float64)
    inside = np.asarray(layer["inside"], dtype=bool)
    valid_data = np.asarray(layer["valid_data"], dtype=bool) & np.isfinite(values)
    in_poly_finite = inside & np.isfinite(values)
    valid_inside = inside & valid_data
    poly_values = values[inside]
    valid_values = values[valid_inside]

    result: dict[str, Any] = {
        "dtype": layer.get("dtype"),
        "nodata_value": layer.get("nodata"),
        "polygon_pixels": int(inside.sum()),
        "finite_pixels_in_polygon": int(in_poly_finite.sum()),
        "valid_data_pixels_in_polygon": int(valid_inside.sum()),
        "masked_or_nodata_pixels_in_polygon": int((inside & ~valid_data).sum()),
        "zero_value_pixels_in_polygon_including_masked": int((inside & np.isfinite(values) & (values == 0)).sum()),
        "zero_value_pixels_among_valid_data": int((valid_inside & (values == 0)).sum()),
        "positive_value_pixels_among_valid_data": int((valid_inside & (values > 0)).sum()),
        "negative_value_pixels_among_valid_data": int((valid_inside & (values < 0)).sum()),
        "min_valid": float(np.min(valid_values)) if valid_values.size else None,
        "max_valid": float(np.max(valid_values)) if valid_values.size else None,
        "mean_valid": float(np.mean(valid_values)) if valid_values.size else None,
    }

    if role in {"connected_components", "mask"}:
        # The AOI is bounded by MAX_WINDOW_PIXELS, so compact categorical
        # summaries are safe; never return raster arrays to the client.
        categorical = valid_values
        if categorical.size:
            unique_values, counts = np.unique(categorical.astype(np.int64), return_counts=True)
            order = np.argsort(counts)[::-1][:8]
            result["unique_value_count"] = int(unique_values.size)
            result["most_common_values"] = [
                {"value": int(unique_values[i]), "count": int(counts[i])}
                for i in order
            ]
        else:
            result["unique_value_count"] = 0
            result["most_common_values"] = []

    if role == "mask":
        # GUNW mask is uint32 bit-packed. Bits 0-7 contain the WRS code;
        # bits 8-15 and 16-23 carry secondary/reference input exception masks;
        # bit 24 is the ionosphere filter/interpolation flag.
        raw_u32 = np.zeros(values.shape, dtype=np.uint32)
        if np.any(valid_inside):
            raw_u32[valid_inside] = np.rint(values[valid_inside]).astype(np.uint32)
        wrs = raw_u32 & np.uint32(0xFF)
        secondary_exception = (raw_u32 >> np.uint32(8)) & np.uint32(0xFF)
        reference_exception = (raw_u32 >> np.uint32(16)) & np.uint32(0xFF)
        ionosphere_filter_flag = (raw_u32 & np.uint32(1 << 24)) != 0
        # ISCE3 PR #381 documents a float32 geocoding bug where 16777227 can
        # round to 16777228 and alter the low-byte WRS code. When packed values
        # are >= 2**24, do not assume that the low-byte swath digit is reliable.
        packed_wrs_ambiguous = valid_inside & (raw_u32 >= np.uint32(1 << 24))
        water_flag = wrs // np.uint32(100)
        ref_subswath = (wrs // np.uint32(10)) % np.uint32(10)
        sec_subswath = wrs % np.uint32(10)
        base_wrs_valid = (
            valid_inside
            & (wrs != np.uint32(255))
            & (water_flag == 0)
            & (ref_subswath > 0)
            & (sec_subswath > 0)
        )
        result["mask_encoding"] = (
            "uint32 bit-packed: bits 0-7 WRS code; bits 8-15 secondary input exception; "
            "bits 16-23 reference input exception; bit 24 ionosphere filter/interpolation flag"
        )
        if np.any(valid_inside):
            codes, code_counts = np.unique(wrs[valid_inside], return_counts=True)
            order_codes = np.argsort(code_counts)[::-1][:8]
            result["wrs_low_byte_value_counts"] = [
                {"value": int(codes[i]), "count": int(code_counts[i])}
                for i in order_codes
            ]
        else:
            result["wrs_low_byte_value_counts"] = []
        result["base_wrs_land_valid_subswath_pixels"] = int(base_wrs_valid.sum())
        result["reference_input_exception_nonzero_pixels"] = int((valid_inside & (reference_exception != 0)).sum())
        result["secondary_input_exception_nonzero_pixels"] = int((valid_inside & (secondary_exception != 0)).sum())
        result["ionosphere_filter_flag_pixels"] = int((valid_inside & ionosphere_filter_flag).sum())
        result["packed_wrs_decode_ambiguous_pixels"] = int(packed_wrs_ambiguous.sum())
        result["wrs_decode_reliable_pixels"] = int((valid_inside & ~packed_wrs_ambiguous).sum())
    return result


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
    required_roles = (
        "unwrapped_phase", "coherence", "connected_components",
        "ionosphere_phase_screen", "ionosphere_phase_screen_uncertainty", "mask",
    )
    missing = [role for role in required_roles if not job_ids.get(role)]
    if missing:
        raise HarmonyJobError("Missing completed Harmony job IDs for required layers: " + ", ".join(missing))

    layers: dict[str, dict[str, Any]] = {}
    for role in required_roles:
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
    iono_uncertainty_layer = layers["ionosphere_phase_screen_uncertainty"]
    iono_uncertainty = iono_uncertainty_layer["values"]
    mask_values = mask_layer["values"]

    # Match the core reader's gates: raw zero is treated as no phase; coherence
    # must meet threshold; connected component IDs must be positive. The GUNW
    # screen and its uncertainty are both required before corrected-phase pixels
    # can qualify. No arbitrary uncertainty magnitude threshold is invented.
    phase_valid = inside & phase_layer["valid_data"] & np.isfinite(phase) & (phase != 0)
    coherence_valid = inside & coh_layer["valid_data"] & np.isfinite(coherence)
    coherence_pass = coherence_valid & (coherence >= coherence_threshold)
    components_valid = inside & comp_layer["valid_data"] & np.isfinite(components)
    components_pass = components_valid & (components > 0)
    iono_valid = inside & iono_layer["valid_data"] & np.isfinite(iono)
    iono_uncertainty_valid = (
        inside & iono_uncertainty_layer["valid_data"] & np.isfinite(iono_uncertainty)
    )

    # The packed product mask schema is uint32. If Harmony returns a floating-
    # point or other converted mask, do not round/cast it and pretend the bits
    # have been recovered. Fail closed while exposing the dtype in diagnostics.
    mask_source_dtype = str(mask_layer.get("dtype") or np.asarray(mask_values).dtype).lower()
    mask_array_dtype = np.asarray(mask_values).dtype
    mask_dtype_valid = mask_source_dtype == "uint32" and mask_array_dtype == np.dtype("uint32")
    mask_valid = inside & mask_layer["valid_data"] & np.isfinite(mask_values)
    if not mask_dtype_valid:
        mask_valid = np.zeros(mask_values.shape, dtype=bool)

    # Decode the uint32 product mask without an intermediate float conversion.
    # WRS is the low byte; higher bytes carry input exceptions and flag fields.
    mask_uint32 = np.zeros(mask_values.shape, dtype=np.uint32)
    if mask_dtype_valid and np.any(mask_valid):
        mask_uint32[mask_valid] = mask_values[mask_valid]
    wrs_code = mask_uint32 & np.uint32(0xFF)
    secondary_input_exception = (mask_uint32 >> np.uint32(8)) & np.uint32(0xFF)
    reference_input_exception = (mask_uint32 >> np.uint32(16)) & np.uint32(0xFF)
    ionosphere_filter_flag = (mask_uint32 & np.uint32(1 << 24)) != 0
    # ISCE3 PR #381 documents a precision bug where float32 geocoding can
    # round 16777227 to 16777228, changing the low-byte WRS code. We therefore
    # exclude packed values >= 2**24 from the strict gate pending verification
    # against the source product or polarization-specific validDataMask.
    packed_wrs_ambiguous = mask_valid & (mask_uint32 >= np.uint32(1 << 24))
    water = wrs_code // np.uint32(100)
    ref_subswath = (wrs_code // np.uint32(10)) % np.uint32(10)
    sec_subswath = wrs_code % np.uint32(10)
    base_wrs_pass = (
        mask_valid
        & (wrs_code != np.uint32(255))
        & (water == 0)
        & (ref_subswath > 0)
        & (sec_subswath > 0)
    )
    mask_pass = (
        base_wrs_pass
        & (reference_input_exception == 0)
        & (secondary_input_exception == 0)
        & ~packed_wrs_ambiguous
    )
    # Bit 24 means pixels masked during ionosphere filtering and filled by
    # interpolation. Keep this as a separate strict gate and visible diagnostic.
    ionosphere_filter_unflagged = mask_valid & ~ionosphere_filter_flag

    jointly_valid = (
        phase_valid & coherence_pass & components_pass & iono_valid
        & iono_uncertainty_valid & mask_pass & ionosphere_filter_unflagged
    )

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
        ("ionosphere_phase_screen_uncertainty", "ionosphere_phase_screen_uncertainty", iono_uncertainty_valid),
        ("mask", "mask_values", mask_valid),
    ):
        layer_stats[role] = _array_stats(layers[role]["values"], valid_mask)

    # Gate-by-gate counts reveal exactly where candidate pixels are rejected.
    phase_and_coherence = phase_valid & coherence_pass
    after_components = phase_and_coherence & components_pass
    after_iono_screen = after_components & iono_valid
    after_iono_uncertainty = after_iono_screen & iono_uncertainty_valid
    # Existing candidate-to-mask diagnostic now means candidate pixels with
    # both a valid screen and valid uncertainty, matching correction eligibility.
    after_iono = after_iono_uncertainty
    after_mask = after_iono & mask_pass
    after_iono_filter_mask = after_mask & ionosphere_filter_unflagged

    # Cross-tab only the pixels that survived phase/coherence/components and
    # had a numerically valid ionosphere screen. This identifies whether the
    # zero final count is due to flagged/interpolated mask pixels, WRS/exception
    # rules, invalid mask samples, or the packed-integer precision quarantine.
    # Do not loosen the quality gate based on this diagnostic.
    candidate_mask_valid = after_iono & mask_valid
    candidate_raw_mask_values: list[dict[str, int]] = []
    candidate_low_byte_counts: list[dict[str, int]] = []
    if np.any(candidate_mask_valid):
        candidate_raw, candidate_raw_counts = np.unique(
            mask_uint32[candidate_mask_valid], return_counts=True
        )
        raw_order = np.argsort(candidate_raw_counts)[::-1][:10]
        candidate_raw_mask_values = [
            {"value": int(candidate_raw[i]), "count": int(candidate_raw_counts[i])}
            for i in raw_order
        ]
        candidate_low_bytes, candidate_low_counts = np.unique(
            wrs_code[candidate_mask_valid], return_counts=True
        )
        low_order = np.argsort(candidate_low_counts)[::-1][:10]
        candidate_low_byte_counts = [
            {"value": int(candidate_low_bytes[i]), "count": int(candidate_low_counts[i])}
            for i in low_order
        ]

    candidate_base_and_exceptions_pass = (
        after_iono
        & base_wrs_pass
        & (reference_input_exception == 0)
        & (secondary_input_exception == 0)
    )
    mask_intersection_diagnostics = {
        "candidate_population": "pixels remaining after phase + coherence + positive components + valid ionosphere phase screen + finite screen uncertainty",
        "candidate_pixels_after_ionosphere_validity": int(after_iono.sum()),
        "candidate_mask_valid_pixels": int(candidate_mask_valid.sum()),
        "candidate_mask_invalid_or_nodata_pixels": int((after_iono & ~mask_valid).sum()),
        "candidate_ionosphere_flagged_pixels": int((candidate_mask_valid & ionosphere_filter_flag).sum()),
        "candidate_ionosphere_unflagged_pixels": int((candidate_mask_valid & ~ionosphere_filter_flag).sum()),
        "candidate_packed_values_ge_2pow24_pixels": int((candidate_mask_valid & packed_wrs_ambiguous).sum()),
        "candidate_base_wrs_pass_pixels": int((after_iono & base_wrs_pass).sum()),
        "candidate_reference_exception_pixels": int((candidate_mask_valid & (reference_input_exception != 0)).sum()),
        "candidate_secondary_exception_pixels": int((candidate_mask_valid & (secondary_input_exception != 0)).sum()),
        "candidate_wrs_and_exceptions_pass_before_precision_quarantine_pixels": int(candidate_base_and_exceptions_pass.sum()),
        "candidate_strict_mask_pass_pixels": int((after_iono & mask_pass).sum()),
        "candidate_passes_all_mask_rules_pixels": int((after_iono & mask_pass & ionosphere_filter_unflagged).sum()),
        "candidate_raw_mask_value_counts_top10": candidate_raw_mask_values,
        "candidate_wrs_low_byte_counts_top10": candidate_low_byte_counts,
        "interpretation_note": "Cross-tab of already qualified candidate pixels only. Packed values >= 2^24 are conservatively quarantined because the low-byte WRS code may have lost precision; bit 24 flag remains checked. This diagnostic does not authorize relaxing any scientific gate.",
    }

    # This remains a phase-domain diagnostic only. It is intentionally null
    # when no pixel survives every quality gate; it is not LOS displacement.
    correction_input_valid = phase_valid & iono_valid & iono_uncertainty_valid
    corrected_phase = np.full(np.asarray(phase).shape, np.nan, dtype=np.float64)
    corrected_phase[correction_input_valid] = (
        phase[correction_input_valid] - iono[correction_input_valid]
    )
    corrected_phase_stats = _array_stats(corrected_phase, jointly_valid) if np.any(jointly_valid) else None
    layer_diagnostics = {
        role: _layer_value_diagnostics(layer, role=role)
        for role, layer in layers.items()
    }
    warnings: list[str] = []
    if not mask_dtype_valid:
        warnings.append(
            f"Packed GUNW mask dtype is {mask_source_dtype!r}, not native uint32. "
            "Mask bit decoding is unresolved; no mask pixels were allowed through the strict gate. "
            "Do not round or cast the mask to recover flags. Verify the Harmony output dtype and source product."
        )
    if int(iono_uncertainty_valid.sum()) == 0:
        warnings.append(
            "No finite ionosphere phase-screen uncertainty pixels are available in this AOI. "
            "Corrected-phase qualification is blocked rather than substituting zero uncertainty."
        )
    if int(components_pass.sum()) == 0:
        comp_diag = layer_diagnostics["connected_components"]
        if comp_diag["valid_data_pixels_in_polygon"] == 0:
            warnings.append(
                "No valid connected-components values were available inside the AOI. "
                "Inspect the layer NoData value and extraction output before applying the component gate."
            )
        elif comp_diag["positive_value_pixels_among_valid_data"] == 0:
            warnings.append(
                "Connected-components values are non-positive throughout the valid AOI pixels. "
                "This rejects the current quality gate; it is not evidence that no ground movement occurred."
            )
    if int(coherence_pass.sum()) > 0 and int(components_pass.sum()) == 0:
        warnings.append(
            "Coherence passes at some pixels but no connected-component IDs pass. "
            "Verify this layer against another GUNW granule or the original source product; do not disable the component gate."
        )
    mask_diag = layer_diagnostics["mask"]
    flagged_iono_pixels = int(mask_diag.get("ionosphere_filter_flag_pixels", 0))
    ambiguous_mask_pixels = int(mask_diag.get("packed_wrs_decode_ambiguous_pixels", 0))
    if flagged_iono_pixels or ambiguous_mask_pixels:
        warnings.append(
            f"The GUNW mask has {flagged_iono_pixels} ionosphere-filter/interpolation-flagged pixel(s) and "
            f"{ambiguous_mask_pixels} packed mask value(s) at or above 2^24. ISCE3 documents that float32 geocoding can "
            "round 16777227 to 16777228 and alter the low-byte WRS code; those pixels are excluded from strict qualification "
            "pending source-product or polarization-specific validDataMask verification."
        )
    candidate_count = int(after_iono.sum())
    candidate_flagged_count = int(mask_intersection_diagnostics["candidate_ionosphere_flagged_pixels"])
    if candidate_count > 0 and candidate_flagged_count == candidate_count:
        warnings.append(
            "Every pixel surviving the phase/coherence/components/ionosphere-validity stages overlaps an ionosphere-filter/interpolation-flagged mask value. "
            "A zero final qualified count is therefore consistent with the current conservative mask policy; verify the native source mask before any policy change."
        )
    elif candidate_count > 0 and int(mask_intersection_diagnostics["candidate_passes_all_mask_rules_pixels"]) == 0:
        warnings.append(
            "Candidate pixels exist, but none passes the mask rules. Use mask_intersection_diagnostics to inspect the candidate pixels' mask flags, exceptions, low-byte codes, and packed values; do not bypass the mask gate."
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
        "mask_semantics": {
            "expected_dtype": "uint32",
            "observed_dtype": mask_source_dtype,
            "dtype_valid": bool(mask_dtype_valid),
            "decode_status": "packed_uint32_decoded_with_high_flag_quarantine" if mask_dtype_valid else "unresolved_non_uint32_mask_dtype",
            "source_product_upstream_encoding_verified": False,
            "packed_values_ge_2pow24_quarantined": int(packed_wrs_ambiguous.sum()),
            "note": "Exact native-HDF5/COG agreement verifies transfer fidelity, not upstream mask encoding. Values >= 2^24 remain quarantined until independent validity evidence or a corrected source product resolves the ISCE3 float32-geocoding ambiguity.",
        },
        "aoi": {
            "window_width_px": int(reference["window_width"]),
            "window_height_px": int(reference["window_height"]),
            "window_pixels": int(reference["window_pixels"]),
            "polygon_pixels": int(inside.sum()),
            "window_bounds_wgs84": reference["window_bounds_wgs84"],
            "crs": reference["crs"],
        },
        "layers": layer_stats,
        "layer_diagnostics": layer_diagnostics,
        "mask_intersection_diagnostics": mask_intersection_diagnostics,
        "quality_gates": {
            "phase_nonzero_finite_pixels": int(phase_valid.sum()),
            "coherence_valid_pixels": int(coherence_valid.sum()),
            "coherence_threshold": coherence_threshold,
            "coherence_pass_pixels": int(coherence_pass.sum()),
            "phase_and_coherence_pixels": int(phase_and_coherence.sum()),
            "connected_components_positive_pixels": int(components_pass.sum()),
            "after_connected_components_pixels": int(after_components.sum()),
            "ionosphere_phase_screen_valid_pixels": int(iono_valid.sum()),
            "ionosphere_screen_uncertainty_valid_pixels": int(iono_uncertainty_valid.sum()),
            "after_ionosphere_screen_pixels": int(after_iono_screen.sum()),
            "after_ionosphere_uncertainty_validity_pixels": int(after_iono_uncertainty.sum()),
            "after_ionosphere_validity_pixels": int(after_iono.sum()),
            "mask_dtype_valid": bool(mask_dtype_valid),
            "mask_low_byte_land_valid_subswath_pixels": int(base_wrs_pass.sum()),
            "mask_reference_exception_pixels": int((mask_valid & (reference_input_exception != 0)).sum()),
            "mask_secondary_exception_pixels": int((mask_valid & (secondary_input_exception != 0)).sum()),
            "mask_ionosphere_filter_flagged_pixels": int((mask_valid & ionosphere_filter_flag).sum()),
            "mask_packed_wrs_ambiguous_pixels": int(packed_wrs_ambiguous.sum()),
            "mask_valid_land_subswath_pixels": int(mask_pass.sum()),
            "after_land_and_subswath_mask_pixels": int(after_mask.sum()),
            "after_ionosphere_filter_mask_pixels": int(after_iono_filter_mask.sum()),
            "jointly_qualified_pixels": int(jointly_valid.sum()),
            "jointly_qualified_fraction_of_polygon": round(float(jointly_valid.sum() / max(1, int(inside.sum()))), 6),
            "dominant_connected_component_id": dominant_component,
            "dominant_component_share_among_phase_coherence_pixels": dominant_component_share,
            "distinct_positive_components_in_phase_coherence_pixels": component_count,
        },
        "ionosphere_corrected_phase_statistics": corrected_phase_stats,
        "warnings": warnings,
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
                "ionosphere phase screen and finite uncertainty required for corrected-phase qualification",
            ],
            "note": "This is an array adapter aligned with the original reader's quality gates, not a call to its HDF5-only read_gunw function. No reference was selected and no LOS displacement was computed.",
        },
        "warnings": warnings + [
            "These are AOI-only diagnostics from layers of one interferometric pair, not a temporal change map.",
            "No landslide detection, prediction, or emergency warning has been performed.",
            "Layer grids were checked for exact alignment before any combined quality gate was calculated.",
        ],
    }
