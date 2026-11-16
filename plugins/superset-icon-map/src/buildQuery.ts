// SPDX-License-Identifier: Apache-2.0
import { buildQueryContext } from '@superset-ui/core';
import { bounded, MAX_POINTS, selectedColumns } from './model';
import { IconMapFormData } from './types';

export default function buildQuery(formData: IconMapFormData) {
  if (
    typeof formData.latitude_column !== 'string' ||
    !formData.latitude_column.trim() ||
    typeof formData.longitude_column !== 'string' ||
    !formData.longitude_column.trim()
  ) {
    throw new Error(
      'Choose latitude and longitude columns before querying the map.',
    );
  }
  const columns = selectedColumns({
    latitudeColumn: formData.latitude_column,
    longitudeColumn: formData.longitude_column,
    labelColumn: formData.label_column,
    categoryColumn: formData.category_column,
    sizeColumn: formData.size_column,
    iconColumn: formData.icon_column,
    tooltipColumns: formData.tooltip_columns,
  });
  return buildQueryContext(formData, baseQuery => [
    {
      ...baseQuery,
      columns,
      metrics: [],
      row_limit: Math.floor(bounded(formData.row_limit, 1000, 1, MAX_POINTS)),
    },
  ]);
}
