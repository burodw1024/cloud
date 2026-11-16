// SPDX-License-Identifier: Apache-2.0
import { ChartMetadata, ChartPlugin, t } from '@superset-ui/core';
import buildQuery from './buildQuery';
import controlPanel from './controlPanel';
import transformProps from './transformProps';
import { IconMapFormData } from './types';
import thumbnail from './thumbnail.png';

export default class BuroIconMapPlugin extends ChartPlugin<IconMapFormData> {
  constructor() {
    super({
      metadata: new ChartMetadata({
        name: t('BURO Icon Map'),
        category: t('Map'),
        description: t(
          'Plot authorized latitude/longitude rows with vector icons, category colors, sizes and safe tooltips. Offline coordinate grid with optional on-prem XYZ tiles.',
        ),
        tags: [t('Map'), t('Geographic'), t('Categorical')],
        thumbnail,
        credits: ['https://leafletjs.com'],
      }),
      buildQuery,
      controlPanel,
      transformProps,
      loadChart: () => import('./IconMap'),
    });
  }
}
