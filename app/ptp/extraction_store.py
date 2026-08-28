"""Persistence for promise extractions (Stage 10).

Two things worth reading before the code:

* **there is no unique index on this collection, and that is deliberate.** Every
  other stage's store has one, because every other stage records a fact that can
  only be true once. An extraction is not a fact about the world — it is a record
  that an attempt was made, and two identical submissions are two attempts that both
  happened. Making them collide would erase the second one, which is exactly the
  event an auditor asking "how many times did this get resubmitted?" wants to see.
  Idempotency belongs one layer down, on the promise, where
  `uniq_event_promised_date` already provides it: a repeat submission of the same
  message writes a second extraction record and no second promise;

* **`promised_date` is stored as an ISO-8601 string**, the same as in
  `app/ptp/store.py` and for the same reason. BSON has no date type, and a bare date
  stored as midnight-UTC invents a time nobody promised and shifts across timezones.

The link to the promise is written as a second update rather than being part of the
insert, and the ordering matters. The attempt record goes in *first*, before
`create_promise` is called, so a crash between the two leaves a visible extraction
with `accepted: true` and `promise_id: null` — an orphan somebody can find — rather
than a promise with no record of the text it came from.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING

from app.db import get_database
from app.models.promise_extraction import PromiseExtraction

logger = logging.getLogger(__name__)

COLLECTION_NAME = "promise_extractions"

#: Every attempt for one event, newest first. What the audit trail reads.
EVENT_INDEX = "event_id_extracted_at"
#: Walk back from a promise to the message it was extracted from. Sparse, because
#: most refused extractions have no promise and indexing their nulls buys nothing.
PROMISE_INDEX = "promise_id_sparse"
#: Count refusals by reason without a collection scan — how often each guardrail
#: fired is the one aggregate this collection exists to be able to answer.
REFUSAL_INDEX = "accepted_refusal_reason"


def collection() -> AsyncIOMotorCollection:
    """Return the promise-extractions collection."""
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the indexes this stage relies on. Idempotent.

    None of them is unique. See the module docstring: an extraction attempt is not a
    fact that can only be true once.
    """
    await collection().create_index(
        [("event_id", ASCENDING), ("extracted_at", DESCENDING)],
        name=EVENT_INDEX,
    )
    await collection().create_index(
        [("promise_id", ASCENDING)],
        name=PROMISE_INDEX,
        sparse=True,
    )
    await collection().create_index(
        [("accepted", ASCENDING), ("refusal_reason", ASCENDING)],
        name=REFUSAL_INDEX,
    )
    logger.info(
        "Ensured indexes on '%s': %s, %s (sparse), %s — none unique, by design",
        COLLECTION_NAME,
        EVENT_INDEX,
        PROMISE_INDEX,
        REFUSAL_INDEX,
    )


def encode(extraction: PromiseExtraction) -> dict[str, Any]:
    """Render an extraction for storage.

    `promised_date` is already a string on this model, so unlike
    `app/ptp/store.py:encode` there is nothing to convert — the model holds the
    stored shape directly, because this record never participates in a date
    comparison and so has no reason to carry a `date`.
    """
    return extraction.model_dump()


async def insert(extraction: PromiseExtraction) -> str:
    """Persist one extraction attempt and return its document id.

    Raises nothing of its own. There is no referential guard here: the caller has
    already run `assert_event_promisable` before spending an API call, and an
    attempt record is worth keeping even for an event that turns out to be
    unpromisable — refusing to record the attempt would lose the evidence of it.
    """
    result = await collection().insert_one(encode(extraction))
    logger.info(
        "Recorded promise extraction for event %r: accepted=%s reason=%r "
        "confidence=%.2f",
        extraction.event_id,
        extraction.accepted,
        extraction.refusal_reason,
        extraction.confidence,
    )
    return str(result.inserted_id)


async def link_promise(*, extraction_id: str, promise_id: str) -> bool:
    """Record which promise an accepted extraction produced.

    Guarded on `accepted` and on `promise_id` still being null, so this cannot
    attach a promise to a refusal and cannot silently overwrite an existing link.
    Returns whether the link was written; a False is logged by the caller rather
    than raised, because a missed link is an audit gap and not a reason to undo a
    promise that legitimately exists.
    """
    result = await collection().update_one(
        {"_id": ObjectId(extraction_id), "accepted": True, "promise_id": None},
        {"$set": {"promise_id": promise_id}},
    )
    if result.matched_count:
        logger.info("Extraction %s linked to promise %s", extraction_id, promise_id)
        return True
    logger.warning(
        "Could not link extraction %s to promise %s: it is either refused or "
        "already linked",
        extraction_id,
        promise_id,
    )
    return False


async def find_by_id(extraction_id: str) -> dict[str, Any] | None:
    """Return one extraction by document id."""
    return await collection().find_one({"_id": ObjectId(extraction_id)})


async def list_extractions(
    event_id: str | None = None,
    *,
    accepted: bool | None = None,
) -> list[dict[str, Any]]:
    """Return stored extraction attempts, newest first.

    No latest-per-event collapse, unlike every other list endpoint in this project.
    That collapse exists where the newest record supersedes the older ones; here they
    accumulate instead — three attempts on one event are three separate things that
    happened, and showing only the last would hide the two refusals before it.
    """
    query: dict[str, Any] = {}
    if event_id is not None:
        query["event_id"] = event_id
    if accepted is not None:
        query["accepted"] = accepted

    cursor = collection().find(query).sort("extracted_at", DESCENDING)
    return await cursor.to_list(length=None)
