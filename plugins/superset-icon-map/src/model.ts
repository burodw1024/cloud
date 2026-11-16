// SPDX-License-Identifier: Apache-2.0
export const MAX_POINTS = 5000;
export const MERCATOR_LIMIT = 85.05112878;
export const ICONS = [
  'pin',
  'branch',
  'person',
  'warning',
  'circle',
  'square',
  'diamond',
  'triangle',
] as const;
export type IconName = (typeof ICONS)[number];
export type DataRow = Record<string, unknown>;

export interface MapOptions {
  latitudeColumn: string;
  longitudeColumn: string;
  labelColumn?: string;
  categoryColumn?: string;
  sizeColumn?: string;
  iconColumn?: string;
  tooltipColumns?: string[];
  iconType?: string;
  markerSize?: number;
  maxMarkerSize?: number;
  fixedColor?: string;
  rowLimit?: number;
}

export interface MapPoint {
  key: number;
  latitude: number;
  longitude: number;
  displayLongitude: number;
  label: string;
  category: string;
  value: number | null;
  color: string;
  icon: IconName;
  size: number;
  details: [string, string][];
}

export interface MapModel {
  points: MapPoint[];
  invalidRows: number;
  omittedRows: number;
  atLimit: boolean;
  categories: { name: string; color: string; count: number }[];
}

const PALETTE = [
  '#0072b2',
  '#d55e00',
  '#009e73',
  '#cc79a7',
  '#8b6508',
  '#6a51a3',
  '#087e8b',
  '#b34261',
];

export function numeric(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (
    typeof value === 'string' &&
    !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())
  )
    return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function text(value: unknown, limit = 500): string {
  if (value === null || value === undefined) return 'Not provided';
  const valueText =
    typeof value === 'object' ? JSON.stringify(value) : String(value);
  return valueText.length > limit ? `${valueText.slice(0, limit)}…` : valueText;
}

export function bounded(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
): number {
  const number = numeric(value);
  return number === null ? fallback : Math.max(min, Math.min(max, number));
}

export function color(value: unknown): string {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)
    ? value
    : '#0072b2';
}

export function icon(value: unknown, fallback: unknown = 'pin'): IconName {
  if (
    typeof value === 'string' &&
    (ICONS as readonly string[]).includes(value.toLowerCase())
  )
    return value.toLowerCase() as IconName;
  return typeof fallback === 'string' &&
    (ICONS as readonly string[]).includes(fallback)
    ? (fallback as IconName)
    : 'pin';
}

export function categoryColor(category: string): string {
  let hash = 0;
  for (let index = 0; index < category.length; index += 1)
    hash = (Math.imul(hash, 31) + category.charCodeAt(index)) | 0;
  return PALETTE[(hash >>> 0) % PALETTE.length];
}

export function selectedColumns(options: MapOptions): string[] {
  const tooltips = Array.isArray(options.tooltipColumns)
    ? options.tooltipColumns.slice(0, 12)
    : [];
  return [
    ...new Set(
      [
        options.latitudeColumn,
        options.longitudeColumn,
        options.labelColumn,
        options.categoryColumn,
        options.sizeColumn,
        options.iconColumn,
        ...tooltips,
      ].filter(
        (value): value is string =>
          typeof value === 'string' && value.trim().length > 0,
      ),
    ),
  ];
}

// Use the smallest circular longitude interval, including points across the date line.
export function unwrapLongitudes(longitudes: number[]): number[] {
  if (!longitudes.length) return [];
  const normalized = longitudes.map(value => ((value % 360) + 360) % 360);
  const sorted = [...normalized].sort((left, right) => left - right);
  let gap = -1;
  let start = sorted[0];
  sorted.forEach((value, index) => {
    const next =
      sorted[(index + 1) % sorted.length] +
      (index === sorted.length - 1 ? 360 : 0);
    if (next - value > gap) {
      gap = next - value;
      start = next % 360;
    }
  });
  const unwrapped = normalized.map(value =>
    value < start ? value + 360 : value,
  );
  const center = (Math.min(...unwrapped) + Math.max(...unwrapped)) / 2;
  return center > 180 ? unwrapped.map(value => value - 360) : unwrapped;
}

export function prepareMap(rows: DataRow[], options: MapOptions): MapModel {
  const limit = Math.floor(bounded(options.rowLimit, 1000, 1, MAX_POINTS));
  const minimum = bounded(options.markerSize, 20, 8, 48);
  const maximum = Math.max(minimum, bounded(options.maxMarkerSize, 44, 8, 72));
  let invalidRows = 0;
  const points: MapPoint[] = [];
  const fields = selectedColumns(options);
  rows.slice(0, limit).forEach((row, index) => {
    const latitude = numeric(row[options.latitudeColumn]);
    const longitude = numeric(row[options.longitudeColumn]);
    if (
      latitude === null ||
      longitude === null ||
      Math.abs(latitude) > MERCATOR_LIMIT ||
      Math.abs(longitude) > 180
    ) {
      invalidRows += 1;
      return;
    }
    const category = options.categoryColumn
      ? text(row[options.categoryColumn], 120)
      : 'Locations';
    const value = options.sizeColumn ? numeric(row[options.sizeColumn]) : null;
    points.push({
      key: index,
      latitude,
      longitude,
      displayLongitude: longitude,
      label: options.labelColumn
        ? text(row[options.labelColumn], 200)
        : `${latitude}, ${longitude}`,
      category,
      value,
      color: options.categoryColumn
        ? categoryColor(category)
        : color(options.fixedColor),
      icon: icon(
        options.iconColumn ? row[options.iconColumn] : options.iconType,
        options.iconType,
      ),
      size: minimum,
      details: fields.map(field => [field, text(row[field])]),
    });
  });
  const maxValue = Math.max(
    0,
    ...points.map(point => Math.abs(point.value || 0)),
  );
  const longitudes = unwrapLongitudes(points.map(point => point.longitude));
  points.forEach((item, index) => {
    const point = item;
    point.displayLongitude = longitudes[index];
    // Area, rather than radius, scales with the absolute value. Original values remain in tooltips.
    point.size =
      point.value === null || maxValue === 0
        ? minimum
        : Math.sqrt(
            minimum ** 2 +
              ((maximum ** 2 - minimum ** 2) * Math.abs(point.value)) /
                maxValue,
          );
  });
  const categories = new Map<
    string,
    { name: string; color: string; count: number }
  >();
  points.forEach(point => {
    const category = categories.get(point.category) || {
      name: point.category,
      color: point.color,
      count: 0,
    };
    category.count += 1;
    categories.set(point.category, category);
  });
  return {
    points,
    invalidRows,
    omittedRows: Math.max(0, rows.length - limit),
    atLimit: rows.length >= limit,
    categories: [...categories.values()].sort((left, right) =>
      left.name.localeCompare(right.name),
    ),
  };
}

export function tileUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '';
  const candidate = value.trim();
  if (
    !candidate.includes('{z}') ||
    !candidate.includes('{x}') ||
    !candidate.includes('{y}')
  )
    return '';
  if (
    !(candidate.startsWith('/') && !candidate.startsWith('//')) &&
    !/^https?:\/\//i.test(candidate)
  )
    return '';
  try {
    const parsed = new URL(
      candidate.replace(/\{[xyzs]\}/g, '0'),
      'https://superset.invalid',
    );
    return ['http:', 'https:'].includes(parsed.protocol) &&
      !parsed.username &&
      !parsed.password
      ? candidate
      : '';
  } catch {
    return '';
  }
}
