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
