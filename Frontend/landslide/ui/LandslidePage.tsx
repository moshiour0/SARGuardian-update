import { useCallback, useEffect, useRef, useState } from 'react';
import { importLibrary, setOptions } from '@googlemaps/js-api-loader';
import type { User } from 'firebase/auth';
import { inspectNisarAoiWindow, searchNisarCatalog } from '../../src/api/catalog';
import type { AoiWindowResponse, CatalogResponse, NisarProductType } from '../../src/api/catalog';
import { investigate } from '../engine/adapter';
import { calculateGeodesicAreaKm2, timestampToRequest, validateAnalysisRequest, validatePolyline } from '../engine/validation';
import type { AnalysisRequest, Coordinate, InvestigationPolyline, InvestigationResult, LocationSelection, TimestampSelection } from '../engine/types';
import './landslide.css';

let mapsLoaderConfigured = false;
const MIN_MAP_ZOOM = 5;
const DEFAULT_MAP_ZOOM = 5;

type MapCanvasProps = {
  location: LocationSelection | null;
  coordinates: Coordinate[];
  drawing: boolean;
  selectedVertexIndex: number | null;
  onLocation: (location: LocationSelection) => void;
  onCoordinates: (coordinates: Coordinate[], isClosed: boolean) => void;
  onSelectedVertex: (index: number | null) => void;
  onSearchText: (value: string) => void;
  onMapReady: (map: google.maps.Map) => void;
};

function MapCanvas({ location, coordinates, drawing, selectedVertexIndex, onLocation, onCoordinates, onSelectedVertex, onSearchText, onMapReady }: MapCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const polygonRef = useRef<google.maps.Polygon | null>(null);
  const markerRef = useRef<google.maps.Marker | null>(null);
  const markersRef = useRef<google.maps.Marker[]>([]);
  const drawingRef = useRef(drawing);
  const coordinatesRef = useRef(coordinates);
  const onCoordinatesRef = useRef(onCoordinates);
  const onLocationRef = useRef(onLocation);
  const onSelectedVertexRef = useRef(onSelectedVertex);
  const [mapError, setMapError] = useState('');
  const [placesError, setPlacesError] = useState('');

  drawingRef.current = drawing;
  coordinatesRef.current = coordinates;
  onCoordinatesRef.current = onCoordinates;
  onLocationRef.current = onLocation;
  onSelectedVertexRef.current = onSelectedVertex;

  useEffect(() => {
    let active = true;
    const apiKey = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;

    if (!apiKey) {
      setMapError('Add VITE_GOOGLE_MAPS_API_KEY to .env to enable satellite imagery.');
      return undefined;
    }

    if (!mapsLoaderConfigured) {
      setOptions({ key: apiKey, v: 'weekly' });
      mapsLoaderConfigured = true;
    }

    void Promise.all([importLibrary('maps'), importLibrary('geometry')])
      .then(async ([maps]) => {
        if (!active || !containerRef.current) return;
        const map = new maps.Map(containerRef.current, {
          center: { lat: 22, lng: 82 },
          zoom: DEFAULT_MAP_ZOOM,
          minZoom: MIN_MAP_ZOOM,
          maxZoom: 18,
          mapTypeId: 'satellite',
          fullscreenControl: false,
          streetViewControl: false,
          mapTypeControl: false,
          zoomControl: false,
          clickableIcons: false,
          backgroundColor: '#dfeff3',
        });

        map.addListener('click', (event: google.maps.MapMouseEvent) => {
          if (!drawingRef.current || !event.latLng) return;
          const next: Coordinate = [Number(event.latLng.lng().toFixed(6)), Number(event.latLng.lat().toFixed(6))];
          const existing = coordinatesRef.current.map((point) => point);
          if (existing.length >= 2) {
            const first = existing[0];
            const distanceKm = Math.hypot(next[0] - first[0], next[1] - first[1]) * 111;
            if (distanceKm < 0.1) {
              onCoordinatesRef.current(existing, true);
              onSelectedVertexRef.current(0);
              return;
            }
          }
          onCoordinatesRef.current([...existing, next], false);
          onSelectedVertexRef.current([...existing, next].length - 1);
        });

        mapRef.current = map;
        onMapReady(map);

        try {
          const places = await importLibrary('places');
          const searchContainer = document.querySelector<HTMLElement>('#landslide-place-search');
          if (!searchContainer) return;

          type PlacePrediction = {
            toPlace: () => Promise<{ fetchFields: (options: { fields: string[] }) => Promise<void>; displayName?: string; formattedAddress?: string; location?: google.maps.LatLng }>;
          };
          type PlacesLibrary = typeof places & {
            PlaceAutocompleteElement?: new (options: { includedPrimaryTypes: string[] }) => HTMLElement & {
              addEventListener: (type: 'gmp-select', listener: (event: Event) => void) => void;
            };
          };
          const PlaceAutocompleteElement = (places as PlacesLibrary).PlaceAutocompleteElement;
          if (!PlaceAutocompleteElement) {
            setPlacesError('Places search is unavailable. Enable Places API (New) for this key.');
            return;
          }

          const autocomplete = new PlaceAutocompleteElement({
            includedPrimaryTypes: ['administrative_area_level_1', 'administrative_area_level_2', 'country', 'locality', 'natural_feature', 'establishment'],
          });
          autocomplete.classList.add('place-autocomplete-element');
          searchContainer.innerHTML = '';
          searchContainer.appendChild(autocomplete);
          autocomplete.addEventListener('input', () => onSearchText(autocomplete.value));
          autocomplete.addEventListener('gmp-select', (event: Event) => {
            void (async () => {
              const prediction = (event as CustomEvent<{ placePrediction?: PlacePrediction }>).detail?.placePrediction;
              if (!prediction) return;
              const place = await prediction.toPlace();
              await place.fetchFields({ fields: ['displayName', 'formattedAddress', 'location'] });
              if (!place.location) return;
              const displayName = place.formattedAddress || place.displayName || 'Selected place';
              onLocationRef.current({
                searchText: displayName,
                name: displayName,
                latitude: place.location.lat(),
                longitude: place.location.lng(),
              });
            })().catch(() => setPlacesError('The selected place could not be loaded. Check Places API (New) access for this key.'));
          });
        } catch {
          setPlacesError('Places search is unavailable. Enable Places API (New) and billing for this key.');
        }
      })
      .catch(() => setMapError('Google Maps could not load. Check the API key and Maps JavaScript API access.'));

    return () => { active = false; };
  }, [onMapReady, onSearchText]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const path = coordinates.map(([lng, lat]) => ({ lat, lng }));

    if (polygonRef.current) {
      polygonRef.current.setPaths(path);
      polygonRef.current.setVisible(path.length > 2);
    } else if (path.length > 2) {
      polygonRef.current = new google.maps.Polygon({
        map,
        paths: path,
        strokeColor: '#9ed8ff',
        fillColor: '#75c9f5',
        fillOpacity: 0.18,
        strokeOpacity: 0.95,
        strokeWeight: 2,
        clickable: false,
      });
    }

    if (path.length <= 2 && polygonRef.current) {
      polygonRef.current.setVisible(false);
    }

    const nextMarkers: google.maps.Marker[] = [];
    for (let index = 0; index < coordinates.length; index += 1) {
      const coordinate = coordinates[index];
      const existing = markersRef.current[index];
      const marker = existing ?? new google.maps.Marker({
        map,
        position: { lat: coordinate[1], lng: coordinate[0] },
        draggable: true,
        clickable: true,
        zIndex: index === 0 ? 30 : 25,
      });

      marker.setPosition({ lat: coordinate[1], lng: coordinate[0] });
      marker.setMap(map);
      marker.setDraggable(true);
      marker.setVisible(true);
      marker.setZIndex(index === 0 ? 30 : 25);
      marker.setIcon({
        path: google.maps.SymbolPath.CIRCLE,
        scale: selectedVertexIndex === index ? 7 : index === 0 ? 6.5 : 5.8,
        fillColor: index === 0 && drawing ? '#d4f5ff' : '#f3fcff',
        fillOpacity: 1,
        strokeColor: index === 0 && drawing ? '#9ae5ff' : '#d8f2ff',
        strokeWeight: selectedVertexIndex === index ? 2.5 : 1.8,
      });

      marker.addListener('click', () => {
        onSelectedVertexRef.current(index);
        if (index === 0 && coordinatesRef.current.length >= 3) {
          onCoordinatesRef.current(coordinatesRef.current, true);
          onSelectedVertexRef.current(0);
        }
      });

      marker.addListener('dragend', (event: google.maps.MapMouseEvent) => {
        if (!event.latLng) return;
        const updated = coordinatesRef.current.map((point) => point);
        const next: Coordinate = [Number(event.latLng.lng().toFixed(6)), Number(event.latLng.lat().toFixed(6))];
        updated[index] = next;
        onCoordinatesRef.current(updated, coordinatesRef.current.length >= 3 && index === 0);
        onSelectedVertexRef.current(index);
      });

      nextMarkers.push(marker);
    }

    for (let index = markersRef.current.length - 1; index >= coordinates.length; index -= 1) {
      markersRef.current[index].setMap(null);
    }

    markersRef.current = nextMarkers;
  }, [coordinates, drawing, selectedVertexIndex]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !location) return;
    const position = { lat: location.latitude, lng: location.longitude };
    map.panTo(position);
    map.setZoom(Math.max(7, map.getZoom() ?? 7));
    markerRef.current?.setMap(null);
    markerRef.current = new google.maps.Marker({
      map,
      position,
      title: location.name,
      animation: google.maps.Animation.DROP,
    });
  }, [location]);

  return (
    <div className="map-canvas-wrap">
      <div ref={containerRef} className="google-map" aria-label="Google satellite map" />
      {mapError && (
        <div className="map-unavailable">
          <span>MAPS SETUP</span>
          <strong>Satellite imagery is not connected.</strong>
          <small>{mapError}</small>
        </div>
      )}
      {placesError && (
        <div className="places-unavailable" role="status">{placesError}</div>
      )}
      {drawing && !mapError && (
        <div className="draw-hint">Click to trace the area and snap back to the first vertex to close it.</div>
      )}
    </div>
  );
}

function Icon({ name }: { name: 'plus' | 'minus' | 'locate' | 'layers' | 'fullscreen' | 'search' | 'draw' | 'undo' | 'clear' | 'area' | 'home' }) {
  const paths = {
    plus: 'M12 5v14M5 12h14',
    minus: 'M5 12h14',
    locate: 'M12 2v4m0 12v4M2 12h4m12 0h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z',
    layers: 'm12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5',
    fullscreen: 'M8 3H3v5m13-5h5v5M8 21H3v-5m18 0v5h-5',
    search: 'm21 21-4.3-4.3m2.3-5.2a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z',
    draw: 'm4 20 4.5-1L19 8.5a2.1 2.1 0 0 0-3-3L5.5 16 4 20Zm10-13 3 3',
    undo: 'M9 7 4 12l5 5M4 12h10a6 6 0 0 1 6 6',
    clear: 'M6 6l12 12M18 6 6 18',
    area: 'M5 18l7-7 6 4 6-10v17H5z',
    home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1v-9.5Z',
  } as const;

  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={paths[name]} />
    </svg>
  );
}

function ResultPanel({ result, onReset }: { result: InvestigationResult; onReset: () => void }) {
  const title = result.status === 'finding'
    ? 'Evidence available'
    : result.qa.status === 'FAILED'
      ? 'Failed'
      : result.observability.geometryStatus === 'insufficient'
        ? 'Insufficient evidence'
        : 'No matching evidence';

  return (
    <div className="evidence-panel">
      <div className="evidence-top">
        <div>
          <span className="result-kicker">LANDSLIDE INVESTIGATION</span>
          <h2>{title}</h2>
        </div>
        <button type="button" className="close-result" onClick={onReset} aria-label="Close result">×</button>
      </div>
      <p className="reference-demo-notice">Reference/demo result only: the current engine uses precomputed fixture data. It did not fetch or process new NISAR products for this request.</p>
      <div className="evidence-grid">
        <div><span>Location</span><strong>{result.request.location.name}</strong></div>
        <div><span>Timestamp</span><strong>{typeof result.request.timestamp === 'string' ? result.request.timestamp.slice(0, 10) : `${result.request.timestamp.start.slice(0, 10)} — ${result.request.timestamp.end.slice(0, 10)}`}</strong></div>
        <div><span>Geometry</span><strong>{result.request.polyline.type}</strong></div>
        <div><span>Observation</span><strong>{result.observability.geometryStatus}</strong></div>
        {result.measurement && <div><span>Measurement</span><strong className="measurement-value">{result.measurement.value} {result.measurement.unit}</strong></div>}
        <div><span>Uncertainty</span><strong>{result.uncertainty ? `${result.uncertainty.value} ${result.uncertainty.unit}` : 'Not reported'}</strong></div>
        <div><span>Observability</span><strong>{result.observability.note}</strong></div>
        <div><span>QA</span><strong>{result.qa.status}</strong></div>
        <div><span>Provenance</span><strong>{result.provenance.algorithm}</strong><small>{result.provenance.notes}</small></div>
      </div>
      <div className="evidence-note">{result.measurement?.interpretation ?? result.limitations.join(' ')}<b>Investigation AOI · {result.request.polyline.coordinates.length} vertices · EPSG:4326</b></div>
    </div>
  );
}


function formatGib(value: number): string {
  if (value >= 1) return `${value.toFixed(2)} GiB`;
  return `${(value * 1024).toFixed(0)} MiB`;
}

function CatalogPreflightPanel({ result, onClose }: { result: CatalogResponse; onClose: () => void }) {
  return (
    <div className="catalog-preflight" aria-live="polite">
      <div className="catalog-preflight-heading">
        <div>
          <span className="result-kicker">ASF CATALOGUE PREFLIGHT · METADATA ONLY</span>
          <h3>{result.product_count} matching {result.query.product_type} granule{result.product_count === 1 ? '' : 's'}</h3>
        </div>
        <button className="close-result" type="button" onClick={onClose} aria-label="Close catalogue results">×</button>
      </div>
      <div className="catalog-stats">
        <div><span>AOI</span><strong>{result.query.aoi_area_km2.toFixed(2)} km²</strong></div>
        <div><span>Full-granule size estimate</span><strong>{formatGib(result.estimated_full_granule_volume_gib)}</strong></div>
        <div><span>Checked (UTC)</span><strong>{new Date(result.checked_at).toLocaleString()}</strong></div>
      </div>
      <p className="catalog-notice"><b>No satellite files were downloaded.</b> This is a metadata search, not a movement analysis. The volume shown sums complete products from ASF; the next stage must request only the required variable(s) via NASA Harmony and process the AOI one product at a time.</p>
      {result.truncated && <p className="catalog-warning">More than the configured result limit may match. Narrow the location or date range before analysis.</p>}
      {result.products_with_unknown_size > 0 && <p className="catalog-warning">Size metadata is missing for {result.products_with_unknown_size} product(s), so the total is incomplete.</p>}
      {result.product_count === 0 ? (
        <p className="catalog-empty">No matching products were returned for this area, time range and product family. Try another date range or switch between GUNW and GOFF.</p>
      ) : (
        <div className="catalog-product-list">
          {result.products.slice(0, 12).map((product, index) => (
            <div className="catalog-product-row" key={`${product.granule_name}-${index}`}>
              <div className="catalog-product-main">
                <strong title={product.granule_name}>{product.granule_name}</strong>
                <small>{product.observation_dates.join(' → ') || product.start_time || 'Acquisition date not parsed'} · {product.direction || 'Direction unknown'}{product.path ? ` · path ${product.path}` : ''}</small>
              </div>
              <span>{product.size_mb == null ? 'Size n/a' : formatGib(product.size_mb / 1024)}</span>
            </div>
          ))}
          {result.products.length > 12 && <p className="catalog-empty">Showing the first 12 of {result.products.length} returned granules.</p>}
        </div>
      )}
      <details className="catalog-disclaimers">
        <summary>Important scientific and storage notes</summary>
        {result.warnings.map((warning) => <p key={warning}>{warning}</p>)}
      </details>
    </div>
  );
}

function AoiWindowPanel({
  coordinates,
  areaClosed,
  jobId,
  onJobIdChange,
  isLoading,
  error,
  result,
  onRun,
}: {
  coordinates: Coordinate[];
  areaClosed: boolean;
  jobId: string;
  onJobIdChange: (value: string) => void;
  isLoading: boolean;
  error: string;
  result: AoiWindowResponse | null;
  onRun: () => void;
}) {
  return (
    <div className="aoi-window-panel" aria-live="polite">
      <div className="catalog-preflight-heading">
        <div>
          <span className="result-kicker">REMOTE COG · AOI WINDOW PILOT</span>
          <h3>Read the selected area from a completed GUNW result</h3>
        </div>
      </div>
      <p className="catalog-notice">This reads only the pixels intersecting your polygon from the remote Cloud-Optimized GeoTIFF where HTTP byte-range access works. It does not save the TIFF or calculate landslide displacement.</p>
      <label className="aoi-job-label">
        <span>Harmony Job ID</span>
        <input
          value={jobId}
          onChange={(event) => onJobIdChange(event.target.value)}
          placeholder="Paste the successful Harmony job ID"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <button className="investigate-button aoi-window-button" type="button" disabled={!areaClosed || coordinates.length < 3 || !jobId.trim() || isLoading} onClick={onRun}>
        {isLoading ? 'Reading AOI window…' : 'Read selected AOI window'}
      </button>
      {!areaClosed && <p className="action-hint">Close the polygon first to send its coordinates.</p>}
      {error && <p className="catalog-error" role="alert">{error}</p>}
      {result && (
        <div className="aoi-window-result">
          <strong>AOI window returned</strong>
          <div className="catalog-stats">
            <div><span>Window</span><strong>{result.aoi_window ? `${result.aoi_window.width} × ${result.aoi_window.height} px` : 'Not available'}</strong></div>
            <div><span>Polygon pixels</span><strong>{result.pixels_inside_polygon ?? '—'}</strong></div>
            <div><span>Valid phase pixels</span><strong>{result.valid_unmasked_pixels ?? '—'}</strong></div>
          </div>
          {result.raw_phase_diagnostic_statistics && (
            <div className="catalog-stats">
              <div><span>Raw phase min</span><strong>{result.raw_phase_diagnostic_statistics.min.toFixed(4)}</strong></div>
              <div><span>Raw phase max</span><strong>{result.raw_phase_diagnostic_statistics.max.toFixed(4)}</strong></div>
              <div><span>Raw phase mean</span><strong>{result.raw_phase_diagnostic_statistics.mean.toFixed(4)}</strong></div>
            </div>
          )}
          <p className="catalog-warning">{result.warning || 'Raw phase diagnostics only; this is not a displacement or landslide finding.'}</p>
          <small className="aoi-window-meta">CRS: {result.source_crs || 'unknown'} · Persistent storage: {result.persistent_storage_used ? 'used' : 'not used'} · Full raster downloaded: {result.full_raster_downloaded ? 'yes' : 'no'}</small>
        </div>
      )}
    </div>
  );
}

export function LandslidePage({ user, onHome, onSignOut }: { user: User; onHome: () => void; onSignOut: () => void }) {
  const [searchText, setSearchText] = useState('');
  const [location, setLocation] = useState<LocationSelection | null>(null);
  const [searchState, setSearchState] = useState('');
  const [coordinates, setCoordinates] = useState<Coordinate[]>([]);
  const [areaClosed, setAreaClosed] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [selectedVertexIndex, setSelectedVertexIndex] = useState<number | null>(null);
  const [windowStart, setWindowStart] = useState('');
  const [windowEnd, setWindowEnd] = useState('');
  const [result, setResult] = useState<InvestigationResult | null>(null);
  const [error, setError] = useState('');
  const [layerOpen, setLayerOpen] = useState(false);
  const [layerMode, setLayerMode] = useState<'satellite' | 'hybrid' | 'terrain'>('satellite');
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [productType, setProductType] = useState<NisarProductType>('GUNW');
  const [catalogResult, setCatalogResult] = useState<CatalogResponse | null>(null);
  const [isCatalogLoading, setIsCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [harmonyJobId, setHarmonyJobId] = useState('');
  const [aoiWindowResult, setAoiWindowResult] = useState<AoiWindowResponse | null>(null);
  const [isAoiWindowLoading, setIsAoiWindowLoading] = useState(false);
  const [aoiWindowError, setAoiWindowError] = useState('');
  const mapRef = useRef<google.maps.Map | null>(null);

  useEffect(() => {
    const handleDocumentPointerDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;

      if (!target.closest('.layers-menu') && !target.closest('[aria-label="Layers"]')) {
        setLayerOpen(false);
      }
      if (!target.closest('.profile-inline')) {
        setProfileMenuOpen(false);
      }
    };

    document.addEventListener('mousedown', handleDocumentPointerDown);
    return () => document.removeEventListener('mousedown', handleDocumentPointerDown);
  }, []);

  const today = new Date().toISOString().slice(0, 10);
  const timestamp: TimestampSelection = { mode: 'window', start: windowStart, end: windowEnd };
  const polygon: InvestigationPolyline = { type: 'LineString', coordinates: areaClosed && coordinates.length > 2 ? [...coordinates, coordinates[0]] : coordinates, crs: 'EPSG:4326' };
  const windowDays = windowStart && windowEnd ? Math.ceil((Date.parse(`${windowEnd}T00:00:00Z`) - Date.parse(`${windowStart}T00:00:00Z`)) / 86400000) : 0;
  const polygonAreaKm2 = coordinates.length > 2 ? calculateGeodesicAreaKm2(coordinates) : 0;
  // The AOI polygon itself is sufficient for catalogue search. Place search is optional;
  // if no named place was selected, the reference demo can use the polygon centre.
  const ready = Boolean(areaClosed && coordinates.length >= 3 && windowStart && windowEnd && windowEnd >= windowStart && windowEnd <= today);
  const geometryStatus = !coordinates.length
    ? (location ? 'Draw an area to investigate.' : 'Search a place or draw an area to begin.')
    : !areaClosed ? 'Add at least 3 vertices, then close the area.'
      : !windowStart || !windowEnd ? 'Choose an observation time.'
        : windowEnd < windowStart ? 'End date must be on or after the start date.'
          : windowEnd > today ? 'End date cannot be in the future.'
            : ready ? (location ? 'Area and time window ready.' : 'Area ready — using its centre point.')
              : 'Complete the required fields.';
  const catalogEligible = ready && windowDays >= 0 && windowDays <= 90 && polygonAreaKm2 > 0.001 && polygonAreaKm2 <= 250;
  const catalogDisabledReason = !ready
    ? geometryStatus
    : windowDays > 90 ? 'Archive search is limited to 90 days; shorten the time window.'
      : polygonAreaKm2 <= 0.001 ? 'Draw a larger, valid polygon with at least 3 vertices.'
        : polygonAreaKm2 > 250 ? `Selected area is ${polygonAreaKm2.toFixed(1)} km². Archive search is limited to 250 km²; reduce the polygon.`
          : '';

  const handleMapReady = useCallback((map: google.maps.Map) => { mapRef.current = map; }, []);

  const updateLocation = (next: LocationSelection) => {
    setLocation(next);
    setSearchText(next.name);
    setSearchState('Location selected.');
    setResult(null);
    setCatalogResult(null);
    setCatalogError('');
    setAoiWindowResult(null);
    setAoiWindowError('');
    setError('');
  };

  const handleSearch = async (event: React.FormEvent) => {
    event.preventDefault();
    const query = searchText.trim();
    if (!query) return;

    const numeric = query.split(/[ ,]+/).map(Number);
    if (numeric.length === 2 && numeric.every(Number.isFinite) && Math.abs(numeric[0]) <= 90 && Math.abs(numeric[1]) <= 180) {
      updateLocation({ searchText: query, name: `${numeric[0].toFixed(4)}, ${numeric[1].toFixed(4)}`, latitude: numeric[0], longitude: numeric[1] });
      return;
    }

    setSearchState('Searching...');
    try {
      const response = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=${encodeURIComponent(query)}`);
      const matches = await response.json() as Array<{ display_name: string; lat: string; lon: string }>;
      if (!matches.length) throw new Error('no matches');
      const match = matches[0];
      updateLocation({
        searchText: query,
        name: match.display_name.split(',').slice(0, 2).join(','),
        latitude: Number(match.lat),
        longitude: Number(match.lon),
      });
    } catch {
      setSearchState('No location found.');
    }
  };

  const handleWindowStartChange = (value: string) => {
    setWindowStart(value);
    if (windowEnd && value > windowEnd) setWindowEnd('');
    setResult(null);
    setCatalogResult(null);
    setCatalogError('');
    setAoiWindowResult(null);
    setAoiWindowError('');
  };

  const handleWindowEndChange = (value: string) => {
    setWindowEnd(value);
    setResult(null);
    setCatalogResult(null);
    setCatalogError('');
    setAoiWindowResult(null);
    setAoiWindowError('');
    if (windowStart && value < windowStart) setError('Ending date cannot be before starting date.');
    else if (value > today) setError('Ending date cannot be after today.');
    else setError('');
  };

  const handleCoordinates = (next: Coordinate[], closed: boolean) => {
    setCoordinates(next);
    setAreaClosed(closed);
    setSelectedVertexIndex(closed ? 0 : next.length ? next.length - 1 : null);
    setResult(null);
    setCatalogResult(null);
    setCatalogError('');
    setAoiWindowResult(null);
    setAoiWindowError('');
    setError('');
  };

  const beginGeometry = () => {
    setDrawing(true);
    setCoordinates([]);
    setAreaClosed(false);
    setSelectedVertexIndex(null);
    setResult(null);
    setCatalogResult(null);
    setCatalogError('');
    setAoiWindowResult(null);
    setAoiWindowError('');
    setError('');
  };

  const undo = () => {
    setCoordinates((current) => {
      const next = current.slice(0, -1);
      setSelectedVertexIndex(next.length ? next.length - 1 : null);
      return next;
    });
    setAreaClosed(false);
    setResult(null);
    setCatalogResult(null);
    setAoiWindowResult(null);
    setAoiWindowError('');
  };

  const clearGeometry = () => {
    setCoordinates([]);
    setAreaClosed(false);
    setDrawing(false);
    setSelectedVertexIndex(null);
    setResult(null);
    setCatalogResult(null);
    setAoiWindowResult(null);
    setAoiWindowError('');
    setError('');
  };

  const completeGeometry = () => {
    const valid = validatePolyline({ type: 'LineString', coordinates: coordinates.length >= 3 ? [...coordinates, coordinates[0]] : coordinates, crs: 'EPSG:4326' }).valid;
    if (!valid) {
      setError('Select at least three valid vertices to close the AOI.');
      return;
    }
    setDrawing(false);
    setAreaClosed(true);
    setSelectedVertexIndex(0);
    setError('');
  };

  const runCatalogPreflight = async () => {
    if (!location) {
      setCatalogError('Search and select a location before checking the archive.');
      return;
    }
    if (!areaClosed || coordinates.length < 3) {
      setCatalogError('Close the area polygon before checking the archive.');
      return;
    }
    if (!windowStart || !windowEnd || windowEnd < windowStart || windowEnd > today) {
      setCatalogError('Choose a valid past date range.');
      return;
    }
    if (windowDays > 90) {
      setCatalogError('For this free demo, choose a time window of 90 days or less.');
      return;
    }
    if (polygonAreaKm2 > 250) {
      setCatalogError('For this free demo, reduce the selected area to 250 km² or less.');
      return;
    }

    setCatalogError('');
    setResult(null);
    setAoiWindowResult(null);
    setAoiWindowError('');
    setIsCatalogLoading(true);
    setCatalogResult(null);
    try {
      const response = await searchNisarCatalog({
        coordinates: [...coordinates, coordinates[0]],
        startDate: windowStart,
        endDate: windowEnd,
        productType,
      });
      setCatalogResult(response);
    } catch (error) {
      setCatalogError(error instanceof Error ? error.message : 'Could not check the NISAR catalogue.');
    } finally {
      setIsCatalogLoading(false);
    }
  };

  const runAoiWindowRead = async () => {
    if (!areaClosed || coordinates.length < 3) {
      setAoiWindowError('Close the polygon before reading its raster window.');
      return;
    }
    setAoiWindowError('');
    setAoiWindowResult(null);
    setIsAoiWindowLoading(true);
    try {
      const response = await inspectNisarAoiWindow({
        jobId: harmonyJobId,
        coordinates: [...coordinates, coordinates[0]],
      });
      setAoiWindowResult(response);
    } catch (error) {
      setAoiWindowError(error instanceof Error ? error.message : 'Could not read the selected AOI window.');
    } finally {
      setIsAoiWindowLoading(false);
    }
  };

  const runInvestigation = () => {
    if (!areaClosed || coordinates.length < 3) {
      setError('Draw at least three vertices and press Close area before investigating.');
      return;
    }

    // A polygon is a complete spatial selection by itself. Use its centre only as
    // contextual metadata when the user did not choose a named place in search.
    const analysisLocation: LocationSelection = location ?? {
      searchText: 'Selected area centre',
      name: 'Selected area centre',
      latitude: coordinates.reduce((sum, point) => sum + point[1], 0) / coordinates.length,
      longitude: coordinates.reduce((sum, point) => sum + point[0], 0) / coordinates.length,
    };
    const request: AnalysisRequest = { location: analysisLocation, polyline: polygon, timestamp: timestampToRequest(timestamp) };
    const validation = validateAnalysisRequest(request);
    if (!validation.valid) {
      setError(validation.errors.join(' '));
      return;
    }

    setError('');
    setCatalogResult(null);
    setCatalogError('');
    setIsProcessing(true);
    window.setTimeout(() => {
      setResult(investigate(request));
      setIsProcessing(false);
    }, 350);
  };

  return (
    <div className="landslide-page">
      <header className="map-header">
        <button className="map-brand" type="button" onClick={onHome}>
          <span className="brand-orbit" />
          <span>SAR</span><span>GUARDIAN</span>
        </button>
        <div className="map-title">
          <span>LANDSLIDE</span>
          <small>Investigation workspace</small>
        </div>
        <div className="map-header-actions">
          <button type="button" onClick={onHome}>Home</button>
          <div className="profile-inline" aria-label="Authenticated user profile" onClick={() => setProfileMenuOpen((current) => !current)}>
            {user.photoURL ? <img src={user.photoURL} alt="" className="profile-avatar" /> : <span className="profile-avatar profile-fallback">{(user.displayName ?? user.email ?? 'U').charAt(0).toUpperCase()}</span>}
            <span>{user.displayName ?? user.email ?? 'Authenticated user'}</span>
            {profileMenuOpen && (
              <div className="profile-popover glass-float">
                <strong>{user.displayName ?? 'Authenticated user'}</strong>
                <span>{user.email ?? 'No email available'}</span>
                <div className="signout-actions">
                  <button type="button" className="cancel-button" onClick={(event) => { event.stopPropagation(); setProfileMenuOpen(false); }}>Close</button>
                  <button type="button" className="confirm-button" onClick={(event) => { event.stopPropagation(); onSignOut(); setProfileMenuOpen(false); }}>Sign out</button>
                </div>
              </div>
            )}
          </div>
        </div>
      </header>

      <main className="map-main">
        <MapCanvas
          location={location}
          coordinates={coordinates}
          drawing={drawing}
          selectedVertexIndex={selectedVertexIndex}
          onLocation={updateLocation}
          onCoordinates={handleCoordinates}
          onSelectedVertex={setSelectedVertexIndex}
          onSearchText={setSearchText}
          onMapReady={handleMapReady}
        />

        <form className="map-search glass-float" onSubmit={handleSearch}>
          <Icon name="search" />
          <div id="landslide-place-search" className="google-place-autocomplete" aria-label="Search a place" />
          <button type="submit">Search</button>
          {location && <span className="selected-place">{location.name}</span>}
          <small>{searchState || 'Search a place to begin.'}</small>
        </form>

        <div className="map-tools glass-float" aria-label="Map controls">
          <button type="button" aria-label="Zoom in" onClick={() => mapRef.current?.setZoom((mapRef.current.getZoom() ?? 5) + 1)}><Icon name="plus" /></button>
          <button type="button" aria-label="Zoom out" onClick={() => mapRef.current?.setZoom(Math.max(MIN_MAP_ZOOM, (mapRef.current.getZoom() ?? DEFAULT_MAP_ZOOM) - 1))}><Icon name="minus" /></button>
          <button type="button" aria-label="Locate selected place" onClick={() => location && mapRef.current?.panTo({ lat: location.latitude, lng: location.longitude })}><Icon name="locate" /></button>
          <div className="layers-menu-wrap">
            <button type="button" aria-label="Layers" onClick={() => setLayerOpen((current) => !current)}><Icon name="layers" /></button>
            {layerOpen && (
              <div className="layers-menu glass-float" role="menu" aria-label="Map layers">
                {(['satellite', 'hybrid', 'terrain'] as const).map((mode) => (
                  <button key={mode} type="button" className={layerMode === mode ? 'active' : ''} onClick={() => { setLayerMode(mode); mapRef.current?.setMapTypeId(mode); setLayerOpen(false); }}>
                    {mode.charAt(0).toUpperCase() + mode.slice(1)}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button type="button" aria-label="Fullscreen map" onClick={() => document.querySelector('.map-canvas-wrap')?.requestFullscreen()}><Icon name="fullscreen" /></button>
        </div>

        {coordinates.length > 0 && (
          <div className="geometry-summary glass-float">
            <span>Area selected</span>
            <strong>{coordinates.length} vertices</strong>
            <small>{polygonAreaKm2.toFixed(2)} km²</small>
          </div>
        )}

        <section className="action-bar glass-float">
          {!result ? (
            <>
              <div className="action-copy">
                <span className="action-status"><i />{geometryStatus}</span>
                {location ? <strong>{location.name}</strong> : coordinates.length > 2 ? <strong>Selected area · {polygonAreaKm2.toFixed(2)} km²</strong> : null}
              </div>

              <div className="draw-actions">
                <button type="button" className={!drawing && !areaClosed ? 'active' : ''} onClick={beginGeometry}><Icon name="area" />Draw area</button>
                {drawing && (
                  <>
                    <button type="button" disabled={!coordinates.length} onClick={undo}><Icon name="undo" />Undo</button>
                    <button type="button" disabled={!coordinates.length} onClick={clearGeometry}><Icon name="clear" />Clear</button>
                    {coordinates.length > 2 && !areaClosed && (
                      <button type="button" className="confirm-button" onClick={completeGeometry}>Close area</button>
                    )}
                  </>
                )}
                {!drawing && areaClosed && (
                  <>
                    <button type="button" onClick={() => setDrawing(true)}><Icon name="draw" />Edit</button>
                    <button type="button" onClick={clearGeometry}><Icon name="clear" />Clear</button>
                  </>
                )}
              </div>

              <div className="time-actions">
                <span>Time window <small>Up to 90 days for archive search</small></span>
                <div className="date-range">
                  <input type="date" value={windowStart} max={today} onChange={(event) => handleWindowStartChange(event.target.value)} aria-label="Starting date" />
                  <span>to</span>
                  <input type="date" value={windowEnd} min={windowStart || undefined} max={today} onChange={(event) => handleWindowEndChange(event.target.value)} aria-label="Ending date" />
                </div>
                <label className="dataset-selector">
                  <span>Data family</span>
                  <select value={productType} onChange={(event) => { setProductType(event.target.value as NisarProductType); setCatalogResult(null); setCatalogError(''); setAoiWindowResult(null); setAoiWindowError(''); }} aria-label="NISAR product family">
                    <option value="GUNW">GUNW · interferometry</option>
                    <option value="GOFF">GOFF · pixel offsets</option>
                  </select>
                </label>
              </div>

              <div className="action-buttons">
                <button className="investigate-button" type="button" disabled={!catalogEligible || isCatalogLoading} onClick={runCatalogPreflight}>{isCatalogLoading ? 'Checking ASF...' : 'Check NISAR archive'}</button>
                <button className="demo-button" type="button" disabled={!ready || isProcessing} onClick={runInvestigation}>{isProcessing ? 'Preparing demo...' : 'Reference demo'}</button>
                {!catalogEligible && <p className="action-hint" role="status">{catalogDisabledReason}</p>}
              </div>
              {catalogResult && <CatalogPreflightPanel result={catalogResult} onClose={() => setCatalogResult(null)} />}
              {catalogResult && productType === 'GUNW' && (
                <AoiWindowPanel
                  coordinates={coordinates}
                  areaClosed={areaClosed}
                  jobId={harmonyJobId}
                  onJobIdChange={(value) => { setHarmonyJobId(value); setAoiWindowResult(null); setAoiWindowError(''); }}
                  isLoading={isAoiWindowLoading}
                  error={aoiWindowError}
                  result={aoiWindowResult}
                  onRun={runAoiWindowRead}
                />
              )}
              {catalogError && <p className="catalog-error" role="alert">{catalogError}</p>}
            </>
          ) : (
            <ResultPanel result={result} onReset={() => setResult(null)} />
          )}

          {error && <p className="form-error">{error}</p>}
        </section>
      </main>
    </div>
  );
}
