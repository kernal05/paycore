"""Outbox Publisher (ADR-003)
================
A standalone worker, not a FastAPI service — its only job is:

    poll outbox_events WHERE published_at IS NULL
    -> publish each to Kafka
    -> mark published_at

This is what actually closes the dual-write gap: ledger-service writes the
DB row and the outbox row in the same transaction, so if this process is
down for five minutes, nothing is lost — it just catches up when it comes
back. Exposes its own /metrics and /healthz on a tiny HTTP server so it
fits the same observability story as everything else.
"""
import json
import os
import sys
import threading
import time

from sqlalchemy import text
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.append("/app")
from common.db import SessionLocal
from common.kafka_utils import get_producer
from common.observability import setup_logging, BUSINESS_EVENTS
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

logger = setup_logging("outbox-publisher")

POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "1.0"))
BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "100"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(generate_latest())
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"outbox-publisher"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep stdout clean for the structured JSON logs instead


def serve_health():
    HTTPServer(("0.0.0.0", 8000), HealthHandler).serve_forever()


def publish_pending(producer):
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT id, aggregate_id, event_type, topic, payload FROM outbox_events "
                "WHERE published_at IS NULL ORDER BY id ASC LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": BATCH_SIZE},
        ).fetchall()

        if not rows:
            session.commit()
            return 0

        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
            producer.send(row.topic, key=str(row.aggregate_id), value={"event_type": row.event_type, **payload})
        producer.flush(timeout=10)

        for row in rows:
            session.execute(
                text("UPDATE outbox_events SET published_at = now() WHERE id = :id"), {"id": row.id}
            )
        session.commit()

        BUSINESS_EVENTS.labels("outbox-publisher", "events_published", "ok").inc()
        logger.info(f"Published {len(rows)} outbox events")
        return len(rows)
    except Exception:
        session.rollback()
        BUSINESS_EVENTS.labels("outbox-publisher", "publish_batch_failed", "error").inc()
        logger.exception("Failed to publish outbox batch — will retry next poll")
        return 0
    finally:
        session.close()


def main():
    threading.Thread(target=serve_health, daemon=True).start()
    producer = get_producer()
    logger.info("Outbox publisher started")
    while True:
        published = publish_pending(producer)
        if published == 0:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
