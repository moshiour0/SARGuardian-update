"""Live Harmony capability discovery and conservative extraction planning.

This module intentionally does not launch Harmony jobs or download science data.
It discovers the exact current collection/layer identifiers first, so subsequent
extraction code does not guess a collection concept ID or HDF5 variable path.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

HARMONY_CAPABILITIES_URL = "https://harmony.earthdata.nasa.gov/capabilities"
HTTP_TIMEOUT_SECONDS = float(os.getenv("HARMONY_CAPABILITIES_TIMEOUT_SECONDS", "20"))
USER_AGENT = "SARGuardian/0.2 (NISAR layer-capability preflight; NASA Space Apps project)"

# Layer requirements match the original SARGuardian GUNW reader. Exact variable
# identifiers are resolved from Harmony at runtime, rather than hardcoded.
LAYER_ALIASES: dict[str, tuple[str, ...]] = {
    "unwrapped_phase": ("unwrappedphase",),
    "coherence": ("coherencemagnitude", "coherence"),
    "connected_components": ("connectedcomponents",),
    "ionosphere_phase_screen": ("ionospherephasescreen",),
    "mask": ("mask",),
}


class HarmonyLookupError(RuntimeError):
    """Harmony capability lookup failed or returned an unusable response."""


def _collection_short_name(product_type: str, production: str, maturity: str = "PROVISIONAL") -> str:
    """Return the CMR collection short name, not the granule processing-type prefix.

    NISAR granule names use PR/UR/OD for processing type (Production,
    Urgent Response, Science On-Demand). Harmony's collection short names use
    product maturity names such as *_PROVISIONAL_V1 and *_BETA_V1 instead.
    These are different concepts and must not be conflated.
    """
    maturity = maturity.upper().strip()
    if maturity not in {"PROVISIONAL", "BETA"}:
        raise ValueError("maturity must be PROVISIONAL or BETA")
    env_key = f"HARMONY_{product_type}_{maturity}_SHORT_NAME"
    default_name = f"NISAR_L2_{product_type}_{maturity}_V1"
    override = os.getenv(env_key, "").strip()
    if override:
        return override
    # Backward compatibility: respect a deliberate older custom override, but
    # never synthesize the invalid NISAR_L2_PR_GUNW-style value by default.
    legacy_key = f"HARMONY_{product_type}_{production}_SHORT_NAME"
    legacy_override = os.getenv(legacy_key, "").strip()
    old_invalid_default = f"NISAR_L2_{production}_{product_type}"
    if legacy_override and legacy_override != old_invalid_default:
        return legacy_override
    return default_name


def _http_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The most common configuration failure is querying a non-existent
        # collection short name. Keep the error useful without returning an
        # arbitrary HTML response body to the public API caller.
        if exc.code in {400, 404, 422}:
            message = (
                f"NASA Harmony rejected the collection capability request (HTTP {exc.code}). "
                "Check the collection short name and maturity; expected NISAR names are "
                "NISAR_L2_GUNW_PROVISIONAL_V1 / NISAR_L2_GUNW_BETA_V1 "
                "or the corresponding GOFF names. No satellite data was downloaded."
            )
        else:
            message = f"NASA Harmony returned HTTP {exc.code} for the capabilities request. No satellite data was downloaded."
        raise HarmonyLookupError(message) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        cause = getattr(exc, "reason", None)
        if isinstance(cause, TimeoutError) or isinstance(exc, TimeoutError):
            reason = "request timed out"
        elif isinstance(cause, OSError):
            reason = "network or DNS connection failed"
        else:
            reason = "network request or JSON response failed"
        raise HarmonyLookupError(
            f"Could not query NASA Harmony capabilities ({reason}). Check service availability and retry; no satellite data was downloaded."
        ) from exc
    if not isinstance(data, dict):
        raise HarmonyLookupError("NASA Harmony returned an unexpected capabilities response.")
    return data


def _extract_variable_values(node: Any) -> list[str]:
    """Flatten capability variables while preserving the best named/path field."""
    found: list[str] = []
    if isinstance(node, str):
        value = node.strip()
        if value:
            found.append(value)
    elif isinstance(node, list):
        for item in node:
            found.extend(_extract_variable_values(item))
    elif isinstance(node, dict):
        # Prefer a readable variable path/name; concept IDs are valid Harmony
        # targets too, but only used as a fallback when no path/name is given.
        readable_keys = ("name", "path", "variableName", "variable_name", "fullName", "full_name", "variable")
        for key in readable_keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
        if not found:
            for key in ("id", "conceptId", "concept_id"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
        # Capability schemas may nest individual layer descriptors.
        for key, value in node.items():
            if key not in {*readable_keys, "id", "conceptId", "concept_id"}:
                if isinstance(value, (dict, list)):
                    found.extend(_extract_variable_values(value))
    # stable deduplication
    return list(dict.fromkeys(found))


def _service_capabilities(payload: dict[str, Any]) -> dict[str, bool]:
    """Normalize Harmony capability schema v3 plus legacy v1/v2 responses.

    Version 3 nests subsetting flags under ``summary.subsetting`` and
    ``services[].capabilities.subsetting``. Older code only inspected flat
    ``variableSubset``-style names and incorrectly reported false for v3.
    """
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    service_list = payload.get("services") if isinstance(payload.get("services"), list) else []

    output: dict[str, bool] = {
        "variable_subset": False,
        "bbox_subset": False,
        "shape_subset": False,
        "temporal_subset": False,
    }
    key_map = {
        "variable_subset": ("variable", "variableSubset", "variable_subset", "variableSubsetting"),
        "bbox_subset": ("bbox", "bboxSubset", "bbox_subset", "boundingBoxSubset"),
        "shape_subset": ("shape", "shapeSubset", "shape_subset", "polygonSubset"),
        "temporal_subset": ("temporal", "temporalSubset", "temporal_subset"),
    }

    nodes: list[dict[str, Any]] = [payload, summary]
    # v3 summary often expresses output and subsetting capabilities as nested
    # structures. Include all common variants without assuming undocumented keys.
    for parent in (payload, summary):
        for child_key in ("subsetting", "capabilities", "operations"):
            child = parent.get(child_key)
            if isinstance(child, dict):
                nodes.append(child)
    for service in service_list:
        if not isinstance(service, dict):
            continue
        nodes.append(service)
        caps = service.get("capabilities")
        if isinstance(caps, dict):
            nodes.append(caps)
            for child_key in ("subsetting", "operations"):
                child = caps.get(child_key)
                if isinstance(child, dict):
                    nodes.append(child)

    for normalized, aliases in key_map.items():
        values: list[Any] = []
        for node in nodes:
            for key in aliases:
                if key in node:
                    values.append(node[key])
        output[normalized] = any(value is True for value in values)
    return output


def _normalize_variables(values: list[str]) -> list[str]:
    # Prefer actual data paths over bare aliases and service/metadata identifiers.
    return sorted(set(values), key=lambda value: (0 if "/" in value else 1, len(value), value.lower()))


def _resolve_layer(alias_values: list[str], layer_role: str, polarization: str = "HH") -> str | None:
    """Resolve a GUNW layer for one explicitly selected polarization.

    The NISAR GUNW capability list commonly contains HH and VV equivalents.
    The first reader version prefers the first frequency-A polarization found
    in the HDF5 tree (commonly HH); for an auditable API plan we select HH by
    default, allow VV explicitly, and never mix polarizations between layers.
    The unwrapped-grid mask is a grid-level variable, not a polarization child.
    """
    aliases = LAYER_ALIASES[layer_role]
    pol = polarization.upper()
    matches: list[str] = []
    for value in alias_values:
        low = value.lower().rstrip("/")
        basename = low.split("/")[-1].split(":")[-1]
        if not any(basename == alias for alias in aliases):
            continue
        if layer_role == "mask":
            if "unwrappedinterferogram" in low:
                matches.append(value)
            continue
        if "unwrappedinterferogram" not in low:
            continue
        if f"/{pol.lower()}/" not in low:
            continue
        matches.append(value)

    matches = _normalize_variables(matches)
    # Prefer the shared unwrapped-grid mask where present; only fall back to a
    # polarization-specific mask if that is the only form exposed.
    if layer_role == "mask" and matches:
        shared = [item for item in matches if f"/{pol.lower()}/" not in item.lower()]
        if shared:
            matches = shared
        else:
            matches = [item for item in matches if f"/{pol.lower()}/" in item.lower()]
    # The unwrapped grid can expose a single shared mask; all other requested
    # scientific layers must resolve to the selected polarization path.
    if not matches:
        return None
    if len(matches) > 1:
        # Prefer frequencyA if a newer collection exposes multiple frequencies;
        # if ambiguity remains, fail closed rather than mixing datasets.
        freq_a = [item for item in matches if "frequencya" in item.lower()]
        if len(freq_a) == 1:
            return freq_a[0]
        return None
    return matches[0]


@lru_cache(maxsize=8)
def _cached_capabilities(short_name: str) -> dict[str, Any]:
    url = f"{HARMONY_CAPABILITIES_URL}?{urllib.parse.urlencode({'shortName': short_name})}"
    payload = _http_json(url)
    # Cache a shallow JSON-native copy; caller routes should not mutate it.
    return payload


def get_harmony_capabilities(
    product_type: str, production: str = "PR", maturity: str = "PROVISIONAL",
    polarization: str = "HH",
) -> dict[str, Any]:
    pol = polarization.upper().strip()
    if pol not in {"HH", "VV"}:
        raise ValueError("polarization must be HH or VV")
    short_name = _collection_short_name(product_type, production, maturity)
    payload = _cached_capabilities(short_name)
    variables_node = payload.get("variables", [])
    variables = _normalize_variables(_extract_variable_values(variables_node))
    services_raw = payload.get("services", [])
    services = []
    if isinstance(services_raw, list):
        for service in services_raw:
            if not isinstance(service, dict):
                continue
            caps = service.get("capabilities") if isinstance(service.get("capabilities"), dict) else {}
            subset = caps.get("subsetting") if isinstance(caps.get("subsetting"), dict) else {}
            formats = caps.get("outputFormats") or caps.get("output_formats") or service.get("outputFormats") or []
            normalized_formats: list[str] = []
            if isinstance(formats, list):
                for fmt in formats:
                    if isinstance(fmt, str):
                        normalized_formats.append(fmt)
                    elif isinstance(fmt, dict):
                        value = fmt.get("mimeType") or fmt.get("mime_type") or fmt.get("name")
                        if isinstance(value, str):
                            normalized_formats.append(value)
            services.append({
                "name": service.get("name") or service.get("id") or service.get("serviceId"),
                "url": service.get("referenceUrl") or service.get("href") or service.get("url"),
                "variable_subset": subset.get("variable", caps.get("variableSubset", caps.get("variable_subset"))),
                "bbox_subset": subset.get("bbox", caps.get("bboxSubset", caps.get("bbox_subset"))),
                "shape_subset": subset.get("shape", caps.get("shapeSubset", caps.get("shape_subset"))),
                "temporal_subset": subset.get("temporal", caps.get("temporalSubset", caps.get("temporal_subset"))),
                "output_formats": normalized_formats,
            })
    layer_map = {role: _resolve_layer(variables, role, pol) for role in LAYER_ALIASES}
    capability_flags = _service_capabilities(payload)
    supports_tiff = any(
        "image/tiff" in str(fmt).lower() or str(fmt).lower() in {"geotiff", "tiff"}
        for service in services
        for fmt in (service.get("output_formats") or [])
    )
    # Harmony v3 summary and v1/v2 root-level formats both occur in practice.
    format_nodes: list[Any] = [payload.get("outputFormats")]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    format_nodes.extend([summary.get("outputFormats"), summary.get("output_formats")])
    for node in format_nodes:
        if not isinstance(node, list):
            continue
        for fmt in node:
            value = fmt if isinstance(fmt, str) else (fmt.get("mimeType") or fmt.get("name") or "") if isinstance(fmt, dict) else ""
            supports_tiff = supports_tiff or "image/tiff" in str(value).lower() or str(value).lower() in {"geotiff", "tiff"}
    layer_ready = layer_map["unwrapped_phase"] is not None and layer_map["coherence"] is not None
    return {
        "service": "NASA Harmony capability lookup",
        "mode": "capabilities-only",
        "data_downloaded": False,
        "scientific_analysis_performed": False,
        "collection": {
            "short_name_requested": short_name,
            "maturity_requested": maturity.upper(),
            "granule_processing_type": production.upper(),
            "concept_id": payload.get("conceptId") or payload.get("collectionId"),
            "short_name_returned": payload.get("shortName") or payload.get("short_name"),
        },
        "capabilities": capability_flags,
        "supports_geotiff_output": supports_tiff,
        "polarization_selected": pol,
        "scientific_layers": layer_map,
        "minimum_gunw_pilot_ready": bool(layer_ready and capability_flags["variable_subset"] and supports_tiff),
        "variables_count": len(variables),
        "variables": variables[:200],
        "variables_truncated": len(variables) > 200,
        "services": services,
        "warnings": [
            "This is a live capability lookup only; it does not start an extraction job.",
            "AOI subsetting must not be assumed unless shape_subset or bbox_subset is explicitly true; this collection may require full-granule layer output followed by a local crop.",
            f"Scientific layers are resolved for the explicit {pol} polarization; confirm scientific suitability before comparing pairs.",
        ],
    }


def build_extraction_plan(
    *, granule_name: str, product_type: str, production: str,
    aoi_area_km2: float, coordinates: list[tuple[float, float]],
    maturity: str = "PROVISIONAL", polarization: str = "HH",
) -> dict[str, Any]:
    capability = get_harmony_capabilities(product_type, production, maturity, polarization)
    layers = capability["scientific_layers"]
    required = ["unwrapped_phase", "coherence"]
    missing = [role for role in required if not layers.get(role)]
    selected = [
        {"role": role, "variable": layers[role], "required": role in required}
        for role in LAYER_ALIASES
        if layers.get(role)
    ]
    spatial = capability["capabilities"]
    spatial_mode = "AOI-subset-confirmed" if spatial["shape_subset"] or spatial["bbox_subset"] else "full-granule-layer-extraction"
    return {
        "mode": "single-granule-extraction-plan",
        "data_downloaded": False,
        "scientific_analysis_performed": False,
        "granule_name": granule_name,
        "product_type": product_type,
        "production": production,
        "collection_maturity": maturity.upper(),
        "polarization": polarization.upper(),
        "aoi_area_km2": round(aoi_area_km2, 3),
        "collection": capability["collection"],
        "ready_for_extraction": not missing and capability["capabilities"]["variable_subset"] and capability["supports_geotiff_output"],
        "missing_required_layers": missing,
        "selected_layers": selected,
        "selected_layer_count": len(selected),
        "one_granule_only": True,
        "download_strategy": "Request one granule and only the selected 2D layers; do not download the source HDF5 product by default.",
        "spatial_strategy": spatial_mode,
        "full_granule_outputs_may_still_be_large": spatial_mode == "full-granule-layer-extraction",
        "aoi_coordinates_received": len(coordinates),
        "original_scientific_engine": {
            "reader": "Backend/src/gunw_reader.py",
            "integration_status": "not-executed-yet",
            "note": "Harmony returns individual GeoTIFF layers; an adapter must window/crop those layers and preserve their georeferencing before invoking the original HDF5-oriented reader.",
        },
        "next_stage": (
            "Ready for a guarded one-granule extraction pilot. Configure EARTHDATA_TOKEN as a Backend secret only after reviewing the exact selected layers and spatial-subsetting support."
            if not missing and capability["capabilities"]["variable_subset"] and capability["supports_geotiff_output"]
            else "Do not extract yet. Inspect the returned capability variables/services and resolve missing or ambiguous required layer names first."
        ),
        "capability_snapshot": {
            "variable_subset": capability["capabilities"]["variable_subset"],
            "bbox_subset": capability["capabilities"]["bbox_subset"],
            "shape_subset": capability["capabilities"]["shape_subset"],
            "supports_geotiff_output": capability["supports_geotiff_output"],
            "minimum_gunw_pilot_ready": capability["minimum_gunw_pilot_ready"],
            "polarization": polarization.upper(),
        },
        "warnings": capability["warnings"],
    }
