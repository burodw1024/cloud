// SPDX-License-Identifier: Apache-2.0
import { QueryFormData } from '@superset-ui/core';

export type IconMapFormData = QueryFormData & {
  latitude_column: string;
  longitude_column: string;
  label_column?: string;
  category_column?: string;
  size_column?: string;
  icon_column?: string;
  tooltip_columns?: string[];
  icon_type?: string;
  marker_size?: number;
  max_marker_size?: number;
  fixed_color?: string;
  tile_url?: string;
  tile_attribution?: string;
  show_legend?: boolean;
  show_labels?: boolean;
  show_bangladesh_districts?: boolean;
};
