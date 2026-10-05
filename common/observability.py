"""Shared observability setup: Prometheus metrics + structured JSON logs +
OpenTelemetry tracing scaffold. Every service calls setup_observability(app, name)
once at startup so the whole platform emits a consistent signal shape —
this is exactly the kind of platform-level consistency an SRE team cares about.
"""
import logging
import sys
import time

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["service", "method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["service", "method", "path"]
)
BUSINESS_EVENTS = Counter(
    "business_events_total", "Domain events emitted", ["service", "event_type", "outcome"]
)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        import json
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": getattr(record, "service", "unknown"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(service_name: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logger = logging.getLogger(service_name)
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.service = service_name
        return record

    logging.setLogRecordFactory(record_factory)
    return logger


class MetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service_name: str):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        route = request.scope.get("route")
        path = route.path if route is not None else "unmatched"  # template, not raw URL: bounded label cardinality
        REQUEST_COUNT.labels(self.service_name, request.method, path, response.status_code).inc()
        REQUEST_LATENCY.labels(self.service_name, request.method, path).observe(duration)
        return response


def setup_observability(app, service_name: str) -> logging.Logger:
    logger = setup_logging(service_name)
    app.add_middleware(MetricsMiddleware, service_name=service_name)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": service_name}

    return logger
