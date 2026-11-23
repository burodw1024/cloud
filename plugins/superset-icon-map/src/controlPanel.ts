// SPDX-License-Identifier: Apache-2.0
import { t, validateNonEmpty } from '@superset-ui/core';
import { ControlConfig, ControlPanelConfig } from '@superset-ui/chart-controls';
import { ICONS } from './model';

function column(
  label: string,
  required = false,
  multi = false,
): ControlConfig<'SelectControl'> {
  return {
    type: 'SelectControl',
    label,
    multi,
    clearable: !required,
    renderTrigger: false,
    validators: required ? [validateNonEmpty] : [],
    description: t(
      'Choose a physical dataset column. No geocoding or inferred coordinates.',
    ),
    mapStateToProps: ({ datasource }) => ({
      choices: (datasource?.columns || []).map(item => [
        item.column_name,
        ('verbose_name' in item && item.verbose_name) || item.column_name,
      ]),
    }),
  };
}

const controlPanel: ControlPanelConfig = {
  controlPanelSections: [
    {
      label: t('Location data'),
      expanded: true,
      controlSetRows: [
        [
          { name: 'latitude_column', config: column(t('Latitude'), true) },
          { name: 'longitude_column', config: column(t('Longitude'), true) },
        ],
        [{ name: 'label_column', config: column(t('Location label')) }],
        [
          { name: 'category_column', config: column(t('Category / color')) },
          { name: 'size_column', config: column(t('Size value')) },
        ],
        [{ name: 'icon_column', config: column(t('Icon column')) }],
        [
          {
            name: 'tooltip_columns',
            config: column(t('Tooltip fields (up to 12)'), false, true),
          },
        ],
        ['adhoc_filters'],
        [
          {
            name: 'row_limit',
            config: {
              type: 'TextControl',
              label: t('Row limit'),
              default: 1000,
              isInt: true,
              description: t(
                'Maximum 5,000 locations. Apply filters to inspect additional records.',
              ),
            },
          },
        ],
      ],
    },
    {
      label: t('Map appearance'),
      expanded: true,
      controlSetRows: [
        [
          {
            name: 'icon_type',
            config: {
              type: 'SelectControl',
              label: t('Default icon'),
              default: 'pin',
              clearable: false,
              renderTrigger: true,
              choices: ICONS.map(value => [value, value]),
              description: t(
                'An icon column can override this with a supported icon name; HTML and image URLs are not accepted.',
              ),
            },
          },
        ],
        [
          {
            name: 'marker_size',
            config: {
              type: 'TextControl',
              label: t('Minimum icon size'),
              default: 20,
              isInt: true,
              renderTrigger: true,
            },
          },
          {
            name: 'max_marker_size',
            config: {
              type: 'TextControl',
              label: t('Maximum icon size'),
              default: 44,
              isInt: true,
              renderTrigger: true,
            },
          },
        ],
        [
          {
            name: 'fixed_color',
            config: {
              type: 'TextControl',
              label: t('Single color (#RRGGBB)'),
              // Fixed user-configurable data color, independent of the application theme.
              // eslint-disable-next-line theme-colors/no-literal-colors
              default: '#0072b2',
              renderTrigger: true,
            },
          },
        ],
        [
          {
            name: 'show_legend',
            config: {
              type: 'CheckboxControl',
              label: t('Show category legend'),
              default: true,
              renderTrigger: true,
            },
          },
          {
            name: 'show_labels',
            config: {
              type: 'CheckboxControl',
              label: t('Show location labels'),
              default: false,
              renderTrigger: true,
            },
          },
        ],
      ],
    },
    {
      label: t('Map background'),
      expanded: false,
      controlSetRows: [
        [
          {
            name: 'show_bangladesh_districts',
            config: {
              type: 'CheckboxControl',
              label: t('Show Bangladesh districts'),
              default: false,
              renderTrigger: true,
              description: t(
                'Show the 64 district boundaries. Zoom in to see district names, or hover over a district.',
              ),
            },
          },
        ],
        [
          {
            name: 'tile_url',
            config: {
              type: 'TextControl',
              label: t('XYZ tile URL'),
              default: '',
              renderTrigger: true,
              description: t(
                'Leave empty for the offline coordinate grid. Use an approved internal HTTP(S) URL containing {z}, {x} and {y}. The tile service receives the map viewport.',
              ),
            },
          },
        ],
        [
          {
            name: 'tile_attribution',
            config: {
              type: 'TextControl',
              label: t('Tile provider attribution'),
              default: '',
              renderTrigger: true,
            },
          },
        ],
      ],
    },
  ],
};
export default controlPanel;
