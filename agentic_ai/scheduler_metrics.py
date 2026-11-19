"""Aggregate scheduler metrics without user, report or recipient labels."""
from sqlite3 import OperationalError as SQLiteOperationalError

from django.db import OperationalError, ProgrammingError
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily


class SchedulerCollector:
    def collect(self):
        from .scheduled_reports import health_counts
        try:
            counts = health_counts()
        except (OperationalError, ProgrammingError, SQLiteOperationalError):
            yield GaugeMetricFamily('agentic_scheduler_metrics_available', 'Scheduler state is readable', value=0)
            return
        yield GaugeMetricFamily('agentic_scheduler_metrics_available', 'Scheduler state is readable', value=1)
        for key, value in counts.items():
            yield GaugeMetricFamily(f'agentic_scheduler_{key}', 'Agentic scheduler ' + key.replace('_', ' '), value=value)

    def describe(self):
        # Do not access the database when the Django app is initialized.
        return []


collector = SchedulerCollector()


def register():
    try:
        REGISTRY.register(collector)
    except ValueError:
        pass
