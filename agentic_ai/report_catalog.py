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
    ('satellite', 'Satellite icon map', 'map', 'teal', 'Plot branch locations with icons over a coordinate grid or configured satellite imagery.'),
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


def valid_tile_url(url):
    # Tile requests come from the Superset browser. A same-origin route can
    # proxy licensed imagery without putting credentials into chart metadata.
    if not url or not all(token in url for token in ('{z}', '{x}', '{y}')):
        return False
    if any(ord(char) < 33 or ord(char) == 127 for char in url) or '\\' in url:
        return False
    try:
        target = urlsplit(url)
        if target.query or target.fragment or target.username or target.password:
            return False
        if not all(token in target.path for token in ('{z}', '{x}', '{y}')):
            return False
        if url.startswith('/'):
            return not url.startswith('//')
        return target.scheme in {'http', 'https'} and bool(target.hostname) and target.port != 0
    except ValueError:
        return False


def satellite_tiles(config=None):
    config = {} if config is None else config
    mode = config.get('MAP_BACKGROUND', 'deployment')
    if mode == 'grid':
        return {}
    if mode == 'custom':
        url = config.get('MAP_TILE_URL', '').strip()
        attribution = config.get('MAP_TILE_ATTRIBUTION', '').strip()
    else:
        url = getattr(settings, 'AGENTIC_SATELLITE_TILE_URL', '').strip()
        attribution = getattr(settings, 'AGENTIC_SATELLITE_TILE_ATTRIBUTION', '').strip()
    if (url.startswith('http:') and (config.get('PUBLIC_URL') or config.get('URL', '')).startswith('https:')):
        return {}
    if valid_tile_url(url) and attribution and len(attribution) <= 300:
        return {'tile_url': url, 'tile_attribution': attribution}
    return {}
