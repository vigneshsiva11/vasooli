"""The one place Stage 7 touches the database — reads only.

Every query in this stage lives in this module, which is what makes "read-only"
checkable rather than asserted: there is exactly one file to look at, and the only
Motor method it calls is `find`. No `insert_*`, no `update_*`, no `delete_*`, no
`replace_*`, no `create_index`, no `find_one_and_*`, no aggregation `$out`/`$merge`.
`app/metrics/verify_readonly.py` asserts that mechanically.

Why whole collections in Python rather than `$group` pipelines: the aggregations
this stage needs are latest-version-per-event groupings joined across seven
collections, and expressed as Mongo stages they become unreviewable. The dataset is
a few hundred documents. If `events` passes roughly 50k the tradeoff inverts and
these should move server-side — that is the threshold, stated so the decision is
revisitable rather than forgotten.

One snapshot per request, loaded once and shared by every aggregation, so the four
Part A figures cannot disagree with each other by having read the database at four
different moments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db import get_database

#: Collection names, taken from each stage's own store module rather than restated,
#: so a rename over there cannot leave this stage silently reading nothing.
from app.decision.store import COLLECTION_NAME as DECISIONS
from app.diagnosis.store import COLLECTION_NAME as DIAGNOSES
from app.execution.store import COLLECTION_NAME as EXECUTIONS
from app.ingestion.store import COLLECTION_NAME as EVENTS
from app.policy.store import COLLECTION_NAME as VERDICTS
from app.ptp.store import COLLECTION_NAME as PROMISES
from app.webhooks.store import COLLECTION_NAME as VERIFICATIONS

Document = dict[str, Any]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Snapshot:
    """Every document this stage can see, read at one moment.

    Frozen, and every collection is a tuple, so an aggregation cannot mutate the
    data another aggregation is about to read. This is the read-only property held
    at the object level rather than only at the query level.
    """

    events: tuple[Document, ...]
    diagnoses: tuple[Document, ...]
    decisions: tuple[Document, ...]
    verdicts: tuple[Document, ...]
    executions: tuple[Document, ...]
    verifications: tuple[Document, ...]
    promises: tuple[Document, ...]
    read_at: datetime

    # -- lookups the aggregations share ------------------------------------

    def events_by_id(self) -> dict[str, Document]:
        """Every event, keyed by `event_id`."""
        return {document["event_id"]: document for document in self.events}

    def decisions_by_object_id(self) -> dict[str, Document]:
        """Every decision, keyed by its stringified `_id`.

        Needed because a `PolicyVerdict` records `decision_id` and carries no
        intervention of its own — "which intervention was authorized" is only
        answerable through this join.
        """
        return {str(document["_id"]): document for document in self.decisions}

    def executions_by_object_id(self) -> dict[str, Document]:
        """Every execution, keyed by its stringified `_id`.

        The join a `VerificationRecord` needs: it names an `execution_id`, and the
        intervention that earned the recovery is on the execution.
        """
        return {str(document["_id"]): document for document in self.executions}


async def load_snapshot(event_id: str | None = None) -> Snapshot:
    """Read every collection this stage aggregates over.

    Args:
        event_id: when given, restrict every collection to that event. Used by the
            audit trail, which needs one event's history and not the whole database.

    Sorted at read time — by `version` where a collection is versioned, by its own
    timestamp otherwise — so "chronological order" is a property of the data every
    consumer receives rather than something each one has to remember to impose.
    """
    database = get_database()
    scope: Document = {} if event_id is None else {"event_id": event_id}

    async def read(collection: str, sort_key: str) -> tuple[Document, ...]:
        cursor = database[collection].find(scope).sort(sort_key, 1)
        return tuple(await cursor.to_list(length=None))

    return Snapshot(
        events=await read(EVENTS, "created_at"),
        diagnoses=await read(DIAGNOSES, "version"),
        decisions=await read(DECISIONS, "version"),
        verdicts=await read(VERDICTS, "version"),
        executions=await read(EXECUTIONS, "executed_at"),
        verifications=await read(VERIFICATIONS, "verified_at"),
        promises=await read(PROMISES, "created_at"),
        read_at=_utc_now(),
    )


# ---------------------------------------------------------------------------
# Grouping helpers, shared so the aggregations cannot disagree about them.
# ---------------------------------------------------------------------------


def latest_per_event(
    documents: tuple[Document, ...], *, key: str = "version"
) -> dict[str, Document]:
    """Return the highest-`key` document per `event_id`.

    Diagnoses, decisions and verdicts are all append-only and versioned, so "the
    current state of this event" is always the highest version and never the last
    one inserted. Used wherever an event must contribute exactly once — every money
    total, and every count the word "events" appears in.
    """
    latest: dict[str, Document] = {}
    for document in documents:
        event_id = document.get("event_id")
        if event_id is None:  # pragma: no cover - every stage requires one
            continue
        current = latest.get(event_id)
        if current is None or document.get(key, 0) >= current.get(key, 0):
            latest[event_id] = document
    return latest


def distinct_recoveries(
    verifications: tuple[Document, ...],
) -> tuple[dict[str, Document], int]:
    """Collapse recovered verifications to one per execution.

    Returns the surviving record per `execution_id` (the latest by `verified_at`),
    and how many were set aside.

    Why this exists. Stage 6 keys webhook idempotency on Razorpay's event id, which
    is correct — replaying one delivery must not write twice. But a *fresh* delivery
    reporting the same payment link is a different event id, so it is a legitimately
    distinct record, and the verification harness mints a new one on every run.
    Eleven recovered records currently describe four payments. Summing
    `amount_recovered` over them therefore reports money that never existed.

    Keyed on `execution_id` rather than `razorpay_payment_link_id` because the
    execution is the referential fact — one authorized action, at most one recovery
    — and is always present. The two keys agree on all current data.

    Nothing is written and nothing is deleted; the duplicates stay in the
    collection, where they are an accurate record of what Razorpay sent. This is a
    read-time correction to an arithmetic question, and the count it sets aside is
    reported beside every figure that depends on it.
    """
    from app.models.verification import RECOVERED_OUTCOME

    recovered = [
        document
        for document in verifications
        if document.get("outcome") == RECOVERED_OUTCOME
    ]
    survivors: dict[str, Document] = {}
    for document in recovered:
        execution_id = document.get("execution_id")
        if execution_id is None:  # pragma: no cover - required by the model
            continue
        current = survivors.get(execution_id)
        if current is None or document["verified_at"] >= current["verified_at"]:
            survivors[execution_id] = document
    return survivors, len(recovered) - len(survivors)


def recovered_amount_per_event(
    verifications: tuple[Document, ...],
) -> dict[str, float]:
    """Deduplicated recovered amount, keyed by `event_id`.

    An event with two recovered executions contributes both, because those are two
    payments. It is only repeat *deliveries* of one payment that are collapsed.
    """
    survivors, _ = distinct_recoveries(verifications)
    per_event: dict[str, float] = {}
    for document in survivors.values():
        event_id = document["event_id"]
        per_event[event_id] = per_event.get(event_id, 0.0) + document["amount_recovered"]
    return per_event


def percentage(numerator: float, denominator: float) -> float | None:
    """Return `numerator / denominator` as a percentage, or None if undefined.

    None rather than 0.0 on a zero denominator, deliberately. "0% of nothing" and
    "0% of four hundred thousand" render identically once a dashboard has drawn
    them, and only the second is a measurement.
    """
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def money(value: float) -> float:
    """Round a money total to paise.

    Applied at the end of a summation rather than per term, so the rounding is one
    step and not an accumulation of them.
    """
    from app.models.decision import MONEY_PRECISION

    return round(value, MONEY_PRECISION)
