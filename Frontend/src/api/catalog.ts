import type { Coordinate } from '../../landslide/engine/types';

export type NisarProductType = 'GUNW' | 'GOFF';

export type CatalogProduct = {
  granule_name: string;
  product_type: string;
  observation_dates: string[];
  start_time?: string | null;
  stop_time?: string | null;
  direction?: string | null;
  path?: string | number | null;
  size_mb?: number | null;
  processing_date?: string | null;
  processing_level?: string | null;
};

export type CatalogResponse = {
  mode: 'catalog-preflight-only';
  checked_at: string;
  data_downloaded: false;
  scientific_analysis_performed: false;
  query: {
    platform: string;
    product_type: NisarProductType;
    start_date: string;
    end_date: string;
    aoi_area_km2: number;
  };
  product_count: number;
  truncated: boolean;
  products_with_unknown_size: number;
  estimated_full_granule_volume_gib: number;
  estimated_full_granule_volume_is_complete: boolean;
  products: CatalogProduct[];
  next_step: string;
  warnings: string[];
};

const configuredApiUrl = (import.meta.env.VITE_SARGUARDIAN_API_URL as string | undefined)?.trim();
const API_BASE = (configuredApiUrl || 'http://localhost:8000').replace(/\/+$/, '');

export async function searchNisarCatalog(input: {
  coordinates: Coordinate[];
  startDate: string;
  endDate: string;
  productType: NisarProductType;
}): Promise<CatalogResponse> {
  const response = await fetch(`${API_BASE}/api/catalog/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      coordinates: input.coordinates,
      start_date: input.startDate,
      end_date: input.endDate,
      product_type: input.productType,
    }),
  });

  const payload = await response.json().catch(() => ({})) as { detail?: string };
  if (!response.ok) {
    throw new Error(payload.detail || `Catalogue request failed (${response.status}).`);
  }
  return payload as CatalogResponse;
}


export type AoiWindowResponse = {
  service: string;
  job_id: string;
  job_status: string;
  result_filename?: string;
  aoi_cropped: boolean;
  crop_representation?: string;
  result_file_downloaded?: boolean;
  full_raster_downloaded?: boolean;
  raster_window_pixels_read?: boolean;
  persistent_storage_used?: boolean;
  scientific_analysis_performed?: boolean;
  scientific_displacement_analysis_performed?: boolean;
  source_driver?: string;
  source_crs?: string;
  source_width?: number;
  source_height?: number;
  source_nodata?: number | null;
  aoi_window?: { width: number; height: number; window_pixels: number };
  aoi_window_bounds_wgs84?: number[];
  pixels_inside_polygon?: number;
  valid_unmasked_pixels?: number;
  valid_fraction_of_polygon_pixels?: number;
  raw_phase_diagnostic_statistics?: { min: number; max: number; mean: number; std: number; unit: string } | null;
  aoi_window_read_from_remote_cog?: boolean;
  aoi_area_km2?: number;
  warning?: string;
  detail?: string;
  message?: string;
};

export async function inspectNisarAoiWindow(input: {
  jobId: string;
  coordinates: Coordinate[];
}): Promise<AoiWindowResponse> {
  const jobId = input.jobId.trim();
  if (!jobId || !/^[A-Za-z0-9_-]{1,128}$/.test(jobId)) {
    throw new Error('Enter the valid NASA Harmony Job ID.');
  }

  const response = await fetch(`${API_BASE}/api/harmony/jobs/${encodeURIComponent(jobId)}/crop-aoi`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ coordinates: input.coordinates }),
  });

  const payload = await response.json().catch(() => ({})) as AoiWindowResponse;
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `AOI window read failed (${response.status}).`);
  }
  return payload;
}

export type HarmonyQualityLayerJob = {
  job_id: string;
  status: string;
  variable?: string;
  job_status_url?: string;
  reused_existing_job?: boolean;
};

export type HarmonyQualityLayerBundle = {
  service: string;
  mode: string;
  granule_name: string;
  polarization: string;
  maturity: string;
  jobs: Record<string, HarmonyQualityLayerJob>;
  submission_errors: Record<string, string>;
  all_required_layers_submitted: boolean;
  aoi_area_km2?: number;
  warning?: string;
  next_step?: string;
};

export type HarmonyJobStatusResponse = {
  job_id: string;
  job?: { status?: string; progress?: number; message?: string };
};

export type GunwQualityAnalysisResponse = {
  service: string;
  mode: string;
  granule_name: string;
  polarization: string;
  aoi?: { window_width_px: number; window_height_px: number; window_pixels: number; polygon_pixels: number; crs: string };
  layers?: Record<string, { min: number; max: number; mean: number; std: number; count: number } | null>;
  layer_diagnostics?: Record<string, {
    dtype?: string | null;
    nodata_value?: number | null;
    polygon_pixels?: number;
    finite_pixels_in_polygon?: number;
    valid_data_pixels_in_polygon?: number;
    masked_or_nodata_pixels_in_polygon?: number;
    zero_value_pixels_in_polygon_including_masked?: number;
    zero_value_pixels_among_valid_data?: number;
    positive_value_pixels_among_valid_data?: number;
    negative_value_pixels_among_valid_data?: number;
    min_valid?: number | null;
    max_valid?: number | null;
    mean_valid?: number | null;
    unique_value_count?: number;
    most_common_values?: Array<{ value: number; count: number }>;
    mask_encoding?: string;
    wrs_low_byte_value_counts?: Array<{ value: number; count: number }>;
    base_wrs_land_valid_subswath_pixels?: number;
    strict_usable_pixels?: number;
    reference_input_exception_nonzero_pixels?: number;
    secondary_input_exception_nonzero_pixels?: number;
    ionosphere_filter_flag_pixels?: number;
    packed_wrs_decode_ambiguous_pixels?: number;
    wrs_decode_reliable_pixels?: number;
  }>;
  warnings?: string[];
  quality_gates?: {
    phase_nonzero_finite_pixels: number;
    coherence_valid_pixels: number;
    coherence_threshold: number;
    coherence_pass_pixels: number;
    phase_and_coherence_pixels?: number;
    connected_components_positive_pixels: number;
    after_connected_components_pixels?: number;
    ionosphere_phase_screen_valid_pixels: number;
    after_ionosphere_validity_pixels?: number;
    mask_valid_land_subswath_pixels: number;
    after_land_and_subswath_mask_pixels?: number;
    mask_low_byte_land_valid_subswath_pixels?: number;
    mask_reference_exception_pixels?: number;
    mask_secondary_exception_pixels?: number;
    mask_ionosphere_filter_flagged_pixels?: number;
    mask_packed_wrs_ambiguous_pixels?: number;
    after_ionosphere_filter_mask_pixels?: number;
    jointly_qualified_pixels: number;
    jointly_qualified_fraction_of_polygon: number;
    dominant_connected_component_id: number | null;
    dominant_component_share_among_phase_coherence_pixels: number | null;
    distinct_positive_components_in_phase_coherence_pixels: number;
  };
  ionosphere_corrected_phase_statistics?: { min: number; max: number; mean: number; std: number; count: number } | null;
  physical_displacement?: { computed: boolean; unit: string | null; reason: string };
  original_scientific_engine?: { hdf5_read_gunw_invoked: boolean; reader_compatible_core_gates_applied_to_aligned_aoi_arrays: boolean; note: string };
  detail?: string;
};

async function readApiPayload<T>(response: Response, fallback: string): Promise<T> {
  const payload = await response.json().catch(() => ({})) as T & { detail?: string; message?: string };
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `${fallback} (${response.status}).`);
  }
  return payload as T;
}

export async function startGunwQualityLayers(input: {
  granuleName: string;
  phaseJobId: string;
  coordinates: Coordinate[];
  maturity?: 'PROVISIONAL' | 'BETA';
  polarization?: 'HH' | 'VV';
}): Promise<HarmonyQualityLayerBundle> {
  const response = await fetch(`${API_BASE}/api/harmony/gunw-quality/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      granule_name: input.granuleName,
      phase_job_id: input.phaseJobId,
      coordinates: input.coordinates,
      maturity: input.maturity ?? 'PROVISIONAL',
      polarization: input.polarization ?? 'HH',
      confirm_full_granule_layers: true,
      include_iono_uncertainty: true,
    }),
  });
  return readApiPayload<HarmonyQualityLayerBundle>(response, 'Could not start GUNW quality-layer jobs');
}

export async function getNisarHarmonyJobStatus(jobId: string): Promise<HarmonyJobStatusResponse> {
  const safeId = jobId.trim();
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(safeId)) throw new Error('Invalid Harmony Job ID.');
  const response = await fetch(`${API_BASE}/api/harmony/jobs/${encodeURIComponent(safeId)}`);
  return readApiPayload<HarmonyJobStatusResponse>(response, 'Could not check Harmony job status');
}

export async function analyzeGunwAoiQuality(input: {
  granuleName: string;
  polarization?: 'HH' | 'VV';
  coordinates: Coordinate[];
  jobs: Record<string, HarmonyQualityLayerJob>;
}): Promise<GunwQualityAnalysisResponse> {
  const required = ['unwrapped_phase', 'coherence', 'connected_components', 'ionosphere_phase_screen', 'mask'];
  const missing = required.filter((role) => !input.jobs[role]?.job_id);
  if (missing.length) throw new Error(`Missing layer jobs: ${missing.join(', ')}`);
  const body: Record<string, unknown> = {
    granule_name: input.granuleName,
    polarization: input.polarization ?? 'HH',
    coordinates: input.coordinates,
    phase_job_id: input.jobs.unwrapped_phase.job_id,
    coherence_job_id: input.jobs.coherence.job_id,
    connected_components_job_id: input.jobs.connected_components.job_id,
    ionosphere_phase_screen_job_id: input.jobs.ionosphere_phase_screen.job_id,
    mask_job_id: input.jobs.mask.job_id,
    coherence_threshold: 0.3,
  };
  if (input.jobs.ionosphere_phase_screen_uncertainty?.job_id) {
    body.ionosphere_phase_screen_uncertainty_job_id = input.jobs.ionosphere_phase_screen_uncertainty.job_id;
  }
  const response = await fetch(`${API_BASE}/api/harmony/gunw-quality/analyze-aoi`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readApiPayload<GunwQualityAnalysisResponse>(response, 'Could not analyze the selected GUNW AOI');
}
