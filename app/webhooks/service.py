"""Reconciliation: turning a verified webhook into a statement about an action.

The narrow half of Stage 6 Part A. Everything here happens *after* the signature
matched, and the type signature is what says so: `reconcile()` takes a
`VerifiedWebhook` and there is no overload, no `dict` variant, and no keyword that
skips the check. The only place a `VerifiedWebhook` is constructed is
`app/webhooks/signature.py:accept()`.

**What this module deliberately does not do.** It does not diagnose, decide, or
consult policy. An inbound HTTP request from a third party is not permitted to
re-open those stages — Stage 2 explained the failure, Stage 3 chose the response,
Stage 4 authorized it, Stage 5 performed it, and all four are already on file. This
module reads Razorpay's word about the artifact Stage 5 created, records it, and
moves the originating event's lifecycle status. Three verbs: match, record, transit.

**Amounts are converted, not assumed.** Razorpay reports money in minor units;
`revenue_at_risk` is in major units. The conversion happens in one place here and
the two numbers are then compared rather than conflated, because "the link was for
₹1,200 and ₹1,200 arrived" and "the link was for ₹1,200 and something arrived" are
different facts and only the first one is a clean recovery.

**Failure to match is not failure.** A payment link this system did not create can
legitimately appear on the same Razorpay account, and Razorpay's at-least-once
delivery means an event for a link whose execution record was never written can
arrive. Both are logged loudly and acknowledged with a 200, because the alternative
is Razorpay retrying for 24 hours and then disabling the endpoint over a link that
was never ours. What is never done is inventing a match.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId

from app.decision.store import COLLECTION_NAME as DECISION_COLLECTION
from app.db import get_database
from app.execution.razorpay import MINOR_UNITS_PER_MAJOR
from app.models.decision import MONEY_PRECISION
from app.models.verification import (
    OUTCOME_FOR_EVENT,
    RECOVERED_OUTCOME,
    SUBSCRIBED_EVENTS,
    WebhookAck,
    WebhookVerification,
    amounts_differ,
)
from app.policy.store import COLLECTION_NAME as VERDICT_COLLECTION
from app.webhooks import store
from app.webhooks.signature import MalformedBody, VerifiedWebhook

logger = logging.getLogger(__name__)

#: The payload key carrying the payment-link entity, for all three subscribed
#: events. Razorpay nests each entity under its own name with the object itself
#: under `entity`: `payload.payment_link.entity`.
LINK_PAYLOAD_KEY = "payment_link"

#: Which lifecycle status each outcome moves the originating event to.
#:
#: `expired` and `cancelled` both land on `recovery_failed`, which is not terminal:
#: that attempt is over, the debt is not. `not_recovered` is absent because nothing
#: produces it — see `app/models/verification.py`.
STATUS_FOR_OUTCOME: dict[str, str] = {
    "recovered": "recovered",
    "expired": "recovery_failed",
    "cancelled": "recovery_failed",
}

assert set(STATUS_FOR_OUTCOME) == set(OUTCOME_FOR_EVENT.values()), (
    f"STATUS_FOR_OUTCOME covers {sorted(STATUS_FOR_OUTCOME)}, but the events this "
    f"system subscribes to produce {sorted(set(OUTCOME_FOR_EVENT.values()))}"
)


def from_minor_units(minor: int) -> float:
    """Convert Razorpay's paise to major units.

    The inverse of `app/execution/razorpay.py:to_minor_units`, sharing that module's
    constant so the round trip cannot drift.
    """
    return round(minor / MINOR_UNITS_PER_MAJOR, MONEY_PRECISION)


def link_entity(webhook: VerifiedWebhook) -> dict[str, Any]:
    """Extract the payment-link entity from a verified payload.

    Raises:
        MalformedBody: the payload does not have the documented shape. Answered 400
            rather than 200 on purpose: a 200 would tell Razorpay we understood a
            `payment_link.paid` we in fact could not read, and a silently-dropped
            paid event is the worst failure available in this stage. The cost is
            that a persistently unreadable payload eventually gets the endpoint
            disabled, which is loud — and loud is the right failure mode here.
    """
    container = webhook.payload.get(LINK_PAYLOAD_KEY)
    if not isinstance(container, dict):
        raise MalformedBody(
            f"Signature verified, but payload has no {LINK_PAYLOAD_KEY!r} object; "
            f"every {webhook.event} carries one. Payload keys: "
            f"{sorted(webhook.payload)[:12]}"
        )
    entity = container.get("entity")
    if not isinstance(entity, dict):
        raise MalformedBody(
            f"Signature verified, but payload.{LINK_PAYLOAD_KEY}.entity is "
            f"{type(entity).__name__}, not an object"
        )
    link_id = entity.get("id")
    if not isinstance(link_id, str) or not link_id:
        raise MalformedBody(
            f"Signature verified, but payload.{LINK_PAYLOAD_KEY}.entity has no 'id'; "
            "there is nothing to match against an executed action"
        )
    return entity


def _recovered_amount(*, event: str, entity: dict[str, Any]) -> float:
    """What Razorpay says arrived, in major units.

    Zero for anything but a paid event: an expiry or cancellation moved no money,
    and the model refuses to store one that claims otherwise.

    `amount_paid` is used rather than `amount`. `amount` is what the link was *for*;
    `amount_paid` is what was actually collected, and on a partial-payment link the
    two differ. Reading `amount` here would make every mismatch invisible by
    construction, since `amount` is the number this system sent Razorpay in the
    first place.
    """
    if OUTCOME_FOR_EVENT[event] != RECOVERED_OUTCOME:
        return 0.0

    paid = entity.get("amount_paid")
    if not isinstance(paid, int) or isinstance(paid, bool):
        raise MalformedBody(
            f"Signature verified, but a {event} payload has amount_paid="
            f"{paid!r} ({type(paid).__name__}); a paid link must report an integer "
            "amount in minor units. Refusing to guess what was collected."
        )
    return from_minor_units(paid)


async def expected_amount(execution: dict[str, Any]) -> float:
    """The amount the action was for: the authorized decision's `revenue_at_risk`.

    Walks execution → policy verdict → decision, which is the same chain
    `app/execution/store.py` walks in its write-time guard, and lands on the exact
    field `app/execution/service.py` passed to Razorpay as the link amount. Reading
    the link's own `amount` back out of the webhook instead would be circular: it
    would compare Razorpay's echo of our number against Razorpay's echo of our
    number.

    Public, and shared with Stage 9's manual path (`app/webhooks/manual.py`), which
    needs the same number for the same comparison. Every execution carries a
    `policy_verdict_id` regardless of action type, so the chain resolves for a logged
    contact exactly as it does for a generated link — which is why the manual path
    reuses this rather than deriving "what was owed" a second way.

    Raises:
        LookupError: the chain is broken. Only possible if a verdict or decision was
            deleted after the execution was written, so it is reported rather than
            defaulted — a mismatch check against a guessed expectation is worse than
            no mismatch check.
    """
    verdict_id = execution.get("policy_verdict_id")
    verdict = await get_database()[VERDICT_COLLECTION].find_one(
        {"_id": ObjectId(str(verdict_id))}, {"decision_id": 1}
    )
    if verdict is None:
        raise LookupError(
            f"Execution {execution['_id']} names policy verdict {verdict_id!r}, "
            "which no longer exists; the expected amount cannot be established"
        )
    decision = await get_database()[DECISION_COLLECTION].find_one(
        {"_id": ObjectId(str(verdict["decision_id"]))}, {"revenue_at_risk": 1}
    )
    if decision is None:
        raise LookupError(
            f"Policy verdict {verdict_id!r} names decision "
            f"{verdict['decision_id']!r}, which no longer exists"
        )
    return round(float(decision["revenue_at_risk"]), MONEY_PRECISION)


def _ack(
    webhook: VerifiedWebhook,
    *,
    processed: bool,
    detail: str,
    verification_id: str | None = None,
    event_status: str | None = None,
) -> WebhookAck:
    """Build the receipt. Every authentic request gets one — see `WebhookAck`."""
    return WebhookAck(
        received=True,
        razorpay_event_id=webhook.razorpay_event_id,
        razorpay_event=webhook.event,
        processed=processed,
        detail=detail,
        verification_id=verification_id,
        event_status=event_status,
    )


async def reconcile(webhook: VerifiedWebhook) -> WebhookAck:
    """Record what a verified webhook says about an executed action.

    Takes `VerifiedWebhook` and nothing else. That is the whole enforcement
    mechanism for "verify before processing": this function cannot be called with an
    unverified body because an unverified body cannot be turned into its argument.

    Order: subscription filter, then deduplication, then payload extraction, then
    the match, then the record, then the status transition. Deduplication comes
    before anything expensive because at-least-once delivery makes a repeat the
    ordinary case, not the exceptional one.

    Raises:
        MalformedBody: a subscribed event whose payload cannot be read. Answered
            400. Every other outcome — unsubscribed event, duplicate, unmatched
            link, broken reference — is a 200 with `processed: false`.
    """
    # 1. Is this an event we asked for? Razorpay sends whatever the dashboard is
    #    subscribed to, and a dashboard can be changed without this code knowing.
    if webhook.event not in SUBSCRIBED_EVENTS:
        logger.info(
            "Webhook %s carries unsubscribed event %s; acknowledged without action",
            webhook.razorpay_event_id,
            webhook.event,
        )
        return _ack(
            webhook,
            processed=False,
            detail=(
                f"{webhook.event} is not an event this stage records; acknowledged "
                "so Razorpay does not retry, but nothing was written"
            ),
        )

    # 2. Have we seen this exact event before? Razorpay's delivery is at-least-once.
    existing = await store.find_by_razorpay_event_id(webhook.razorpay_event_id)
    if existing is not None:
        logger.info(
            "Webhook %s is a re-delivery of a %s already recorded for event %s; "
            "ignored",
            webhook.razorpay_event_id,
            existing.get("outcome"),
            existing.get("event_id"),
        )
        return _ack(
            webhook,
            processed=False,
            detail=(
                f"duplicate delivery: this event was already recorded as "
                f"{existing.get('outcome')!r} for event {existing.get('event_id')!r}"
            ),
            verification_id=str(existing["_id"]),
            event_status=await store.current_event_status(str(existing["event_id"])),
        )

    # 3. Read the payload. Raises MalformedBody -> 400, deliberately.
    entity = link_entity(webhook)
    link_id = str(entity["id"])

    # 4. Which action created this link? No match means no evidence about anything
    #    on file, and specifically does not mean "pick the closest execution".
    execution = await store.find_execution_by_link_id(link_id)
    if execution is None:
        logger.warning(
            "Webhook %s reports %s for payment link %s, which no execution record "
            "claims. Acknowledged without action — NOT matched to any event.",
            webhook.razorpay_event_id,
            webhook.event,
            link_id,
        )
        return _ack(
            webhook,
            processed=False,
            detail=(
                f"payment link {link_id!r} does not belong to any recorded execution; "
                "acknowledged, but deliberately not matched to an event"
            ),
        )

    event_id = str(execution["event_id"])

    # 5. Assemble the statement. Both amounts, then the comparison.
    try:
        amount_expected = await expected_amount(execution)
    except LookupError as exc:
        logger.error(
            "Webhook %s matched execution %s for event %s, but the expected amount "
            "could not be established: %s",
            webhook.razorpay_event_id,
            execution["_id"],
            event_id,
            exc,
        )
        return _ack(
            webhook,
            processed=False,
            detail=(
                "matched an execution, but its authorizing verdict or decision is "
                f"missing, so no expected amount exists to verify against: {exc}"
            ),
            event_status=await store.current_event_status(event_id),
        )

    amount_recovered = _recovered_amount(event=webhook.event, entity=entity)
    outcome = OUTCOME_FOR_EVENT[webhook.event]
    mismatch = (
        amounts_differ(amount_recovered, amount_expected)
        if outcome == RECOVERED_OUTCOME
        else False
    )
    if mismatch:
        # Loud, and recorded on the row as well. The record is still written: what
        # arrived, arrived, and pretending otherwise would lose the money entirely.
        logger.warning(
            "AMOUNT MISMATCH on event %s: Razorpay confirmed %.2f against an "
            "expected %.2f (link %s, Razorpay event %s). Recording the recovery with "
            "amount_mismatch=True — the amounts are NOT assumed to agree.",
            event_id,
            amount_recovered,
            amount_expected,
            link_id,
            webhook.razorpay_event_id,
        )

    record = WebhookVerification(
        event_id=event_id,
        execution_id=str(execution["_id"]),
        razorpay_event_id=webhook.razorpay_event_id,
        razorpay_event=webhook.event,
        razorpay_payment_link_id=link_id,
        outcome=outcome,
        amount_recovered=amount_recovered,
        amount_expected=amount_expected,
        amount_mismatch=mismatch,
    )

    # 6. Store it. The unique index is what makes a concurrent re-delivery safe;
    #    step 2 only catches the sequential case.
    try:
        verification_id = await store.insert(record)
    except store.DuplicateVerification as duplicate:
        return _ack(
            webhook,
            processed=False,
            detail=(
                "duplicate delivery, caught by the unique index rather than the "
                f"pre-flight check: {duplicate}"
            ),
            verification_id=str(duplicate.existing["_id"]),
            event_status=await store.current_event_status(event_id),
        )
    except store.VerificationReferenceError as exc:
        # The guard refused. Not a bad request from Razorpay — a disagreement
        # between two of our own records — so it is answered 200 and shouted about.
        logger.error(
            "Refusing to record webhook %s for event %s: %s",
            webhook.razorpay_event_id,
            event_id,
            exc,
        )
        return _ack(
            webhook,
            processed=False,
            detail=f"referential check refused this verification: {exc}",
            event_status=await store.current_event_status(event_id),
        )

    # 7. Move the event's lifecycle status. Guarded by the transition table inside
    #    the query, so an out-of-order webhook cannot walk a recovery backwards.
    transition = await store.transition_event_status(
        event_id=event_id, target=STATUS_FOR_OUTCOME[outcome]
    )

    detail = f"recorded {outcome} for event {event_id}"
    if mismatch:
        detail += (
            f"; AMOUNT MISMATCH: {amount_recovered:.2f} arrived against an expected "
            f"{amount_expected:.2f}"
        )
    if transition.refused:
        detail += f"; {transition.detail}"

    return _ack(
        webhook,
        processed=True,
        detail=detail,
        verification_id=verification_id,
        event_status=transition.current,
    )


async def has_recovered(event_id: str) -> dict[str, Any] | None:
    """Return the verification proving this event's money came back, or None.

    The single read behind promise-to-pay's mandatory payment re-check. Lives here
    rather than in the promise module so that "has this been paid" has exactly one
    definition in the codebase: a stored `VerificationRecord` with outcome
    `recovered`. A second implementation of that question is precisely how a
    follow-up gets sent to somebody who already paid.

    **There is no `source` filter, and that is the decision, not an omission.** A
    manual confirmation counts here exactly as a webhook does, which has two
    consequences, both intended. It makes `promised -> honored` reachable for a
    contact-only event, which before Stage 9 it was not — the only path to `honored`
    runs through this predicate. And it stops a follow-up being sent to a customer
    the merchant has already told us paid, which is the failure this function exists
    to prevent and does not become acceptable because the evidence was a human's.

    Money totals are a different question, and they DO split by source: see
    `app/metrics/aggregate.py`. Suppressing a chase and counting revenue are not the
    same decision, so they do not read the same filter.
    """
    for verification in await store.list_for_event(event_id):
        if verification.get("outcome") == RECOVERED_OUTCOME:
            return verification
    return None
