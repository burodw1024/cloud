// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useRef, useState } from 'react';
import * as L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import './style.css';
import { DataRow, MapOptions, MapPoint, prepareMap, tileUrl } from './model';

export interface IconMapProps extends MapOptions {
  width: number;
  height: number;
  rows: DataRow[];
  tileTemplate?: string;
  tileAttribution?: string;
  showLegend?: boolean;
  showLabels?: boolean;
}

const PATHS = {
  pin: 'M12 22s8-8.2 8-13a8 8 0 1 0-16 0c0 4.8 8 13 8 13z M12 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  branch: 'M2 9h20L12 2 2 9z M5 10v9 M10 10v9 M15 10v9 M20 10v9 M2 22h20',
  person: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M4 22v-3a8 8 0 0 1 16 0v3',
  warning: 'M12 2 1 22h22L12 2z M12 8v7 M12 18v1',
  circle: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18',
  square: 'M3 3h18v18H3z',
  diamond: 'M12 1 23 12 12 23 1 12z',
  triangle: 'M12 2 1 22h22z',
};

function markerIcon(point: MapPoint): L.DivIcon {
  const wrapper = document.createElement('div');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(point.size));
  svg.setAttribute('height', String(point.size));
  svg.setAttribute('aria-hidden', 'true');
  const background = document.createElementNS(svg.namespaceURI, 'circle');
  background.setAttribute('cx', '12');
  background.setAttribute('cy', '12');
  background.setAttribute('r', '11');
  background.setAttribute('fill', '#ffffff');
  background.setAttribute('fill-opacity', '.95');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', PATHS[point.icon]);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', point.color);
  path.setAttribute('stroke-width', '2');
  path.setAttribute('stroke-linejoin', 'round');
  path.setAttribute('stroke-linecap', 'round');
  svg.append(background, path);
  wrapper.append(svg);
  return L.divIcon({
    html: wrapper,
    className: 'buro-icon-map-marker',
    iconSize: [point.size, point.size],
    iconAnchor: [point.size / 2, point.size / 2],
  });
}

function popup(point: MapPoint): HTMLElement {
  const root = document.createElement('div');
  root.className = 'buro-map-popup';
  const title = document.createElement('strong');
  title.textContent = point.label;
  root.append(title);
  const list = document.createElement('dl');
  point.details.forEach(([field, value]) => {
    const label = document.createElement('dt');
    label.textContent = field;
    const cell = document.createElement('dd');
    cell.textContent = value;
    list.append(label, cell);
  });
  root.append(list);
  return root;
}

function fit(map: L.Map, points: MapPoint[]) {
  if (!points.length) return;
  if (points.length === 1)
    map.setView([points[0].latitude, points[0].displayLongitude], 11, {
      animate: false,
    });
  else
    map.fitBounds(
      L.latLngBounds(
        points.map(
          point => [point.latitude, point.displayLongitude] as L.LatLngTuple,
        ),
      ),
      { padding: [35, 35], maxZoom: 14, animate: false },
    );
}

class CoordinateGrid extends L.GridLayer {
  createTile(coords: L.Coords): HTMLElement {
    const tile = document.createElement('div');
    tile.className = 'buro-map-grid-tile';
    const world = 2 ** coords.z;
    const longitude = (((((coords.x / world) * 360) % 360) + 360) % 360) - 180;
    const latitude =
      (Math.atan(Math.sinh(Math.PI * (1 - (2 * coords.y) / world))) * 180) /
      Math.PI;
    const label = document.createElement('span');
    label.textContent = `${latitude.toFixed(2)}°, ${longitude.toFixed(2)}°`;
    tile.append(label);
    return tile;
  }
}

function gridLayer(): L.GridLayer {
  return new CoordinateGrid({
    tileSize: 256,
    attribution: 'Coordinate grid · no external map service',
  });
}

function escapedAttribution(value: string): string {
  const node = document.createElement('span');
  node.textContent = value.slice(0, 500);
  return node.innerHTML;
}

export default function IconMap(props: IconMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef<L.LayerGroup | null>(null);
  const model = useMemo(
    () =>
      prepareMap(props.rows, {
        latitudeColumn: props.latitudeColumn,
        longitudeColumn: props.longitudeColumn,
        labelColumn: props.labelColumn,
        categoryColumn: props.categoryColumn,
        sizeColumn: props.sizeColumn,
        iconColumn: props.iconColumn,
        tooltipColumns: props.tooltipColumns,
        iconType: props.iconType,
        markerSize: props.markerSize,
        maxMarkerSize: props.maxMarkerSize,
        fixedColor: props.fixedColor,
        rowLimit: props.rowLimit,
      }),
    [
      props.rows,
      props.latitudeColumn,
      props.longitudeColumn,
      props.labelColumn,
      props.categoryColumn,
      props.sizeColumn,
      props.iconColumn,
      props.tooltipColumns,
      props.iconType,
      props.markerSize,
      props.maxMarkerSize,
      props.fixedColor,
      props.rowLimit,
    ],
  );
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [tileError, setTileError] = useState(false);
  const template = tileUrl(props.tileTemplate);
  const visible = useMemo(
    () => model.points.filter(point => !hidden.has(point.category)),
    [model, hidden],
  );

  useEffect(() => {
    if (!container.current) return undefined;
    const instance = L.map(container.current, {
      zoomControl: false,
      attributionControl: true,
      worldCopyJump: false,
      preferCanvas: true,
    }).setView([0, 0], 2);
    L.control.zoom({ position: 'bottomright' }).addTo(instance);
    L.control
      .scale({ imperial: false, position: 'bottomleft' })
      .addTo(instance);
    map.current = instance;
    markers.current = L.layerGroup().addTo(instance);
    return () => {
      instance.remove();
      map.current = null;
      markers.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance) return undefined;
    setTileError(false);
    const grid = gridLayer().addTo(instance);
    const tiles = template
      ? L.tileLayer(template, {
          maxZoom: 19,
          attribution: escapedAttribution(
            props.tileAttribution || 'Configured tile service',
          ),
        }).addTo(instance)
      : null;
    const onError = () => setTileError(true);
    tiles?.on('tileerror', onError);
    return () => {
      tiles?.off('tileerror', onError);
      tiles?.remove();
      grid.remove();
    };
  }, [template, props.tileAttribution]);

  useEffect(() => {
    setHidden(new Set());
    if (map.current) fit(map.current, model.points);
  }, [model]);

  useEffect(() => {
    markers.current?.clearLayers();
    visible.forEach((point, index) => {
      const marker = L.marker([point.latitude, point.displayLongitude], {
        icon: markerIcon(point),
        title: point.label,
        alt: point.label,
        keyboard: true,
      }).bindPopup(popup(point), { maxWidth: 320 });
      if (props.showLabels && index < 200) {
        const label = document.createElement('span');
        label.textContent = point.label;
        marker.bindTooltip(label, { permanent: true, direction: 'top' });
      }
      markers.current?.addLayer(marker);
    });
  }, [visible, props.showLabels]);

  useEffect(() => {
    map.current?.invalidateSize({ animate: false });
  }, [props.width, props.height]);

  const toggle = (name: string) =>
    setHidden(previous => {
      const next = new Set(previous);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  const configured = Boolean(props.latitudeColumn && props.longitudeColumn);
  return (
    <section
      className="buro-icon-map"
      aria-label="BURO Icon Map"
      style={{ width: props.width, height: props.height }}
    >
      <header className="buro-map-toolbar">
        <div>
          <strong>Icon Map</strong>
          <span>{visible.length.toLocaleString()} plotted locations</span>
        </div>
        <button
          type="button"
          disabled={!visible.length}
          onClick={() => map.current && fit(map.current, visible)}
        >
          Fit locations
        </button>
      </header>
      <div
        className="buro-map-canvas"
        ref={container}
        aria-label="Interactive geographic map"
      />
      {(!configured || !model.points.length) && (
        <div className="buro-map-empty" role="status">
          {!configured
            ? 'Choose latitude and longitude columns in the Data panel.'
            : 'No valid coordinates were returned. Check decimal latitude and longitude values.'}
        </div>
      )}
      {props.showLegend !== false && model.categories.length > 1 && (
        <div className="buro-map-legend" aria-label="Filter categories">
          {model.categories.slice(0, 12).map(category => (
            <button
              key={category.name}
              type="button"
              aria-pressed={!hidden.has(category.name)}
              onClick={() => toggle(category.name)}
              title={category.name}
            >
              <i
                style={{ backgroundColor: category.color }}
                aria-hidden="true"
              />
              <span>{category.name}</span>
              <small>{category.count}</small>
            </button>
          ))}
          {model.categories.length > 12 && (
            <span>+{model.categories.length - 12} other categories</span>
          )}
        </div>
      )}
      <footer className="buro-map-notes" role="status">
        {!template && (
          <span>
            Offline coordinate grid · add an internal tile URL for geographic
            detail.
          </span>
        )}
        {Boolean(props.tileTemplate) && !template && (
          <span>Tile URL is invalid; coordinate grid is shown.</span>
        )}
        {tileError && (
          <span>
            Map tiles are unavailable; locations remain plotted on the
            coordinate grid.
          </span>
        )}
        {model.invalidRows > 0 && (
          <span>
            {model.invalidRows} rows excluded: missing/invalid coordinates or
            outside Web Mercator coverage.
          </span>
        )}
        {(model.atLimit || model.omittedRows > 0) && (
          <span>
            Query/display limit reached. Narrow the filters to inspect more
            locations.
          </span>
        )}
        {props.sizeColumn && (
          <span>
            Icon area scales with the absolute size value; tooltips retain the
            original values.
          </span>
        )}
        {props.showLabels && model.points.length > 200 && (
          <span>
            Permanent labels are limited to the first 200 visible locations.
          </span>
        )}
      </footer>
      <details className="buro-map-table">
        <summary>View plotted coordinates as a table</summary>
        <div>
          <table>
            <thead>
              <tr>
                <th>Location</th>
                <th>Latitude</th>
                <th>Longitude</th>
                <th>Category</th>
                <th>Size value</th>
              </tr>
            </thead>
            <tbody>
              {visible.slice(0, 100).map(point => (
                <tr key={point.key}>
                  <td>{point.label}</td>
                  <td>{point.latitude}</td>
                  <td>{point.longitude}</td>
                  <td>{point.category}</td>
                  <td>{point.value ?? 'Not provided'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {visible.length > 100 && (
          <p>First 100 visible locations, in returned order.</p>
        )}
      </details>
    </section>
  );
}
