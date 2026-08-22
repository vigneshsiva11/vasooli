"""Stage 1 — Ingestion.

Accepts revenue-at-risk events (failed payment, abandoned checkout, overdue
invoice) and normalises them into the system's internal event shape.
"""

from app.ingestion.store import (
    COLLECTION_NAME,
    EVENT_ID_INDEX,
    count_prior_events,
    ensure_indexes,
    get_event,
    list_events,
    upsert_event,
)

__all__ = [
    "COLLECTION_NAME",
    "EVENT_ID_INDEX",
    "count_prior_events",
    "ensure_indexes",
    "get_event",
    "list_events",
    "upsert_event",
]
