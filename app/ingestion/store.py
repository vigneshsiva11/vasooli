"""Persistence for ingested revenue-at-risk events.

Owns the `events` collection: its indexes and the read/write operations the
ingestion routes delegate to. Later stages should read events through this module
rather than reaching for the collection directly.
"""

from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.models import RevenueEvent

logger = logging.getLogger(__name__)

COLLECTION_NAME = "events"
EVENT_ID_INDEX = "uniq_event_id"

#: Fields the pipeline owns once an event has been ingested. Re-ingesting the same
#: `event_id` must not reset them, or a re-delivered webhook would rewind an event
#: that has already been diagnosed or recovered back to square one.
INSERT_ONLY_FIELDS = ("created_at", "status")


def collection() -> AsyncIOMotorCollection:
    """Return the events collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the unique index on `event_id`.

    Idempotent — MongoDB treats re-creating an identical index as a no-op. This is
    the invariant that makes `upsert_event` safe: without it, two concurrent
    inserts could both land.
    """
    await collection().create_index("event_id", unique=True, name=EVENT_ID_INDEX)
    logger.info(
        "Ensured unique index %r on %s.event_id", EVENT_ID_INDEX, COLLECTION_NAME
    )


async def upsert_event(event: RevenueEvent) -> tuple[str, bool]:
    """Insert an event, or refresh the existing one with the same `event_id`.

    Upstream systems re-deliver: a failed-payment webhook can fire twice for one
    payment. Treating `event_id` as the natural key means a repeat delivery updates
    the event in place instead of creating a second one that would be diagnosed and
    actioned independently.

    Returns:
        The stored document's id, and whether this call created it.
    """
    payload = event.model_dump()
    insert_only = {field: payload.pop(field) for field in INSERT_ONLY_FIELDS}
    update = {"$set": payload, "$setOnInsert": insert_only}
    query = {"event_id": event.event_id}

    try:
        result = await collection().update_one(query, update, upsert=True)
    except DuplicateKeyError:
        # A concurrent insert won the race between our upsert's lookup and write.
        # The document now exists, so the same update applies cleanly as a match.
        logger.warning(
            "Concurrent insert for event_id %r; retrying as update", event.event_id
        )
        result = await collection().update_one(query, update, upsert=True)

    if result.upserted_id is not None:
        return str(result.upserted_id), True

    existing: dict[str, Any] | None = await collection().find_one(query, {"_id": 1})
    if existing is None:  # pragma: no cover - would mean a delete raced this write
        raise RuntimeError(f"Event {event.event_id!r} vanished mid-upsert")
    return str(existing["_id"]), False


async def list_events() -> list[dict[str, Any]]:
    """Return every stored event document, newest first."""
    return await collection().find().sort("created_at", -1).to_list(length=None)


async def get_event(event_id: str) -> dict[str, Any] | None:
    """Return one event document by its `event_id`, or None if absent."""
    return await collection().find_one({"event_id": event_id})


async def count_prior_events(customer_ref: str, before: Any, exclude_event_id: str) -> int:
    """Count earlier at-risk events for a customer.

    Used as supporting evidence during diagnosis — a third failure in a row reads
    differently from a first. Scoped to events created strictly before the one being
    diagnosed so a diagnosis does not shift as later events arrive.
    """
    return await collection().count_documents(
        {
            "customer_ref": customer_ref,
            "created_at": {"$lt": before},
            "event_id": {"$ne": exclude_event_id},
        }
    )
