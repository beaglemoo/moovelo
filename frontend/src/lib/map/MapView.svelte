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
		onAddWaypoint: (wp: Waypoint) => void;
		onMoveWaypoint: (index: number, wp: Waypoint) => void;
		onInsertVia: (position: number, wp: Waypoint) => void;
	}

	let {
		waypoints,
		routeLine,
		legStartIndices,
		hoverPoint,
		onAddWaypoint,
		onMoveWaypoint,
		onInsertVia
	}: Props = $props();

	const CYCLOSM_STYLE: maplibregl.StyleSpecification = {
		version: 8,
		sources: {
			cyclosm: {
				type: 'raster',
				tiles: [
					'https://a.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png',
					'https://b.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png',
					'https://c.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png'
				],
				tileSize: 256,
				maxzoom: 20,
				attribution:
					'<a href="https://www.cyclosm.org">CyclOSM</a> | © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
			}
		},
		layers: [{ id: 'cyclosm', type: 'raster', source: 'cyclosm' }]
	};

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
		map = new maplibregl.Map({
			container,
			style: CYCLOSM_STYLE,
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
			// Grabbing the line handles its own interaction; plain map clicks add waypoints.
			if (m.queryRenderedFeatures(e.point, { layers: ['route-hit'] }).length > 0) return;
			onAddWaypoint({ lat: e.lngLat.lat, lon: e.lngLat.lng });
		});

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
			return marker;
		});
	});

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
</script>

<div class="map" bind:this={container}></div>

<style>
	.map {
		width: 100%;
		height: 100%;
	}
</style>
