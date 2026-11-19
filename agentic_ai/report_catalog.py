"""Report designs supported by the pinned Superset 4.1 frontend."""
from urllib.parse import urlsplit

from django.conf import settings


# Key, user-facing name, visual style, accent, and data guidance.
CATALOG = (
    ('table', 'Source table', 'table', 'slate', 'Every displayed field in the original row order.'),
    ('comparison', 'Horizontal bars', 'bars', 'teal', 'Compare values across named rows.'),
    ('management', 'Management pack', 'bars', 'indigo', 'A source table and comparisons for up to four measures.'),
    ('columns', 'Vertical columns', 'columns', 'blue', 'Compare individual values across displayed rows.'),
    ('line', 'Line profile', 'line', 'indigo', 'Connect values across the displayed row sequence.'),
    ('smooth', 'Smooth line', 'line', 'violet', 'Trace the shape of values across the displayed row sequence.'),
    ('step', 'Step profile', 'step', 'amber', 'Highlight changes between consecutive displayed rows.'),
    ('area', 'Filled area', 'area', 'teal', 'Emphasize the magnitude of values across displayed rows.'),
    ('points', 'Dot plot', 'points', 'blue', 'Inspect individual values and spot outliers.'),
    ('donut', 'Donut composition', 'donut', 'rose', 'Show shares of one non-negative measure across 2–12 displayed rows.'),
    ('heatmap', 'Category heatmap', 'heatmap', 'amber', 'Compare one measure across two report categories with unique pairs.'),
    ('satellite', 'Satellite icon map', 'map', 'teal', 'Plot real coordinates over approved satellite imagery with location icons.'),
)
TEMPLATES = [(key, title) for key, title, *_ in CATALOG]
DESIGNS = {row[0]: row for row in CATALOG}

SERIES = {
    'comparison': ('echarts_timeseries_bar', 'supersetColors', {'orientation': 'horizontal', 'show_value': True}),
    'management': ('echarts_timeseries_bar', 'd3Category10', {'orientation': 'horizontal', 'show_value': True}),
    'columns': ('echarts_timeseries_bar', 'd3Category10', {'orientation': 'vertical', 'show_value': True}),
    'line': ('echarts_timeseries_line', 'd3Category10', {'markerEnabled': True}),
    'smooth': ('echarts_timeseries_smooth', 'supersetColors', {}),
    'step': ('echarts_timeseries_step', 'd3Category10', {'markerEnabled': True}),
    'area': ('echarts_timeseries_line', 'supersetColors', {'area': True, 'opacity': 0.25}),
    'points': ('echarts_timeseries_scatter', 'd3Category10', {'markerSize': 8}),
}


def satellite_tiles():
    """Return only deployment-owned settings. Tile requests originate in the browser."""
    url = getattr(settings, 'AGENTIC_SATELLITE_TILE_URL', '').strip()
    attribution = getattr(settings, 'AGENTIC_SATELLITE_TILE_ATTRIBUTION', '').strip()
    try:
        target = urlsplit(url)
        valid = (target.scheme in {'http', 'https'} and target.hostname and not target.username
                 and not target.password and not target.query and not target.fragment
                 and all(token in target.path for token in ('{z}', '{x}', '{y}')))
    except ValueError:
        valid = False
    if valid and attribution and len(attribution) <= 300:
        return {'tile_url': url, 'tile_attribution': attribution}
    return {}
