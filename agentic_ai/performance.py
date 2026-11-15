"""Bounded, credential-free timing for completed MCP attempts."""
from contextlib import contextmanager
import math
import time

STAGES = {
    'audit': ('Record request', 'Check database latency and SQLite writer contention.'),
    'permissions': ('Load user permissions', 'Check database latency and permission-query volume.'),
    'configuration': ('Load integration settings', 'Check configuration database/cache latency.'),
    'connection': ('Connect to MCP gateway', 'Check gateway network latency, TLS and connection setup.'),
    'discovery': ('Discover allowed tools', 'Check gateway catalog size, pagination and discovery latency.'),
    'selection': ('Select tool with model', 'Check model inference latency and model/tool catalog size.'),
    'authorization': ('Validate user data access', 'Check branch directory size and permission database latency.'),
    'dispatch_audit': ('Record tool dispatch', 'Check database write latency and SQLite writer contention.'),
    'backend': ('Retrieve data through MCP', 'Inspect ContextForge, NiFi queues and Oracle query plans. This timing includes all remote work; it cannot separate NiFi from Oracle.'),
    'scope_validation': ('Verify returned access scope', 'Check response size and branch-scope validation cost.'),
    'presentation': ('Prepare report / summary', 'Check report size and any summary-model latency.'),
    'other': ('Other application work', 'Inspect request validation, response decoding and connection cleanup.'),
    'final_access': ('Recheck access before return', 'Check permission database latency.'),
}


@contextmanager
def stage(state, name):
    """Always close failed/cancelled stages; never retain inputs or exception text."""
    started = time.monotonic()
    outcome = 'completed'
    try:
        yield
    except BaseException:
        outcome = 'interrupted'
        raise
    finally:
        state.setdefault('_timings', []).append({
            'name': name, 'duration_ms': round(max(0, time.monotonic() - started) * 1000, 2),
            'status': outcome,
        })


def summary(state, total_ms, request_id, result):
    timings = list(state.get('_timings', []))[:31]
    timings.append({'name': 'other', 'duration_ms': round(max(0, total_ms - sum(item['duration_ms'] for item in timings)), 2), 'status': 'completed'})
    return {'version': 1, 'request_id': request_id, 'total_ms': total_ms,
            'outcome': 'clarification' if result.get('kind') == 'clarification' else result.get('code', 'ok'),
            'dispatched': bool(state.get('dispatched')), 'completed': bool(state.get('completed')),
            'model_calls': result.get('usage', {}).get('model_calls', 0), 'stages': timings}


def percentile(values, percent):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * percent) - 1)], 2) if values else None


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def trace_rows(trace):
    """Read persisted versioned traces defensively; old logs remain unmeasured."""
    if not isinstance(trace, dict) or trace.get('version') != 1 or not valid_number(trace.get('total_ms')):
        return []
    rows = []
    for item in trace.get('stages', [])[:32] if isinstance(trace.get('stages'), list) else []:
        if not isinstance(item, dict) or item.get('name') not in STAGES or not valid_number(item.get('duration_ms')):
            continue
        rows.append({**item, 'label': STAGES[item['name']][0],
                     'width': round(min(100, item['duration_ms'] / max(trace['total_ms'], 1) * 100), 2)})
    return rows
