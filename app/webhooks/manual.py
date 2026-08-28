"""Stage 9 — the receivable verification path (manual payment confirmation).

The second, parallel verification path. Stage 6 Part A verifies a Razorpay payment
link by receiving a signed webhook about it; that path is untouched and lives in
`app/webhooks/service.py`. This module handles the case that path structurally
cannot: an intervention that created no gateway artifact.

**The gap this closes.** `reminder`, `escalating_reminder_sequence` and
`manual_escalation` all execute as `contact_logged` — a structured record of a
message, no Razorpay call, no link. Verification only ever happened via a payment
link webhook, so an event routed to one of those could never produce a
`VerificationRecord` no matter what the customer actually did. A merchant chasing
receivables by contact alone would read 0% recovery forever. That is a hole in the
measurement, not a property of the interventions.

**Why this is a separate module rather than a branch inside `reconcile`.**
`reconcile` takes a `VerifiedWebhook` and there is no overload — that signature is
the whole enforcement mechanism for "verify before processing", and threading a
sourceless second caller through it would spend that guarantee. The two paths share
what should be shared (the record model's arithmetic validators, the expected-amount
derivation, the status-transition table, the write-time referential guard) and share
nothing else.

**What this path is not.** It is not a way to mark a payment link paid. A
link-producing execution is refused here — see
`app/webhooks/store.py:NotManuallyConfirmable` — because a manual override on a
channel that has real verification available is the same as not having real
verification. Nor does it fabricate gateway provenance: there is no synthesised
`razorpay_event_id`, and every record it writes says `source:
"manual_confirmation"` so that no total can silently mix asserted money with
verified money.

**Order of the checks, which matters.** Idempotency first, then the terminal-state
guard. Re-confirming the same execution has to be a no-op returning the existing
record; if the already-recovered guard ran first it would answer 409 instead,
because the record blocking it would be the caller's own previous confirmation.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from app.db import get_database
from app.execution.store import COLLECTION_NAME as EXECUTION_COLLECTION
from app.models.verification import (
    MANUAL_CONFIRMATION_CHANNEL,
    RECOVERED_OUTCOME,
    ManualConfirmationAck,
    ManualPaymentConfirmation,
    ManualVerification,
    ManualVerificationDocument,
    amounts_differ,
    confirmation_id_for,
    verification_document,
)
from app.webhooks import store
from app.webhooks.service import STATUS_FOR_OUTCOME, expected_amount, has_recovered

logger = logging.getLogger(__name__)


class ManualConfirmationError(ValueError):
    """Base for refusals of a manual payment confirmation."""


class ExecutionNotFound(ManualConfirmationError):
    """No execution exists under the id in the path."""


class EventAlreadyRecovered(ManualConfirmationError):
    """This event already has a recovered verification. Terminal, deliberately.

    The same guard philosophy as Stage 6 Part B's promise-creation check: money
    that has already been recorded as recovered cannot be recovered a second time,
    and a second confirmation of it would add a second amount to the totals.

    Carries the blocking verification so the caller is told which record stopped
    it — and, through that record's `source`, whether the earlier evidence was a
    gateway webhook or another assertion.

    Named distinctly from `app.ptp.safety.AlreadyRecovered`, which is a different
    exception with a different consumer, so a handler cannot catch one meaning the
    other. Both are raised off the same underlying predicate, `has_recovered`.
    """

    def __init__(self, event_id: str, verification: dict[str, Any]) -> None:
        self.event_id = event_id
        self.verification = verification
        super().__init__(
            f"Event {event_id!r} is already recorded as recovered by verification "
            f"{str(verification.get('_id'))} (source "
            f"{verification.get('source', 'webhook')!r}, "
            f"{verification.get('amount_recovered')} at "
            f"{verification.get('verified_at')}); refusing to confirm the same money "
            "twice"
        )


class ExpectedAmountUnavailable(ManualConfirmationError):
    """The execution's verdict or decision is gone, so there is nothing to compare to.

    Not defaulted around. A mismatch check against a guessed expectation is worse
    than no mismatch check, and the same reasoning is spelled out in
    `app/webhooks/service.py:expected_amount`.
    """


async def _load_execution(execution_id: str) -> dict[str, Any]:
    """Read the execution named in the path, or refuse.

    Raises:
        ExecutionNotFound: the id is not a valid ObjectId, or names nothing. Both are
            the same fact to a caller — there is no such execution — and
            distinguishing them in the response would only report on the id format.
    """
    try:
        object_id = ObjectId(execution_id)
    except InvalidId as exc:
        raise ExecutionNotFound(
            f"{execution_id!r} is not a valid execution id"
        ) from exc

    document = await get_database()[EXECUTION_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise ExecutionNotFound(f"No execution with id {execution_id!r} exists")
    return document


def _as_manual_document(document: dict[str, Any]) -> ManualVerificationDocument:
    """Narrow a stored verification to the manual variant.

    Reachable only for documents found by `confirmation_id`, which no webhook record
    carries, so a failure here means the collection holds something the union cannot
    describe.
    """
    parsed = verification_document(document)
    assert isinstance(parsed, ManualVerificationDocument), (
        f"verification {str(document.get('_id'))} was found by confirmation_id but "
        f"parsed as {type(parsed).__name__}"
    )
    return parsed


async def confirm_payment(
    execution_id: str, request: ManualPaymentConfirmation
) -> ManualConfirmationAck:
    """Record a merchant's assertion that a contact-type recovery was paid.

    Six steps, in this order:

    1. load the execution named in the path. The event id comes from it, never from
       the caller, so a payment cannot be attributed to a different event than the
       action was taken for;
    2. idempotency — one confirmation per execution, keyed on an id derived from the
       execution. A repeat returns the existing record with `created: false`;
    3. the terminal-state guard — refuse if this event already has a recovered
       verification from anywhere. Reuses `has_recovered`, the single definition of
       "has this been paid", rather than asking the question a second way;
    4. derive the expected amount from the execution's own verdict chain. The caller
       supplies what arrived and nothing else, so it cannot also supply what was
       owed and thereby define away a mismatch;
    5. write, through the same `store.insert` the webhook path uses — which is where
       the referential guard and the contact-action allowlist live. A link-producing
       execution is refused there, not here, so the refusal cannot be bypassed by a
       future second caller;
    6. move the event's lifecycle status with Stage 6's own
       `transition_event_status`, against Stage 6's own `STATUS_FOR_OUTCOME`. A
       status field with two writers is a status field that can go backwards.

    Raises:
        ExecutionNotFound: no such execution.
        EventAlreadyRecovered: this event's money is already recorded as returned.
        ExpectedAmountUnavailable: the verdict chain behind the execution is broken.
        store.NotManuallyConfirmable: the execution is a link action, or not completed.
        store.VerificationReferenceError: any other write-time referential refusal.
    """
    execution = await _load_execution(execution_id)
    event_id = str(execution["event_id"])

    # 2. Already confirmed? Before the terminal guard, so a repeat is a no-op rather
    #    than a 409 raised by the caller's own earlier record.
    confirmation_id = confirmation_id_for(execution_id)
    existing = await store.find_by_confirmation_id(confirmation_id)
    if existing is not None:
        logger.info(
            "Execution %s is already confirmed by %s; returning it unchanged",
            execution_id,
            confirmation_id,
        )
        return ManualConfirmationAck(
            created=False,
            verification_id=str(existing["_id"]),
            verification=_as_manual_document(existing),
            event_status=await store.current_event_status(event_id),
            detail=(
                f"execution {execution_id} was already confirmed as paid "
                f"({existing.get('amount_recovered')} at "
                f"{existing.get('verified_at')}); nothing happened this time"
            ),
        )

    # 3. The terminal-state guard.
    recovered = await has_recovered(event_id)
    if recovered is not None:
        raise EventAlreadyRecovered(event_id, recovered)

    # 4. What was owed, from the chain rather than from the request body.
    try:
        amount_expected = await expected_amount(execution)
    except LookupError as exc:
        raise ExpectedAmountUnavailable(str(exc)) from exc

    amount_recovered = request.amount_recovered
    mismatch = amounts_differ(amount_recovered, amount_expected)
    if mismatch:
        # Loud, and on the row as well. Recorded rather than rejected: what the
        # merchant says arrived is what they say arrived, and refusing the record
        # would lose the payment entirely instead of flagging the discrepancy.
        logger.warning(
            "AMOUNT MISMATCH on manual confirmation for event %s: %.2f asserted "
            "against an expected %.2f (execution %s). Recording with "
            "amount_mismatch=True.",
            event_id,
            amount_recovered,
            amount_expected,
            execution_id,
        )

    record = ManualVerification(
        event_id=event_id,
        execution_id=execution_id,
        confirmation_id=confirmation_id,
        confirmed_by=MANUAL_CONFIRMATION_CHANNEL,
        amount_recovered=amount_recovered,
        amount_expected=amount_expected,
        amount_mismatch=mismatch,
        **({} if request.confirmed_at is None else {"confirmed_at": request.confirmed_at}),
    )

    # 5. Write. The allowlist and the referential guard are inside this call.
    try:
        verification_id = await store.insert(record)
    except store.DuplicateVerification as duplicate:
        # The partial unique index caught a concurrent second confirmation that step
        # 2 could not see. Same answer as step 2: the record that won.
        logger.warning(
            "Concurrent confirmation of execution %s; returning the record that won",
            execution_id,
        )
        return ManualConfirmationAck(
            created=False,
            verification_id=str(duplicate.existing["_id"]),
            verification=_as_manual_document(duplicate.existing),
            event_status=await store.current_event_status(event_id),
            detail=(
                "duplicate confirmation, caught by the unique index rather than the "
                f"pre-flight check: {duplicate}"
            ),
        )

    # 6. Move the event's lifecycle status, through Stage 6's guarded write.
    transition = await store.transition_event_status(
        event_id=event_id, target=STATUS_FOR_OUTCOME[RECOVERED_OUTCOME]
    )

    detail = (
        f"recorded a MANUALLY ASSERTED recovery of {amount_recovered:.2f} for event "
        f"{event_id} (execution {execution_id}); no gateway verified this, and the "
        "record says so in `source`"
    )
    if mismatch:
        detail += (
            f"; AMOUNT MISMATCH: {amount_recovered:.2f} asserted against an expected "
            f"{amount_expected:.2f}"
        )
    if transition.refused:
        detail += f"; {transition.detail}"

    return ManualConfirmationAck(
        created=True,
        verification_id=verification_id,
        verification=ManualVerificationDocument(
            id=verification_id, **record.model_dump()
        ),
        event_status=transition.current,
        detail=detail,
    )
