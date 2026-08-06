<script lang="ts">
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { Feature, FeatureCollection, LineString } from 'geojson';
	import { onMount } from 'svelte';
	import type { Waypoint } from '$lib/api';
	import { nearestVertexIndex } from '$lib/geo';

	interface Props {
		waypoints: Waypoint[];
		routeLine: [number, number][];
		legStartIndices: number[];
		hoverPoint: [number, number] | null;
		cyclosmTileUrl: string | null;
		fitTrigger: number;
		onAddWaypoint: (wp: Waypoint) => void;
		onMoveWaypoint: (index: number, wp: Waypoint) => void;
		onInsertVia: (position: number, wp: Waypoint) => void;
		onRemoveWaypoint: (index: number) => void;
		onSetStart: (wp: Waypoint) => void;
		onSetEnd: (wp: Waypoint) => void;
		onClear: () => void;
	}

	let {
		waypoints,
		routeLine,
		legStartIndices,
		hoverPoint,
		cyclosmTileUrl,
		fitTrigger,
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

	type Basemap = 'cyclosm' | 'osm';
	const BASEMAP_STORAGE_KEY = 'komoot-lite:basemap';

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

	let container: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let mapReady = $state(false);
	let markers: maplibregl.Marker[] = [];

	function lineGeoJSON(line: [number, number][]): Feature<LineString> {
		return { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } };
	}

	function pointGeoJSON(point: [number, number] | null): FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: point
				? [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: point } }]
				: []
		};
	}

	onMount(() => {
		basemap = savedBasemap();
		map = new maplibregl.Map({
			container,
			style: baseStyle(cyclosmTileUrl ? [cyclosmTileUrl] : PUBLIC_CYCLOSM_TILES),
			center: [-1.4, 52.8],
			zoom: 6.3
		});
		map.addControl(new maplibregl.NavigationControl(), 'top-right');
		map.addControl(
			new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true } }),
			'top-right'
		);

		map.on('load', () => {
			if (!map) return;
			map.addSource('route', { type: 'geojson', data: lineGeoJSON([]) });
			map.addLayer({
				id: 'route-line',
				type: 'line',
				source: 'route',
				layout: { 'line-cap': 'round', 'line-join': 'round' },
				paint: { 'line-color': '#d33682', 'line-width': 4.5, 'line-opacity': 0.85 }
			});
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

	function setupInteractions(m: maplibregl.Map) {
		m.on('click', (e) => {
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

		m.on('movestart', () => (menu = null));

		m.on('mouseenter', 'route-hit', () => (m.getCanvas().style.cursor = 'grab'));
		m.on('mouseleave', 'route-hit', () => (m.getCanvas().style.cursor = ''));

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

		m.on('mousedown', 'route-hit', (e) => {
			e.preventDefault();
			startDrag(e.lngLat, 'mousemove', 'mouseup');
		});
		m.on('touchstart', 'route-hit', (e) => {
			if (e.points.length !== 1) return;
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
			marker.getElement().addEventListener('contextmenu', (event) => {
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
			return marker;
		});
	});

	function menuAction(action: () => void) {
		menu = null;
		action();
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') menu = null;
	}

	// Sync route geometry.
	$effect(() => {
		if (!map || !mapReady) return;
		setSourceData(map, 'route', lineGeoJSON(routeLine));
	});

	// Sync elevation-chart hover marker.
	$effect(() => {
		if (!map || !mapReady) return;
		setSourceData(map, 'hover-point', pointGeoJSON(hoverPoint));
	});

	// Fit the viewport to the route when asked (e.g. after loading a saved one).
	$effect(() => {
		// Read all reactive dependencies before any early return.
		const trigger = fitTrigger;
		const line = routeLine;
		if (trigger === 0 || !map || !mapReady || line.length < 2) return;
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
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="map" bind:this={container}></div>
{#if menu}
	<div class="context-menu" style="left: {menu.x}px; top: {menu.y}px" role="menu">
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
	.context-menu hr {
		border: none;
		border-top: 1px solid #e5e5e5;
		margin: 3px 0;
	}
</style>
