import logging
import signal
from sqlite3 import OperationalError as SQLiteOperationalError
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections
from django.utils import timezone

from agentic_ai.scheduled_reports import (
    HEARTBEAT_KEY, claim_due, process_claimed, reconcile_expired, renew_claims,
)

logger = logging.getLogger(__name__)
POLL_SECONDS = 10
WORKERS = 2


def run_job(job, stop_requested):
    close_old_connections()
    try:
        process_claimed(job, stop_requested)
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = 'Process due Agentic reports with bounded concurrency and recover expired leases.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')

    def handle(self, *args, **options):
        stopped = Event()

        def stop(signum, frame):
            stopped.set()

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if not options['watch']:
                reconcile_expired(timezone.now())
                job = claim_due()
                if job:
                    process_claimed(job, stopped.is_set)
                return
            active = {}
            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                while active or not stopped.is_set():
                    close_old_connections()
                    for job_id, (_, future) in list(active.items()):
                        if future.done():
                            del active[job_id]
                            if future.exception():
                                logger.error('Scheduled report %s could not persist its final status', job_id)
                    try:
                        now = timezone.now()
                        renew_claims(active.keys(), now)
                        reconcile_expired(now)
                        cache.set(HEARTBEAT_KEY, True, timeout=45)
                        while len(active) < WORKERS and not stopped.is_set():
                            job = claim_due(user_id for user_id, _ in active.values())
                            if not job:
                                break
                            active[job.pk] = (job.user_id, executor.submit(run_job, job, stopped.is_set))
                    except (OperationalError, SQLiteOperationalError):
                        logger.warning('Scheduler database tick failed; retrying without replaying dispatched jobs.')
                    if stopped.is_set():
                        if active:
                            # Keep leases alive while the in-flight query or send drains.
                            wait([future for _, future in active.values()], timeout=POLL_SECONDS,
                                 return_when=FIRST_COMPLETED)
                    else:
                        stopped.wait(POLL_SECONDS)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
