"""Persistence for policy verdicts and the do-not-contact list (Stage 4).

Two collections:

* `policy_verdicts` — append-only per event, versioned exactly like diagnoses and
  decisions. Re-authorizing an event records a new verdict rather than
  overwriting the last one, because "we blocked this yesterday and allowed it
  today" is the kind of history an auditor asks about.
* `customer_opt_outs` — keyed uniquely by `customer_ref`. A stand-in for a real
  customer-preference service.

Writes only to those two. The events, diagnoses and decisions collections are
read and never modified: a verdict is additive to an event's history.

This module performs the I/O that `app.policy.engine` deliberately cannot, and
nothing here decides anything — it gathers facts, checks references, and stores
what the engine concluded.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.db import get_database
from app.decision.store import COLLECTION_NAME as DECISION_COLLECTION
from app.models import DecisionRecord
from app.models.policy import CustomerOptOut, PolicyVerdict
from app.policy.engine import PolicyContext
from app.policy.rules import is_contact_intervention

logger = logging.getLogger(__name__)

COLLECTION_NAME = "policy_verdicts"
OPT_OUT_COLLECTION_NAME = "customer_opt_outs"

VERSION_INDEX = "uniq_event_id_version"
OPT_OUT_INDEX = "uniq_customer_ref"
#: Supports the prior-authorized-contact lookup, which runs on every authorize.
AUTHORIZED_INDEX = "event_id_verdict_evaluated_at"

#: Bound on the optimistic-retry loop when allocating a version number.
MAX_VERSION_ATTEMPTS = 5


class DecisionReferenceError(ValueError):
    """Base for verdicts whose referenced decision does not hold up."""


class DanglingDecisionReference(DecisionReferenceError):
    """Raised when a verdict points at a decision that does not exist as claimed."""


class StaleDecisionReference(DecisionReferenceError):
    """Raised when a verdict points at a superseded decision.

    Distinct from dangling, because the failure mode is different and worse: the
    document exists and validates, so the verdict would look perfectly sound
    while granting permission for a recommendation that has since been replaced.
    """


def collection() -> AsyncIOMotorCollection:
    """Return the policy verdicts collection.

    Raises:
        RuntimeError: if MongoDB is not connected.
    """
    return get_database()[COLLECTION_NAME]


def opt_out_collection() -> AsyncIOMotorCollection:
    """Return the customer opt-out collection."""
    return get_database()[OPT_OUT_COLLECTION_NAME]


async def ensure_indexes() -> None:
    """Create the verdict and opt-out indexes. Idempotent."""
    await collection().create_index(
        [("event_id", ASCENDING), ("version", DESCENDING)],
        unique=True,
        name=VERSION_INDEX,
    )
    await collection().create_index(
        [("event_id", ASCENDING), ("verdict", ASCENDING), ("evaluated_at", DESCENDING)],
        name=AUTHORIZED_INDEX,
    )
    await opt_out_collection().create_index(
        [("customer_ref", ASCENDING)],
        unique=True,
        name=OPT_OUT_INDEX,
    )
    logger.info(
        "Ensured indexes %r/%r on %s and %r on %s",
        VERSION_INDEX,
        AUTHORIZED_INDEX,
        COLLECTION_NAME,
        OPT_OUT_INDEX,
        OPT_OUT_COLLECTION_NAME,
    )


async def latest_version(event_id: str) -> int:
    """Return the highest stored verdict version for an event, or 0 if none."""
    document = await collection().find_one(
        {"event_id": event_id},
        {"version": 1},
        sort=[("version", DESCENDING)],
    )
    return int(document["version"]) if document else 0


# ---------------------------------------------------------------------------
# The do-not-contact list.
# ---------------------------------------------------------------------------


async def add_opt_out(opt_out: CustomerOptOut) -> bool:
    """Record a customer as do-not-contact.

    Returns:
        True if newly added, False if the customer was already on the list. The
        original `opted_out_at` is preserved on a repeat call, since when consent
        was withdrawn is a fact and re-submitting the request does not change it.
    """
    try:
        await opt_out_collection().insert_one(opt_out.model_dump())
    except DuplicateKeyError:
        logger.info("Customer %s was already opted out", opt_out.customer_ref)
        return False
    return True


async def is_opted_out(customer_ref: str) -> bool:
    """Whether this customer is on the do-not-contact list."""
    document = await opt_out_collection().find_one(
        {"customer_ref": customer_ref}, {"_id": 1}
    )
    return document is not None


async def list_opt_outs() -> list[dict[str, Any]]:
    """Return every opted-out customer, most recent first."""
    return (
        await opt_out_collection()
        .find({}, {"_id": 0})
        .sort([("opted_out_at", DESCENDING)])
        .to_list(length=None)
    )


# ---------------------------------------------------------------------------
# Gathering the facts the pure engine needs.
# ---------------------------------------------------------------------------


async def prior_authorized_contacts(event_id: str) -> tuple[int, Any]:
    """Count authorized contact-type verdicts for an event, and time the latest.

    Scope is ratified as per `event_id` across ALL decision and diagnosis
    versions: the cap protects a person from being chased repeatedly about one
    debt, and re-diagnosing the same failure does not reset how many messages they
    have already received.

    Only `authorized` verdicts count. A blocked or review-pending verdict never
    reached execution, so it did not consume a contact.

    Whether a verdict was a contact is a property of the *decision* it authorized,
    not of the verdict — so this joins rather than denormalising the intervention
    onto `PolicyVerdict`, which holds permission facts only.

    Returns:
        (count, timestamp of the most recent) with timestamp None when count is 0.
    """
    verdicts = (
        await collection()
        .find(
            {"event_id": event_id, "verdict": "authorized"},
            {"decision_id": 1, "evaluated_at": 1, "version": 1},
        )
        .to_list(length=None)
    )
    if not verdicts:
        return 0, None

    decision_ids = {ObjectId(verdict["decision_id"]) for verdict in verdicts}
    decisions = (
        await get_database()[DECISION_COLLECTION]
        .find({"_id": {"$in": list(decision_ids)}}, {"recommended_intervention": 1})
        .to_list(length=None)
    )
    contact_decisions = {
        str(document["_id"])
        for document in decisions
        if is_contact_intervention(document["recommended_intervention"])
    }

    contacts = [
        verdict for verdict in verdicts if verdict["decision_id"] in contact_decisions
    ]
    if not contacts:
        return 0, None

    latest = max(verdict["evaluated_at"] for verdict in contacts)
    return len(contacts), latest


async def gather_context(
    *, decision: DecisionRecord, customer_ref: str, now: Any = None
) -> PolicyContext:
    """Read the world facts policy needs, then hand them to the pure engine.

    Every query here is a read. Assembling the context is the only place Stage 4
    touches the database before a verdict exists.
    """
    opted_out = await is_opted_out(customer_ref)
    contacts, last_contact = await prior_authorized_contacts(decision.event_id)

    kwargs: dict[str, Any] = {
        "customer_ref": customer_ref,
        "customer_opted_out": opted_out,
        "prior_authorized_contacts": contacts,
        "last_authorized_contact_at": last_contact,
    }
    if now is not None:
        kwargs["now"] = now
    return PolicyContext(**kwargs)


# ---------------------------------------------------------------------------
# Writing a verdict.
# ---------------------------------------------------------------------------


async def _assert_decision_is_current(verdict: PolicyVerdict) -> None:
    """Verify the referenced decision exists, matches, and is not superseded.

    Pydantic can check that `decision_id` is shaped like an ObjectId; only the
    database knows whether that document exists, belongs to this event, is the
    claimed version, and is still the current recommendation. So the referential
    guard lives here, at the write boundary, where it cannot be skipped.

    The staleness check is stricter than the equivalent guard in Stage 3, which
    lets a decision pin any diagnosis version. Authorization has to be stricter:
    a decision may reference a superseded explanation as a historical fact, but
    granting permission for a superseded recommendation would authorize an action
    the pipeline no longer recommends.
    """
    try:
        object_id = ObjectId(verdict.decision_id)
    except InvalidId as exc:  # pragma: no cover - the model's pattern precedes this
        raise DanglingDecisionReference(
            f"decision_id {verdict.decision_id!r} is not a valid ObjectId"
        ) from exc

    decisions = get_database()[DECISION_COLLECTION]
    document = await decisions.find_one(
        {"_id": object_id}, {"event_id": 1, "version": 1, "recommended_intervention": 1}
    )
    if document is None:
        raise DanglingDecisionReference(
            f"No decision with id {verdict.decision_id!r} exists; refusing to "
            "store a verdict that authorizes nothing identifiable"
        )
    if document["event_id"] != verdict.event_id:
        raise DanglingDecisionReference(
            f"Decision {verdict.decision_id!r} belongs to event "
            f"{document['event_id']!r}, not {verdict.event_id!r}"
        )
    if int(document["version"]) != verdict.decision_version:
        raise DanglingDecisionReference(
            f"Decision {verdict.decision_id!r} is version {document['version']}, "
            f"not {verdict.decision_version}"
        )

    newest = await decisions.find_one(
        {"event_id": verdict.event_id},
        {"version": 1},
        sort=[("version", DESCENDING)],
    )
    if newest is not None and int(newest["version"]) > verdict.decision_version:
        raise StaleDecisionReference(
            f"Decision v{verdict.decision_version} for event "
            f"{verdict.event_id!r} has been superseded by v{newest['version']}; "
            "refusing to authorize a recommendation that is no longer current"
        )


async def append(verdict: PolicyVerdict) -> tuple[str, int]:
    """Store a verdict as the next version for its event.

    Returns:
        The new document's id and its version.

    Raises:
        DanglingDecisionReference: if the referenced decision is missing, or
            belongs to a different event or version.
        StaleDecisionReference: if the referenced decision has been superseded.
    """
    await _assert_decision_is_current(verdict)

    payload = verdict.model_dump()

    for attempt in range(1, MAX_VERSION_ATTEMPTS + 1):
        version = await latest_version(verdict.event_id) + 1
        try:
            result = await collection().insert_one({**payload, "version": version})
        except DuplicateKeyError:
            logger.warning(
                "Verdict version %d for event %s was taken (attempt %d/%d); retrying",
                version,
                verdict.event_id,
                attempt,
                MAX_VERSION_ATTEMPTS,
            )
            continue
        return str(result.inserted_id), version

    raise RuntimeError(
        f"Could not allocate a verdict version for {verdict.event_id!r} after "
        f"{MAX_VERSION_ATTEMPTS} attempts"
    )


async def list_verdicts(
    event_id: str | None = None,
    *,
    history: bool = False,
    verdict: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored verdicts, newest first.

    Args:
        event_id: Optionally restrict to one event.
        history: When False (the default) only the latest verdict per event is
            returned, since that is the current authorization state. When True,
            every version is returned so a changed verdict can be compared with
            what it replaced.
        verdict: Optionally restrict to one verdict value.
    """
    query: dict[str, Any] = {}
    if event_id:
        query["event_id"] = event_id
    if verdict:
        query["verdict"] = verdict

    if history:
        return (
            await collection()
            .find(query)
            .sort([("evaluated_at", DESCENDING), ("version", DESCENDING)])
            .to_list(length=None)
        )

    pipeline: list[dict[str, Any]] = []
    if query:
        pipeline.append({"$match": query})
    pipeline += [
        {"$sort": {"event_id": ASCENDING, "version": DESCENDING}},
        {"$group": {"_id": "$event_id", "document": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$document"}},
        {"$sort": {"evaluated_at": DESCENDING, "event_id": ASCENDING}},
    ]
    return await collection().aggregate(pipeline).to_list(length=None)
