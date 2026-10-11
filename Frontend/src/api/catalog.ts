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
