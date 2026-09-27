"""Thin wrapper around kafka-python so every service publishes/consumes
events the same, predictable way: JSON payloads, explicit topics, and a
producer that retries and waits for broker acks (acks='all') so we never
silently drop a financial event.
"""
import json
import os
import time
import logging

from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import NoBrokersAvailable

BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
logger = logging.getLogger("kafka_utils")

TOPIC_TRANSACTIONS_CREATED = "transactions.created"
TOPIC_FRAUD_SCORED = "transactions.fraud_scored"
TOPIC_LEDGER_POSTED = "ledger.posted"
TOPIC_RECONCILIATION_MISMATCH = "reconciliation.mismatch"


def retry_topic(base_topic: str) -> str:
    return f"{base_topic}.retry"


def dlq_topic(base_topic: str) -> str:
    return f"{base_topic}.dlq"


def send_to_dlq(producer: KafkaProducer, base_topic: str, original_event: dict, error: str) -> None:
    """Reconciliation-service's consumer deliberately does NOT retry/DLQ —
    its failure mode already has a safety net (the /reconcile/run-batch
    CronJob re-picks up anything the event-driven path missed), which is a
    simpler and equally correct design for that one consumer. This helper
    is the primitive a *different* consumer without that kind of built-in
    safety net (e.g. a future notifications/settlement consumer) would use
    to implement the full retry-N-times-then-DLQ pattern from the doc:
    catch, republish to `<topic>.retry` up to a retry-count limit carried
    in the payload, then to `<topic>.dlq` for manual investigation/replay.
    """
    producer.send(dlq_topic(base_topic), value={"original_event": original_event, "error": error})
    producer.flush(timeout=5)


def get_producer(retries: int = 10, delay_seconds: float = 3.0) -> KafkaProducer:
    """Create a producer, retrying while Kafka finishes starting up
    (matters a lot in docker-compose, where startup order isn't guaranteed).
    """
    last_err = None
    for attempt in range(retries):
        try:
            return KafkaProducer(
                bootstrap_servers=BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=5,
                linger_ms=10,
            )
        except NoBrokersAvailable as e:
            last_err = e
            logger.warning("Kafka not ready yet (attempt %s/%s), retrying...", attempt + 1, retries)
            time.sleep(delay_seconds)
    raise RuntimeError(f"Could not connect to Kafka after {retries} attempts") from last_err


def get_consumer(topic: str, group_id: str, retries: int = 10, delay_seconds: float = 3.0) -> KafkaConsumer:
    last_err = None
    for attempt in range(retries):
        try:
            return KafkaConsumer(
                topic,
                bootstrap_servers=BOOTSTRAP_SERVERS,
                group_id=group_id,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="earliest",
                enable_auto_commit=True,
            )
        except NoBrokersAvailable as e:
            last_err = e
            logger.warning("Kafka not ready yet (attempt %s/%s), retrying...", attempt + 1, retries)
            time.sleep(delay_seconds)
    raise RuntimeError(f"Could not connect to Kafka after {retries} attempts") from last_err
