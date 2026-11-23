// SPDX-License-Identifier: Apache-2.0
import { ChartProps } from '@superset-ui/core';
import { IconMapProps } from './IconMap';
import { bounded, DataRow, MAX_POINTS } from './model';
import { IconMapFormData } from './types';

export default function transformProps(chartProps: ChartProps): IconMapProps {
  const form = chartProps.rawFormData as IconMapFormData;
  const data = chartProps.queriesData[0]?.data || [];
  return {
    width: Math.max(180, chartProps.width),
    height: Math.max(180, chartProps.height),
    rows: (data as unknown[]).filter(
      row => row !== null && typeof row === 'object' && !Array.isArray(row),
    ) as DataRow[],
    latitudeColumn: form.latitude_column || '',
    longitudeColumn: form.longitude_column || '',
    labelColumn: form.label_column,
    categoryColumn: form.category_column,
    sizeColumn: form.size_column,
    iconColumn: form.icon_column,
    tooltipColumns: form.tooltip_columns || [],
    iconType: form.icon_type || 'pin',
    markerSize: form.marker_size,
    maxMarkerSize: form.max_marker_size,
    fixedColor: form.fixed_color,
    rowLimit: bounded(form.row_limit, 1000, 1, MAX_POINTS),
    tileTemplate: form.tile_url || '',
    tileAttribution: form.tile_attribution || '',
    showLegend: form.show_legend !== false,
    showLabels: form.show_labels === true,
    showBangladeshDistricts: form.show_bangladesh_districts === true,
  };
}
