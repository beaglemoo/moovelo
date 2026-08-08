<script lang="ts">
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { Feature, FeatureCollection, LineString } from 'geojson';
	import { onMount } from 'svelte';
	import type { Climb, PoiResult, Waypoint } from '$lib/api';
	import { cumulativeDistances, nearestVertexIndex } from '$lib/geo';
	import { coordsBetween, GRADIENT_BANDS, type GradientSegment } from '$lib/gradient';
	import { colourExpression } from '$lib/pois';

	interface Props {
		waypoints: Waypoint[];
		routeLine: [number, number][];
		legStartIndices: number[];
		/** Gradient-band segments to colour the route line by. Empty when the
		 * route has no elevation - the line then falls back to the plain
		 * colour rather than disappearing. */
		gradientSegments?: GradientSegment[];
		hoverPoint: [number, number] | null;
		cyclosmTileUrl: string | null;
		fitTrigger: number;
		/** Pan here when this counter changes, e.g. after picking a search
		 * result that is off screen. Optional: the read-only share page
		 * shows a map with no search attached to it. */
		flyTo?: Waypoint | null;
		flyTrigger?: number;
		onAddWaypoint: (wp: Waypoint) => void;
		onMoveWaypoint: (index: number, wp: Waypoint) => void;
		onInsertVia: (position: number, wp: Waypoint) => void;
		onRemoveWaypoint: (index: number) => void;
		onSetStart: (wp: Waypoint) => void;
		onSetEnd: (wp: Waypoint) => void;
		onClear: () => void;
		/** The map centre, so place search can prefer the area in view -
		 * many English places share a name. */
		onMapMove?: (centre: Waypoint) => void;
		/** Name the point under the context menu. Left out when there is no
		 * place index, and by the read-only share page, which has no menu. */
		resolvePlaceName?: (wp: Waypoint) => Promise<string | null>;
		/** Whether the place index exists, so the signed cycle network has
		 * anything to draw. False leaves the source unadded and the toggle
		 * hidden rather than fetching empty tiles forever. */
		cycleNetworkAvailable?: boolean;
		/** Index build stamp, appended to the tile URL. The tiles carry a
		 * long max-age, so without it a re-index stays invisible for a day. */
		cycleNetworkVersion?: string | null;
		/** Water, coffee and the rest, drawn as a circle layer. */
		pois?: PoiResult[];
		hoveredPoiId?: number | null;
		/** Hovering a marker highlights its row in the panel. This is the
		 * first map-to-app hover in the codebase - the elevation chart only
		 * ever syncs the other way. */
		onHoverPoi?: (id: number | null) => void;
		/** Detected climbs, 0-10 per ride - few enough that only the hovered
		 * one's sub-line is ever drawn, rather than a feature per climb. */
		climbs?: Climb[];
		hoveredClimbIndex?: number | null;
		/** Valhalla's own isochrone GeoJSON. Null hides the layer rather than
		 * clearing its source, so a stale polygon never flashes back on. */
		isochrone?: FeatureCollection | null;
		/** Where the isochrone was asked from, drawn as a small marker of its
		 * own so the overlay never reads as anchored to the route. */
		isochroneOrigin?: Waypoint | null;
		/** Opens the context menu's "Isochrone from here" entry. Left out by
		 * the read-only share page, which has no menu at all. */
		onIsochrone?: (wp: Waypoint) => void;
		/** "Loop from here" on the context menu. Left out by the read-only
		 * share page, which has no menu. */
		onLoop?: (wp: Waypoint) => void;
		/** Up to 3 candidate loops from the generator (services/loop.py),
		 * drawn as ghost lines while its card is open. */
		loopPreviews?: { index: number; coords: [number, number][] }[] | null;
		hoveredLoopIndex?: number | null;
	}

	let {
		waypoints,
		routeLine,
		legStartIndices,
		gradientSegments = [],
		hoverPoint,
		cyclosmTileUrl,
		fitTrigger,
		flyTo = null,
		flyTrigger = 0,
		onMapMove,
		resolvePlaceName,
		cycleNetworkAvailable = false,
		cycleNetworkVersion = null,
		pois = [],
		hoveredPoiId = null,
		onHoverPoi,
		climbs = [],
		hoveredClimbIndex = null,
		isochrone = null,
		isochroneOrigin = null,
		onIsochrone,
		onLoop,
		loopPreviews = null,
		hoveredLoopIndex = null,
		onAddWaypoint,
		onMoveWaypoint,
		onInsertVia,
		onRemoveWaypoint,
		onSetStart,
		onSetEnd,
		onClear
	}: Props = $props();

	interface ContextMenu {
		x: number;
		y: number;
		wp: Waypoint;
		waypointIndex: number | null;
	}

	let menu: ContextMenu | null = $state(null);
	let menuPlace: string | null = $state(null);

	type Basemap = 'cyclosm' | 'osm';
	const BASEMAP_STORAGE_KEY = 'moovelo:basemap';

	const PUBLIC_CYCLOSM_TILES = [
		'https://a.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png',
		'https://b.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png',
		'https://c.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png'
	];

	// CyclOSM is the default for its cycling detail. The public community-run
	// servers render uncached high-zoom tiles on demand (seconds per tile), so
	// a self-hosted CyclOSM tile server can be configured via TILE_URL_CYCLOSM,
	// and the faster OSM standard style is offered as a fallback either way.
	const baseStyle = (cyclosmTiles: string[]): maplibregl.StyleSpecification => ({
		version: 8,
		sources: {
			cyclosm: {
				type: 'raster',
				tiles: cyclosmTiles,
				tileSize: 256,
				maxzoom: 20,
				attribution:
					'<a href="https://www.cyclosm.org">CyclOSM</a> | © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
			},
			osm: {
				type: 'raster',
				tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
				tileSize: 256,
				maxzoom: 19,
				attribution:
					'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
			}
		},
		layers: [
			{ id: 'basemap-cyclosm', type: 'raster', source: 'cyclosm' },
			{ id: 'basemap-osm', type: 'raster', source: 'osm', layout: { visibility: 'none' } }
		]
	});

	function savedBasemap(): Basemap {
		return localStorage.getItem(BASEMAP_STORAGE_KEY) === 'osm' ? 'osm' : 'cyclosm';
	}

	let basemap: Basemap = $state('cyclosm');

	const OVERLAY_STORAGE_KEY = 'moovelo:cycle-network';
	let cycleNetwork = $state(false);

	let container: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let mapReady = $state(false);
	let markers: maplibregl.Marker[] = [];

	function lineGeoJSON(line: [number, number][]): Feature<LineString> {
		return { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } };
	}

	// One feature per gradient segment, carrying its band index. With no
	// segments (no elevation) the whole line is one feature with no `band`
	// property, so the match expression below falls through to its default
	// colour instead of the layer going empty.
	function gradientGeoJSON(
		segments: GradientSegment[],
		line: [number, number][]
	): FeatureCollection {
		if (segments.length === 0) {
			return {
				type: 'FeatureCollection',
				features:
					line.length >= 2
						? [
								{
									type: 'Feature',
									properties: {},
									geometry: { type: 'LineString', coordinates: line }
								}
							]
						: []
			};
		}
		return {
			type: 'FeatureCollection',
			features: segments.map((segment) => ({
				type: 'Feature',
				properties: { band: segment.band },
				geometry: { type: 'LineString', coordinates: segment.coords }
			}))
		};
	}

	/** A MapLibre `match` expression colouring the route line by gradient
	 * band, falling back to the plain route colour for the unbanded
	 * (no-elevation) feature `gradientGeoJSON` emits above. */
	function gradientColourExpression(): unknown[] {
		const match: unknown[] = ['match', ['get', 'band']];
		GRADIENT_BANDS.forEach((band, i) => match.push(i, band.colour));
		match.push('#d33682');
		return match;
	}

	/** The route line's own coordinates between the hovered climb's start and
	 * end distance, or empty when nothing is hovered - reusing gradient.ts's
	 * `coordsBetween` rather than duplicating its interpolation. */
	function climbHighlightGeoJSON(
		climb: Climb | null,
		line: [number, number][]
	): Feature<LineString> {
		if (!climb || line.length < 2) return lineGeoJSON([]);
		const dists = cumulativeDistances(line);
		return lineGeoJSON(coordsBetween(line, dists, climb.start_dist_m, climb.end_dist_m));
	}

	function poiGeoJSON(list: PoiResult[]): FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: list.map((poi) => ({
				type: 'Feature',
				id: poi.id,
				// `hovered` rides along in the data rather than in a filter, so
				// emphasis is a paint expression and needs no second layer.
				properties: { id: poi.id, category: poi.category, hovered: poi.id === hoveredPoiId },
				geometry: { type: 'Point', coordinates: [poi.lon, poi.lat] }
			}))
		};
	}

	/** Up to 3 candidate loop lines, carrying which candidate they are and
	 * whether the matching card row is hovered - both ride in the feature
	 * data (the POI-layer pattern) so emphasis is a paint expression. */
	function loopPreviewGeoJSON(
		previews: { index: number; coords: [number, number][] }[] | null,
		hoveredIndex: number | null
	): FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: (previews ?? []).map((preview) => ({
				type: 'Feature',
				properties: { candidate: preview.index, hovered: preview.index === hoveredIndex },
				geometry: { type: 'LineString', coordinates: preview.coords }
			}))
		};
	}

	function pointGeoJSON(point: [number, number] | null): FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: point
				? [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: point } }]
				: []
		};
	}

	function emptyFeatureCollection(): FeatureCollection {
		return { type: 'FeatureCollection', features: [] };
	}

	onMount(() => {
		basemap = savedBasemap();
		cycleNetwork = localStorage.getItem(OVERLAY_STORAGE_KEY) === 'on';
		map = new maplibregl.Map({
			container,
			style: baseStyle(cyclosmTileUrl ? [cyclosmTileUrl] : PUBLIC_CYCLOSM_TILES),
			center: [-1.4, 52.8],
			zoom: 6.3,
			// Right-button drag rotates the map by default, and that gesture
			// begins on the very press that opens our context menu - so the
			// menu closed itself roughly one time in six. Right-click is a
			// menu here, not a rotate. The compass control and two-finger
			// touch rotation still work.
			dragRotate: false
		});
		map.addControl(new maplibregl.NavigationControl(), 'top-right');
		map.addControl(
			new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true } }),
			'top-right'
		);

		map.on('moveend', () => {
			if (!map) return;
			const centre = map.getCenter();
			onMapMove?.({ lat: centre.lat, lon: centre.lng });
		});

		map.on('load', () => {
			if (!map) return;
			// Added first so the planned route and its markers draw on top of
			// it. The overlay is context, not the thing being edited.
			if (cycleNetworkAvailable) {
				map.addSource('cycle-network', {
					type: 'vector',
					tiles: [
						`${location.origin}/api/places/cycle-network/{z}/{x}/{y}.mvt` +
							(cycleNetworkVersion ? `?v=${cycleNetworkVersion}` : '')
					],
					minzoom: 4,
					maxzoom: 16
				});
				map.addLayer({
					id: 'cycle-network-line',
					type: 'line',
					source: 'cycle-network',
					'source-layer': 'cycle_ways',
					layout: {
						'line-cap': 'round',
						'line-join': 'round',
						visibility: 'none'
					},
					paint: {
						'line-color': [
							'match',
							['get', 'network'],
							'icn',
							'#6c71c4',
							'ncn',
							'#268bd2',
							'rcn',
							'#cb4b16',
							'#859900'
						],
						// Thin at country scale where routes are dense, and
						// wide enough to follow once you are looking at roads.
						'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1, 10, 2, 14, 3.5],
						'line-opacity': 0.75
					}
				});
			}
			// A casing under the coloured route line, drawn only for whichever
			// climb ClimbsList is hovering - one source updated on hover, not a
			// feature per climb, since a ride carries at most a handful.
			map.addSource('climb-highlight', { type: 'geojson', data: lineGeoJSON([]) });
			map.addLayer({
				id: 'climb-highlight-line',
				type: 'line',
				source: 'climb-highlight',
				layout: { 'line-cap': 'round', 'line-join': 'round' },
				paint: {
					'line-color': '#ffffff',
					'line-width': 10,
					'line-opacity': 0.85
				}
			});
			// Inserted below climb-highlight-line (via beforeId) so the route,
			// its markers and the climb casing all stay readable on top of a
			// wide reachability polygon. Hidden until an isochrone is asked
			// for, and styled entirely from each feature's own Valhalla
			// properties rather than a fixed paint colour, since a request can
			// carry several differently-coloured contours at once.
			map.addSource('isochrone', { type: 'geojson', data: emptyFeatureCollection() });
			map.addLayer(
				{
					id: 'isochrone-fill',
					type: 'fill',
					source: 'isochrone',
					layout: { visibility: 'none' },
					paint: {
						'fill-color': ['get', 'fill'],
						'fill-opacity': 0.25
					}
				},
				'climb-highlight-line'
			);
			map.addLayer(
				{
					id: 'isochrone-outline',
					type: 'line',
					source: 'isochrone',
					layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
					paint: {
						'line-color': ['get', 'color'],
						'line-width': 1.5,
						'line-opacity': 0.8
					}
				},
				'climb-highlight-line'
			);
			map.addSource('isochrone-origin', { type: 'geojson', data: pointGeoJSON(null) });
			map.addLayer(
				{
					id: 'isochrone-origin-point',
					type: 'circle',
					source: 'isochrone-origin',
					paint: {
						'circle-radius': 6,
						'circle-color': '#ffffff',
						'circle-stroke-color': '#268bd2',
						'circle-stroke-width': 3
					}
				},
				'climb-highlight-line'
			);
			// Candidate loops from the generator (services/loop.py), previewed
			// as ghost lines while its card is open - added before the real
			// route line so that stays on top of them.
			map.addSource('loop-preview', { type: 'geojson', data: loopPreviewGeoJSON(null, null) });
			map.addLayer({
				id: 'loop-preview-line',
				type: 'line',
				source: 'loop-preview',
				layout: { 'line-cap': 'round', 'line-join': 'round' },
				paint: {
					'line-color': [
						'match',
						['get', 'candidate'],
						0,
						'#268bd2',
						1,
						'#b58900',
						2,
						'#d33682',
						'#93a1a1'
					],
					'line-width': ['case', ['get', 'hovered'], 5, 3],
					'line-opacity': 0.65,
					'line-dasharray': [2, 1.5]
				}
			});
			// The visible line is driven by its own gradient-coloured source;
			// `route` stays a plain LineString because dragging and hit-testing
			// (route-hit below) don't care about gradient bands.
			map.addSource('route-gradient', { type: 'geojson', data: gradientGeoJSON([], []) });
			map.addLayer({
				id: 'route-line',
				type: 'line',
				source: 'route-gradient',
				layout: { 'line-cap': 'round', 'line-join': 'round' },
				paint: {
					'line-color': gradientColourExpression() as maplibregl.ExpressionSpecification,
					'line-width': 4.5,
					'line-opacity': 0.85
				}
			});
			map.addSource('route', { type: 'geojson', data: lineGeoJSON([]) });
			// Wide invisible twin of the route line so it is easy to grab.
			map.addLayer({
				id: 'route-hit',
				type: 'line',
				source: 'route',
				paint: { 'line-color': '#000000', 'line-width': 18, 'line-opacity': 0.001 }
			});
			map.addSource('drag-point', { type: 'geojson', data: pointGeoJSON(null) });
			map.addLayer({
				id: 'drag-point',
				type: 'circle',
				source: 'drag-point',
				paint: {
					'circle-radius': 6,
					'circle-color': '#ffffff',
					'circle-stroke-color': '#d33682',
					'circle-stroke-width': 3
				}
			});
			// POIs are a GL layer rather than maplibregl.Marker elements: a
			// 50 km ride passes hundreds of them, unlike the handful of
			// draggable waypoints, and that many DOM nodes would crawl.
			map.addSource('pois', { type: 'geojson', data: poiGeoJSON([]) });
			map.addLayer({
				id: 'poi-points',
				type: 'circle',
				source: 'pois',
				paint: {
					'circle-radius': ['case', ['get', 'hovered'], 8, 5],
					'circle-color': colourExpression() as maplibregl.ExpressionSpecification,
					'circle-stroke-color': '#ffffff',
					'circle-stroke-width': ['case', ['get', 'hovered'], 3, 1.5]
				}
			});
			map.addSource('hover-point', { type: 'geojson', data: pointGeoJSON(null) });
			map.addLayer({
				id: 'hover-point',
				type: 'circle',
				source: 'hover-point',
				paint: {
					'circle-radius': 5,
					'circle-color': '#268bd2',
					'circle-stroke-color': '#ffffff',
					'circle-stroke-width': 2
				}
			});
			setupInteractions(map);
			mapReady = true;
		});

		return () => {
			map?.remove();
			map = undefined;
		};
	});

	let longPressFired = false;

	function setupInteractions(m: maplibregl.Map) {
		m.on('click', (e) => {
			if (longPressFired) {
				longPressFired = false;
				return;
			}
			if (menu) {
				menu = null;
				return;
			}
			// Grabbing the line handles its own interaction; plain map clicks add waypoints.
			if (m.queryRenderedFeatures(e.point, { layers: ['route-hit'] }).length > 0) return;
			onAddWaypoint({ lat: e.lngLat.lat, lon: e.lngLat.lng });
		});

		m.on('contextmenu', (e) => {
			e.preventDefault();
			menu = {
				x: e.point.x,
				y: e.point.y,
				wp: { lat: e.lngLat.lat, lon: e.lngLat.lng },
				waypointIndex: null
			};
		});

		// Touch devices get no contextmenu on the canvas; a long-press (600ms
		// without moving or lifting) opens the same menu.
		let pressTimer: ReturnType<typeof setTimeout> | null = null;
		const cancelPress = () => {
			if (pressTimer) {
				clearTimeout(pressTimer);
				pressTimer = null;
			}
		};
		m.on('touchstart', (e) => {
			cancelPress();
			if (e.points.length !== 1) return;
			const { point, lngLat } = e;
			pressTimer = setTimeout(() => {
				pressTimer = null;
				longPressFired = true;
				menu = {
					x: point.x,
					y: point.y,
					wp: { lat: lngLat.lat, lon: lngLat.lng },
					waypointIndex: null
				};
			}, 600);
		});
		m.on('touchmove', cancelPress);
		m.on('touchend', cancelPress);
		m.on('touchcancel', cancelPress);

		// Close the menu when the rider moves the map under it - but listen
		// for the gestures, not `movestart`, which also fires when the map is
		// merely resized. The panel below the map grows as a route is planned
		// and again when its POIs arrive, and each of those resizes was
		// closing a context menu the rider had just opened. That is the whole
		// of the "sometimes the menu vanishes before you can click Remove"
		// flake: it needed a resize to land in the gap after the right-click.
		for (const gesture of ['dragstart', 'zoomstart', 'rotatestart', 'pitchstart'] as const) {
			m.on(gesture, () => (menu = null));
		}

		m.on('mouseenter', 'route-hit', () => (m.getCanvas().style.cursor = 'grab'));
		m.on('mouseleave', 'route-hit', () => (m.getCanvas().style.cursor = ''));

		// Mirrors the cursor handlers above, but reports outward so the panel
		// can highlight the matching row.
		m.on('mousemove', 'poi-points', (e) => {
			m.getCanvas().style.cursor = 'pointer';
			const id = e.features?.[0]?.properties?.id;
			if (typeof id === 'number') onHoverPoi?.(id);
		});
		m.on('mouseleave', 'poi-points', () => {
			m.getCanvas().style.cursor = '';
			onHoverPoi?.(null);
		});

		const startDrag = (
			grab: maplibregl.LngLat,
			moveEvent: 'mousemove' | 'touchmove',
			endEvent: 'mouseup' | 'touchend'
		) => {
			const grabIndex = nearestVertexIndex(routeLine, [grab.lng, grab.lat]);
			let last = grab;
			const onMove = (ev: maplibregl.MapMouseEvent | maplibregl.MapTouchEvent) => {
				last = ev.lngLat;
				setSourceData(m, 'drag-point', pointGeoJSON([last.lng, last.lat]));
			};
			m.on(moveEvent, onMove);
			m.once(endEvent, () => {
				m.off(moveEvent, onMove);
				setSourceData(m, 'drag-point', pointGeoJSON(null));
				// The grabbed vertex sits inside some leg; the new via goes
				// between that leg's endpoints.
				let leg = 0;
				while (leg + 1 < legStartIndices.length && legStartIndices[leg + 1] <= grabIndex) leg++;
				onInsertVia(leg + 1, { lat: last.lat, lon: last.lng });
			});
		};

		// Waypoint markers sit on the route line and their DOM events bubble to
		// the canvas container, so a press on a marker also hits route-hit -
		// without these guards, right-clicking or dragging a marker inserts a
		// phantom via point at that spot.
		const onMarker = (e: maplibregl.MapMouseEvent | maplibregl.MapTouchEvent) =>
			e.originalEvent.target instanceof Element &&
			e.originalEvent.target.closest('.maplibregl-marker') !== null;

		m.on('mousedown', 'route-hit', (e) => {
			if (e.originalEvent.button !== 0 || onMarker(e)) return;
			e.preventDefault();
			startDrag(e.lngLat, 'mousemove', 'mouseup');
		});
		m.on('touchstart', 'route-hit', (e) => {
			if (e.points.length !== 1 || onMarker(e)) return;
			e.preventDefault();
			startDrag(e.lngLat, 'touchmove', 'touchend');
		});
	}

	function setSourceData(
		m: maplibregl.Map,
		id: string,
		data: Feature<LineString> | FeatureCollection
	) {
		const source = m.getSource<maplibregl.GeoJSONSource>(id);
		source?.setData(data);
	}

	function markerColor(index: number, count: number): string {
		if (index === 0) return '#2aa198';
		if (index === count - 1) return '#dc322f';
		return '#268bd2';
	}

	// Sync waypoint markers.
	$effect(() => {
		if (!map || !mapReady) return;
		const m = map;
		markers.forEach((marker) => marker.remove());
		markers = waypoints.map((wp, i) => {
			const marker = new maplibregl.Marker({
				color: markerColor(i, waypoints.length),
				draggable: true,
				scale: 0.85
			})
				.setLngLat([wp.lon, wp.lat])
				.addTo(m);
			marker.on('dragend', () => {
				const pos = marker.getLngLat();
				onMoveWaypoint(i, { lat: pos.lat, lon: pos.lng });
			});
			const el = marker.getElement();
			el.addEventListener('contextmenu', (event) => {
				event.preventDefault();
				event.stopPropagation();
				const rect = container.getBoundingClientRect();
				menu = {
					x: event.clientX - rect.left,
					y: event.clientY - rect.top,
					wp,
					waypointIndex: i
				};
			});
			// Long-press on a marker opens its menu on touch devices.
			let markerTimer: ReturnType<typeof setTimeout> | null = null;
			const cancelMarkerPress = () => {
				if (markerTimer) {
					clearTimeout(markerTimer);
					markerTimer = null;
				}
			};
			el.addEventListener(
				'touchstart',
				(event) => {
					if (event.touches.length !== 1) return;
					const touch = event.touches[0];
					markerTimer = setTimeout(() => {
						markerTimer = null;
						longPressFired = true;
						const rect = container.getBoundingClientRect();
						menu = {
							x: touch.clientX - rect.left,
							y: touch.clientY - rect.top,
							wp,
							waypointIndex: i
						};
					}, 600);
				},
				{ passive: true }
			);
			el.addEventListener('touchend', cancelMarkerPress);
			el.addEventListener('touchmove', cancelMarkerPress);
			el.addEventListener('touchcancel', cancelMarkerPress);
			return marker;
		});
	});

	// Name whatever the menu is pointing at. One effect rather than a call at
	// each of the four sites that open the menu (right-click and long-press,
	// on the canvas and on a marker), and its cleanup discards a lookup whose
	// menu has already gone - the name arrives after a round trip, by which
	// time the menu may be somewhere else entirely.
	$effect(() => {
		const current = menu;
		menuPlace = null;
		if (!current || !resolvePlaceName) return;
		let stale = false;
		void resolvePlaceName(current.wp).then((name) => {
			if (!stale) menuPlace = name;
		});
		return () => {
			stale = true;
		};
	});

	function menuAction(action: () => void) {
		// Run the action before clearing the menu: the menu items capture
		// template constants derived from `menu`, and nulling it first makes
		// that read throw instead of running the action.
		action();
		menu = null;
	}

	// MapLibre's own keyboard handler pans and zooms with arrows and +/-,
	// and it does so through easeTo - which fires movestart, not any of the
	// gesture events the menu listens for. So the keys are handled here
	// too, or a menu opened by right-click would sit at stale pixel
	// coordinates over a map that has moved beneath it.
	// Exactly what MapLibre's KeyboardHandler binds - arrows to pan, +/-
	// to zoom. PageUp and PageDown were in here and move nothing.
	const PAN_KEYS = new Set(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', '+', '=', '-', '_']);

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			menu = null;
			return;
		}
		if (!PAN_KEYS.has(event.key)) return;
		// Only when the keypress is actually going to the map. This listener
		// is on the window, and MapLibre only pans when its canvas has
		// focus - so without this check, arrowing down the place-search
		// suggestions closed the context menu while the map sat still.
		const target = event.target;
		if (target instanceof Node && container?.contains(target)) menu = null;
	}

	// Sync route geometry.
	$effect(() => {
		if (!map || !mapReady) return;
		setSourceData(map, 'route', lineGeoJSON(routeLine));
	});

	// Sync the gradient-coloured route line.
	$effect(() => {
		if (!map || !mapReady) return;
		setSourceData(map, 'route-gradient', gradientGeoJSON(gradientSegments, routeLine));
	});

	// Sync the hovered-climb casing.
	$effect(() => {
		if (!map || !mapReady) return;
		const climb = hoveredClimbIndex !== null ? (climbs[hoveredClimbIndex] ?? null) : null;
		setSourceData(map, 'climb-highlight', climbHighlightGeoJSON(climb, routeLine));
	});

	// Sync the POI layer. Reading hoveredPoiId here as well as `pois` is
	// deliberate: the hovered flag lives in the feature data, so a hover
	// change has to rewrite the source for the paint expression to see it.
	$effect(() => {
		if (!map || !mapReady) return;
		void hoveredPoiId;
		setSourceData(map, 'pois', poiGeoJSON(pois));
	});

	// Sync candidate loop previews. Reads hoveredLoopIndex too, for the same
	// reason the POI layer does above: emphasis lives in the feature data,
	// not a filter, so a hover change has to rewrite the source.
	$effect(() => {
		if (!map || !mapReady) return;
		void hoveredLoopIndex;
		setSourceData(map, 'loop-preview', loopPreviewGeoJSON(loopPreviews, hoveredLoopIndex));
	});

	// Sync elevation-chart hover marker.
	$effect(() => {
		if (!map || !mapReady) return;
		setSourceData(map, 'hover-point', pointGeoJSON(hoverPoint));
	});

	// Sync the isochrone overlay. Visibility follows presence of data rather
	// than a separate flag - null hides the layer, clearing the origin
	// marker along with it, so the two can never disagree about whether an
	// isochrone is currently shown.
	$effect(() => {
		if (!map || !mapReady) return;
		const data = isochrone ?? emptyFeatureCollection();
		setSourceData(map, 'isochrone', data);
		const visibility = isochrone ? 'visible' : 'none';
		map.setLayoutProperty('isochrone-fill', 'visibility', visibility);
		map.setLayoutProperty('isochrone-outline', 'visibility', visibility);
		setSourceData(
			map,
			'isochrone-origin',
			pointGeoJSON(isochroneOrigin ? [isochroneOrigin.lon, isochroneOrigin.lat] : null)
		);
	});

	// Fit the viewport to the route when asked (e.g. after loading a saved one).
	$effect(() => {
		// Read all reactive dependencies before any early return.
		const trigger = fitTrigger;
		const line = routeLine;
		if (trigger === 0 || !map || !mapReady || line.length < 2) return;
		menu = null;
		const lons = line.map((c) => c[0]);
		const lats = line.map((c) => c[1]);
		map.fitBounds(
			[
				[Math.min(...lons), Math.min(...lats)],
				[Math.max(...lons), Math.max(...lats)]
			],
			{ padding: 60, animate: false }
		);
	});

	// Pan to a picked search result, which may be well off screen.
	$effect(() => {
		const trigger = flyTrigger;
		const target = flyTo;
		if (trigger === 0 || !map || !mapReady || !target) return;
		// Closed here rather than left to the gesture listeners. Under
		// prefers-reduced-motion MapLibre turns flyTo into jumpTo, which
		// fires movestart/move/moveend and fires zoomstart only if the zoom
		// actually changes - and this call asks for max(current, 12), a
		// no-op above zoom 12. So for a rider who has asked for reduced
		// motion, picking a search result moved the map and left the menu
		// sitting over the wrong place.
		menu = null;
		map.flyTo({
			center: [target.lon, target.lat],
			// Zoom in if the map is showing the whole country, but never
			// zoom out from wherever the rider already is.
			zoom: Math.max(map.getZoom(), 12),
			duration: 800
		});
	});

	// Sync basemap layer visibility.
	$effect(() => {
		if (!map || !mapReady) return;
		map.setLayoutProperty(
			'basemap-cyclosm',
			'visibility',
			basemap === 'cyclosm' ? 'visible' : 'none'
		);
		map.setLayoutProperty('basemap-osm', 'visibility', basemap === 'osm' ? 'visible' : 'none');
	});

	function switchBasemap(next: Basemap) {
		basemap = next;
		localStorage.setItem(BASEMAP_STORAGE_KEY, next);
	}

	// Sync the overlay. A plain aria-pressed toggle rather than a member of
	// the basemap radiogroup: showing the network is orthogonal to which
	// basemap is underneath it, not mutually exclusive with it.
	$effect(() => {
		if (!map || !mapReady || !cycleNetworkAvailable) return;
		map.setLayoutProperty('cycle-network-line', 'visibility', cycleNetwork ? 'visible' : 'none');
	});

	function toggleCycleNetwork() {
		cycleNetwork = !cycleNetwork;
		localStorage.setItem(OVERLAY_STORAGE_KEY, cycleNetwork ? 'on' : 'off');
	}
</script>

<!-- A window resize or a device rotation reflows the map under a menu that
     is positioned in pixels, so it has to go. This is safe to listen for
     now that the POI panel is a fixed height: the panel no longer grows
     under the map, which is what made the old movestart handler close
     menus the rider had just opened. -->
<svelte:window onkeydown={handleKeydown} onresize={() => (menu = null)} />

<div class="map" bind:this={container}></div>
{#if menu}
	<div class="context-menu" style="left: {menu.x}px; top: {menu.y}px" role="menu">
		{#if menuPlace}
			<div class="menu-place">{menuPlace}</div>
		{/if}
		{#if menu.waypointIndex !== null}
			{@const idx = menu.waypointIndex}
			<button type="button" role="menuitem" onclick={() => menuAction(() => onRemoveWaypoint(idx))}>
				Remove waypoint
			</button>
		{:else}
			{@const wp = menu.wp}
			<button type="button" role="menuitem" onclick={() => menuAction(() => onSetStart(wp))}>
				Route from here
			</button>
			<button type="button" role="menuitem" onclick={() => menuAction(() => onAddWaypoint(wp))}>
				Add waypoint
			</button>
			<button type="button" role="menuitem" onclick={() => menuAction(() => onSetEnd(wp))}>
				Route to here
			</button>
			{#if onIsochrone}
				<hr />
				<button type="button" role="menuitem" onclick={() => menuAction(() => onIsochrone(wp))}>
					Isochrone from here
				</button>
			{/if}
			{#if onLoop}
				<button type="button" role="menuitem" onclick={() => menuAction(() => onLoop(wp))}>
					Loop from here
				</button>
			{/if}
			{#if waypoints.length > 0}
				<hr />
				<button type="button" role="menuitem" onclick={() => menuAction(onClear)}>
					Clear route
				</button>
			{/if}
		{/if}
	</div>
{/if}
<div class="basemap-switch" role="radiogroup" aria-label="Basemap">
	<button
		type="button"
		role="radio"
		aria-checked={basemap === 'cyclosm'}
		class:active={basemap === 'cyclosm'}
		title="Cycling map (can be slow to render new areas)"
		onclick={() => switchBasemap('cyclosm')}
	>
		CyclOSM
	</button>
	<button
		type="button"
		role="radio"
		aria-checked={basemap === 'osm'}
		class:active={basemap === 'osm'}
		title="OpenStreetMap standard (faster)"
		onclick={() => switchBasemap('osm')}
	>
		OSM
	</button>
</div>
{#if cycleNetworkAvailable}
	<button
		type="button"
		class="overlay-toggle"
		class:active={cycleNetwork}
		aria-pressed={cycleNetwork}
		title="Show signed cycle routes (NCN, RCN, LCN)"
		onclick={toggleCycleNetwork}
	>
		Cycle routes
	</button>
{/if}

<style>
	.map {
		width: 100%;
		height: 100%;
	}
	.basemap-switch {
		position: absolute;
		bottom: 28px;
		left: 10px;
		display: flex;
		border-radius: 8px;
		overflow: hidden;
		border: 1px solid #ccc;
		background: #fff;
		box-shadow: 0 1px 4px #0002;
		z-index: 5;
	}
	.basemap-switch button {
		border: none;
		background: transparent;
		padding: 0.3rem 0.6rem;
		font: inherit;
		font-size: 0.8rem;
		cursor: pointer;
	}
	.basemap-switch button + button {
		border-left: 1px solid #ddd;
	}
	.basemap-switch button.active {
		background: #268bd2;
		color: #fff;
	}
	.overlay-toggle {
		position: absolute;
		bottom: 28px;
		left: 132px;
		border: 1px solid #ccc;
		border-radius: 8px;
		background: #fff;
		box-shadow: 0 1px 4px #0002;
		padding: 0.3rem 0.6rem;
		font: inherit;
		font-size: 0.8rem;
		color: #586e75;
		cursor: pointer;
		z-index: 5;
	}
	.overlay-toggle.active {
		background: #268bd2;
		border-color: #268bd2;
		color: #fff;
	}
	.context-menu {
		position: absolute;
		min-width: 160px;
		background: #fff;
		border: 1px solid #ccc;
		border-radius: 8px;
		box-shadow: 0 2px 10px #0003;
		padding: 4px;
		z-index: 10;
		display: flex;
		flex-direction: column;
	}
	.context-menu button {
		border: none;
		background: transparent;
		text-align: left;
		padding: 0.45rem 0.7rem;
		font: inherit;
		font-size: 0.9rem;
		border-radius: 5px;
		cursor: pointer;
	}
	.context-menu button:hover {
		background: #f0f0f0;
	}
	.menu-place {
		padding: 0.35rem 0.7rem 0.4rem;
		font-size: 0.8rem;
		font-weight: 600;
		color: #586e75;
		border-bottom: 1px solid #e5e5e5;
		margin-bottom: 3px;
		overflow-wrap: anywhere;
	}
	.context-menu hr {
		border: none;
		border-top: 1px solid #e5e5e5;
		margin: 3px 0;
	}
</style>
