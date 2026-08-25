"""The executor: turn an authorized verdict into a real action, once.

This is where the split the whole project is built around finally bites. Diagnosis
proposes, decision recommends, policy authorizes — none of them can spend anything.
This module can, and the shape of `execute` is a direct consequence:

* it takes an `AuthorizedVerdict`, not a verdict. There is no branch inside it that
  decides whether execution is permitted, because a verdict that should not execute
  cannot be constructed as its argument. `require_authorized` in
  `app/models/execution.py` is the single narrowing gate, and it raises rather than
  returning a falsy value;
* it re-reads the world before acting — the event, the decision, whether a record
  already exists — rather than trusting whatever the caller assembled;
* the action it takes is selected from `ACTION_FOR_INTERVENTION` by the intervention
  that was *authorized*, never by an argument. A caller cannot ask for a payment link
  under a reminder's permission.

**Idempotency.** One `ExecutionRecord` per verdict, ever, keyed on the verdict's own
ObjectId. The check-first path returns the existing record; the unique index makes
that correct under a race; Razorpay's `reference_id` refuses a duplicate at the
provider. See `app/execution/store.py`.

**A failed execution is terminal for its verdict** — RATIFIED, and the least obvious
choice here. The tempting alternative is to let a retry re-use the same permission,
which is wrong for one specific reason: a network timeout may have created a payment
link whose response we never saw. Retrying the same permission would then send a
second link to somebody who already has one, and the failure record would say we
never sent the first. So the recovery path goes back through policy —
`POST /authorize/{event_id}` produces a new verdict, which gets its own execution.
That path is only usable because a failed execution releases both the contact-cap
slot and the cooldown anchor; otherwise re-authorization would be blocked by the
failure it is trying to recover from.

**Retries are SIMULATED, and this is a real simplification.** `immediate_retry` and
`delayed_retry` should re-present the original mandate to the payment rail. Test mode
has no captured mandate to re-present, so both generate a fresh payment link
representing "pay this now" and record it under `action_type="retry_simulated"`,
which is a distinct action type precisely so nothing downstream can mistake it for a
real retry. What is faithful: an artifact exists, the timing is real, and Stage 6 has
something to verify. What is not: no mandate is exercised, no issuer sees a second
authorization attempt, and `delayed_retry`'s delay is not honoured — the link is
created now, not in four hours. Scheduling is not in this stage's scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from app.decision import store as decision_store
from app.diagnosis import store as diagnosis_store
from app.execution import razorpay, templates
from app.execution import store as execution_store
from app.ingestion import store as event_store
from app.models import DecisionRecord, DiagnosisRecord, RevenueEvent
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    CONTACT_ACTION_TYPES,
    LINK_ACTION_TYPES,
    AuthorizedVerdict,
    ExecutionRecord,
    ExecutionRecordDocument,
    _utc_now,
)

logger = logging.getLogger(__name__)

#: How long a generated link stays payable, in the description shown to the customer.
#: Not enforced against Razorpay's `expire_by` — expiry management is Stage 6's
#: problem at the earliest, and a stated expiry we do not enforce would be worse than
#: none. Present only as wording.
LINK_DESCRIPTION_MAX = 255


class ExecutionError(RuntimeError):
    """Base for failures that prevent an execution from being *attempted*.

    Distinct from a failed execution. These mean the attempt never got off the
    ground — a missing event, an intervention with no template — so nothing was
    sent and there is nothing to record. A failure that happens *during* an attempt
    produces a stored record with `status="failed"` instead.
    """


class EventNotFound(ExecutionError):
    """The verdict names an event that no longer exists."""


class DecisionNotFound(ExecutionError):
    """The verdict names a decision that no longer exists."""


class DiagnosisNotFound(ExecutionError):
    """The authorized decision names a diagnosis that no longer exists.

    Fatal rather than degraded, because the diagnosis is where `root_cause` lives and
    every message and link description this stage produces is built from it. A
    generic message attributed to a specific diagnosis would be a false record of
    what the customer was told.
    """


@dataclass(frozen=True)
class AuthorizedAction:
    """The full chain behind one authorized verdict, read back from the database.

    Assembled rather than passed in, so `execute` acts on what is stored now and not
    on what a caller believes. The diagnosis is the one the *decision* pinned, not
    the event's latest: the recommendation was made for a particular explanation, and
    the message has to describe that explanation rather than a newer one the
    recommendation never saw.
    """

    event: RevenueEvent
    decision: DecisionRecord
    diagnosis: DiagnosisRecord

    @property
    def root_cause(self) -> str:
        return self.diagnosis.root_cause


async def _load_chain(verdict: AuthorizedVerdict) -> AuthorizedAction:
    """Read the event, the authorized decision, and the diagnosis it was based on.

    Raises:
        EventNotFound, DecisionNotFound, DiagnosisNotFound: with the reference that
            could not be resolved. Each means the attempt cannot be made, so nothing
            is recorded.
    """
    event_document = await event_store.get_event(verdict.event_id)
    if event_document is None:
        raise EventNotFound(
            f"Verdict {verdict.id} authorizes an action for event "
            f"{verdict.event_id!r}, which no longer exists"
        )
    event = RevenueEvent.model_validate(
        {key: value for key, value in event_document.items() if key != "_id"}
    )

    decision_document = await _by_id(decision_store.collection(), verdict.decision_id)
    if decision_document is None:
        raise DecisionNotFound(
            f"Verdict {verdict.id} authorizes decision {verdict.decision_id!r}, "
            "which no longer exists; what was permitted cannot be determined"
        )
    decision = DecisionRecord.from_document(decision_document)

    diagnosis_document = await _by_id(
        diagnosis_store.collection(), decision.diagnosis_id
    )
    if diagnosis_document is None:
        raise DiagnosisNotFound(
            f"Decision {decision.id} is based on diagnosis "
            f"{decision.diagnosis_id!r}, which no longer exists; the root cause "
            "every message and link description is built from is unavailable"
        )
    diagnosis = DiagnosisRecord.from_document(diagnosis_document)

    return AuthorizedAction(event=event, decision=decision, diagnosis=diagnosis)


async def _by_id(collection: Any, document_id: str) -> dict[str, Any] | None:
    """Fetch one document by its string id, or None if the id is unusable."""
    try:
        object_id = ObjectId(document_id)
    except InvalidId:  # pragma: no cover - the models' patterns precede this
        return None
    return await collection.find_one({"_id": object_id})


@dataclass(frozen=True)
class ExecutionOutcome:
    """What `execute` did.

    `created` distinguishes the two ways a caller gets a record back: a side effect
    that just happened, or one that happened earlier and was returned unchanged. The
    route turns it into 201 versus 200, which is the only externally visible
    difference between executing and refusing to execute twice.
    """

    record: ExecutionRecordDocument
    created: bool


def _link_description(*, event: RevenueEvent, intervention: str, root_cause: str) -> str:
    """The line the customer sees on the Razorpay payment page.

    Built from the event and the diagnosis. Carries no customer identifier: the
    payment page is a URL anyone holding it can open, so `customer_ref` stays in
    `notes`, which only the merchant sees.
    """
    phrase = templates.CAUSE_PHRASES.get(root_cause, "this payment did not complete")
    if intervention == "payment_method_update_link":
        text = f"Update your payment method — {phrase}"
    elif intervention == "recovery_payment_link":
        text = f"Complete your payment — {phrase}"
    else:
        # Both retry variants. Worded as what it actually is, a fresh link, rather
        # than as a retry, because the customer is not retrying anything.
        text = f"Payment for {event.event_id} — {phrase}"
    return text[:LINK_DESCRIPTION_MAX]


async def _execute_link(
    *,
    verdict: AuthorizedVerdict,
    event: RevenueEvent,
    decision: DecisionRecord,
    root_cause: str,
    credentials: razorpay.RazorpayCredentials | None,
) -> dict[str, Any]:
    """Create a Razorpay payment link and return the fields it contributes.

    Raises:
        razorpay.RazorpayNotConfigured: no credentials. Nothing was attempted.
        razorpay.RazorpayCallFailed: the call was made and did not succeed.
    """
    link = await razorpay.create_payment_link(
        amount=decision.revenue_at_risk,
        currency=event.currency,
        description=_link_description(
            event=event,
            intervention=decision.recommended_intervention,
            root_cause=root_cause,
        ),
        reference_id=razorpay.reference_id_for(verdict.id),
        notes={
            # Everything needed to walk back from a Razorpay dashboard row to the
            # authorization that produced it, without a lookup table.
            "vasooli_event_id": event.event_id,
            "vasooli_customer_ref": event.customer_ref,
            "vasooli_policy_verdict_id": verdict.id,
            "vasooli_intervention": decision.recommended_intervention,
            "vasooli_root_cause": root_cause,
        },
        credentials=credentials,
    )
    return {
        "razorpay_payment_link_id": link.id,
        "razorpay_payment_link_url": link.short_url,
    }


def _execute_contact(
    *, event: RevenueEvent, decision: DecisionRecord, root_cause: str
) -> dict[str, Any]:
    """Render the contact message and return the fields it contributes.

    Nothing is delivered — the system holds a `customer_ref` and no address. The
    record says a message was *logged*, and `contact_channel` names the channel a
    real integration would use. `contact_logged` is the action type for exactly that
    reason: it does not claim delivery.
    """
    message = templates.render(
        intervention=decision.recommended_intervention,
        root_cause=root_cause,
        event_id=event.event_id,
        customer_ref=event.customer_ref,
        amount_at_risk=decision.revenue_at_risk,
        currency=event.currency,
    )
    logger.info(
        "Contact logged for event %s: %s", event.event_id, templates.summarise(message)
    )
    return {
        "contact_channel": message.channel,
        "contact_message_summary": templates.summarise(message),
    }


async def execute(
    verdict: AuthorizedVerdict,
    *,
    credentials: razorpay.RazorpayCredentials | None = None,
) -> ExecutionOutcome:
    """Perform the action one authorized verdict permits, at most once.

    Args:
        verdict: The stored verdict, narrowed by `require_authorized`. The type is
            the precondition; there is no permission check in the body.
        credentials: Razorpay keys, defaulting to the configured pair. An explicit
            pair is how the failure test drives a real rejection without mutating a
            module global.

    Returns:
        The record and whether this call created it.

    Raises:
        EventNotFound, DecisionNotFound, DiagnosisNotFound: the attempt could not be
            made at all, so nothing is recorded.
        templates.NoTemplate: a contact intervention with no message. A build error.
        razorpay.RazorpayNotConfigured: no keys. Nothing was attempted, so nothing
            is recorded — an operator problem, not a failed send.
        execution_store.VerdictReferenceError: the write-time guard refused. Only
            reachable if the verdict changed underneath us between narrowing and
            writing.
    """
    existing = await execution_store.find_for_verdict(verdict.id)
    if existing is not None:
        logger.info(
            "Verdict %s was already executed (%s); returning the existing record",
            verdict.id,
            existing["status"],
        )
        return ExecutionOutcome(
            record=ExecutionRecordDocument.from_document(existing), created=False
        )

    chain = await _load_chain(verdict)
    event, decision, root_cause = chain.event, chain.decision, chain.root_cause

    # The intervention comes from the authorized decision, never from a caller. The
    # action type comes from the intervention by the declared table, so there is no
    # point at which a caller chooses what happens.
    intervention = decision.recommended_intervention
    action_type = ACTION_FOR_INTERVENTION.get(intervention)
    if action_type is None:
        # A `no_action` variant, or something outside the table. Policy blocks these,
        # so arriving here means an unauthorized action got through — a structural
        # error, not a case to skip quietly.
        raise ExecutionError(
            f"Verdict {verdict.id} authorized {intervention!r}, which has no "
            "executable action. Policy is supposed to block this; an authorized "
            "verdict for it means the decision, the verdict or the catalogue "
            "disagree about what was permitted"
        )

    fields: dict[str, Any] = {}
    status = "completed"
    failure_reason: str | None = None

    try:
        if action_type in LINK_ACTION_TYPES:
            fields = await _execute_link(
                verdict=verdict,
                event=event,
                decision=decision,
                root_cause=root_cause,
                credentials=credentials,
            )
        elif action_type in CONTACT_ACTION_TYPES:
            fields = _execute_contact(
                event=event, decision=decision, root_cause=root_cause
            )
        else:  # pragma: no cover - the table and the sets are checked at import
            raise ExecutionError(
                f"action type {action_type!r} has no handler in this module"
            )
    except razorpay.RazorpayCallFailed as exc:
        # The attempt was made and did not succeed. This is a real execution
        # outcome, so it is recorded — with no artifact fields, which the model
        # enforces — rather than raised. A stored failure is what releases the
        # contact-cap slot and the cooldown anchor, making re-authorization possible.
        status = "failed"
        failure_reason = str(exc)
        fields = {}
        logger.warning(
            "Execution of %s for event %s failed: %s",
            intervention,
            event.event_id,
            failure_reason,
        )

    # Minted after the side effect, never before: this is the real send timestamp and
    # the cooldown measures from it.
    record = ExecutionRecord(
        event_id=verdict.event_id,
        policy_verdict_id=verdict.id,
        policy_verdict_version=verdict.version,
        intervention=intervention,
        action_type=action_type,
        executed_at=_utc_now(),
        status=status,
        failure_reason=failure_reason,
        **fields,
    )

    try:
        document_id = await execution_store.insert(record)
    except execution_store.DuplicateExecution as exc:
        # Lost a race against a concurrent request. The other one's side effect is
        # the one that counts; ours produced a second Razorpay link only if the
        # provider also failed to catch the duplicate `reference_id`, which is why
        # that layer exists.
        return ExecutionOutcome(
            record=ExecutionRecordDocument.from_document(exc.existing), created=False
        )

    return ExecutionOutcome(
        record=ExecutionRecordDocument(id=document_id, **record.model_dump()),
        created=True,
    )
