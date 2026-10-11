import logging
import time
import uuid

logger = logging.getLogger(__name__)

class RequestTraceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.trace_id = uuid.uuid4().hex[:12]
        start = time.perf_counter()

        logger.info("[%s] REQ_START %s %s",
                    request.trace_id,
                    request.method,
                    request.path)

        try:
            response = self.get_response(request)
            return response
        except Exception as e:
            logger.exception("[%s] REQ_FAIL err=%s",
                             request.trace_id,
                             str(e))
            raise
        finally:
            dt = time.perf_counter() - start
            logger.info("[%s] REQ_END duration=%.3fs",
                        request.trace_id,
                        dt)
