import signal
from threading import Event

from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from agentic_ai.scheduled_reports import process_due


class Command(BaseCommand):
    help = 'Process due Agentic report schedules without retrying dispatched work.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')

    def handle(self, *args, **options):
        stopped = Event()

        def stop(signum, frame):
            stopped.set()

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            while not stopped.is_set():
                close_old_connections()
                try:
                    worked = process_due(stopped.is_set)
                except OperationalError:
                    if not options['watch']:
                        raise
                    worked = False
                if not options['watch']:
                    return
                if not worked:
                    stopped.wait(10)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
