"""Persistence for diagnoses.

Append-only: each diagnosis run for an event is stored as a new document with an
incrementing `version`, and earlier versions are never modified. The events
collection is never touched from here — diagnosis is additive to an event, not a
mutation of it.
"""

from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.models import Diagnosis, DiagnosisMethod

logger = logging.getLogger(__name__)

COLLECTION_NAME = "diagnoses"
VERSION_INDEX = "uniq_event_id_version"

#: Bound on the optimistic-retry loop when allocating a version number.
MAX_VERSION_ATTEMPTS = 5


def collection() -> AsyncIOMotorCollection:
    """Return the diagnoses collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the unique (event_id, version) index. Idempotent.

    Uniqueness on the pair is what makes the append-only history safe: two
    concurrent diagnoses cannot both claim version N.
    """
    await collection().create_index(
        [("event_id", 1), ("version", DESCENDING)],
        unique=True,
        name=VERSION_INDEX,
    )
    logger.info(
        "Ensured unique index %r on %s.(event_id, version)",
        VERSION_INDEX,
        COLLECTION_NAME,
    )


async def latest_version(event_id: str) -> int:
    """Return the highest stored version for an event, or 0 if none exist."""
    document = await collection().find_one(
        {"event_id": event_id},
        {"version": 1},
        sort=[("version", DESCENDING)],
    )
    return int(document["version"]) if document else 0


async def append(
    diagnosis: Diagnosis, method: DiagnosisMethod
) -> tuple[str, int]:
    """Store a diagnosis as the next version for its event.

    Retries on a version collision rather than serialising writes, so two
    simultaneous diagnoses of one event both land, as versions N and N+1.

    Returns:
        The new document's id and its version.
    """
    payload = diagnosis.model_dump()
    payload["method"] = method

    for attempt in range(1, MAX_VERSION_ATTEMPTS + 1):
        version = await latest_version(diagnosis.event_id) + 1
        try:
            result = await collection().insert_one({**payload, "version": version})
        except DuplicateKeyError:
            logger.warning(
                "Version %d for event %s was taken (attempt %d/%d); retrying",
                version,
                diagnosis.event_id,
                attempt,
                MAX_VERSION_ATTEMPTS,
            )
            continue
        return str(result.inserted_id), version

    raise RuntimeError(
        f"Could not allocate a diagnosis version for {diagnosis.event_id!r} "
        f"after {MAX_VERSION_ATTEMPTS} attempts"
    )


async def latest_diagnosis(event_id: str) -> dict[str, Any] | None:
    """Return an event's most recent diagnosis document, or None if it has none.

    Stage 3 decides from the latest version, so it needs the whole document —
    including its `_id`, which is what a decision pins to make itself
    reproducible after a later re-diagnosis.
    """
    return await collection().find_one(
        {"event_id": event_id},
        sort=[("version", DESCENDING)],
    )


async def list_diagnoses(event_id: str | None = None) -> list[dict[str, Any]]:
    """Return stored diagnoses, newest first.

    Args:
        event_id: Optionally restrict to one event's history.
    """
    query = {"event_id": event_id} if event_id else {}
    return (
        await collection()
        .find(query)
        .sort([("diagnosed_at", DESCENDING), ("version", DESCENDING)])
        .to_list(length=None)
    )
