"""Promise-to-pay logic (Stage 6 Part B) — recording commitments and resolving them.

THE ORDER OF OPERATIONS IN `check_promise` IS THE POINT OF THIS FILE
-------------------------------------------------------------------
The mandatory payment re-check is not one step among several. It is the first
thing that happens, it happens on every path including the ones that go on to do
nothing, and the object it produces is the only key to the follow-up sender:

    1. read the promise
    2. RE-CHECK PAYMENT STATUS  ->  raises AlreadyRecovered if money is in
    3. (only reachable holding a confirmation from step 2) consider the deadline
    4. (only reachable holding a confirmation from step 2) send a follow-up

Step 2 raising rather than returning is what makes steps 3 and 4 unreachable when
the customer has paid: there is no `if recovered:` to get wrong, because the
remainder of the function does not execute. And `send_follow_up` requires the
confirmation object as its first argument, so it cannot be called from anywhere
that has not just been through step 2. See `app/ptp/safety.py`.

FOLLOW-UPS ARE NOT EXEMPT FROM THE GUARDRAILS
---------------------------------------------
`send_follow_up` does not construct a message, choose an intervention, or write an
`ExecutionRecord`. It calls `authorize_event` and `execute_event` — the exact
functions behind `POST /authorize/{event_id}` and `POST /execute/{event_id}`. Not
reimplementations of them, and not the policy package's primitives reassembled in
the same order: the same two functions.

That is a deliberate inversion of the usual layering, and the reason is worth
being explicit about. A promise module that called `gather_context` / `evaluate` /
`append` itself would be a *second* assembly of the policy gate, and second
assemblies drift. When they drift, the thing that stops working is a guardrail —
an opt-out, a contact cap, a cooldown — and it stops working silently, on the code
path that sends messages to people who have already been chased twice. Importing
"upwards" is a smaller cost than that. It is noted in the closing report rather
than hidden here.

Consequently a `blocked` verdict is a NORMAL outcome of this module, reported in
`FollowUpReport(sent=False, ...)`, never raised. Policy refusing to send is the
system working. The promise stays `broken` and `follow_up_sent` stays False, so a
later check retries once the cooldown has elapsed.

WHAT IS NOT BUILT
-----------------
There is no scheduler. In production a job would sweep every open promise whose
date has passed and call `check_promise` on each; here that sweep is triggered by
hand through `POST /promises/{event_id}/check`. The query such a job would run is
`app/ptp/store.py:count_open_overdue`, kept alongside the rest so the shape of the
missing piece is precise rather than described. Nothing about the safety property
depends on what triggers the check — the re-check is inside `check_promise`, so a
scheduler could not skip it either.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Response

from app.models.promise import (
    OPEN_PROMISE_STATE,
    FollowUpReport,
    PromiseCheck,
    PromiseRequest,
    PromiseToPay,
    PromiseToPayDocument,
    deadline_passed,
)
from app.ptp import store
from app.ptp.safety import AlreadyRecovered, UnpaidConfirmation, confirm_still_unpaid
from app.routes.executions import execute_event
from app.routes.policy import authorize_event
from app.webhooks.store import transition_event_status

logger = logging.getLogger(__name__)

#: The event status a live promise puts its event into. Declared here because this
#: is the only stage that produces it: Part A moves events to `recovered` and
#: `recovery_failed`, and nothing else has a reason to say "we are waiting on a
#: commitment".
AWAITING_PROMISE_STATUS = "awaiting_promise"

#: The state a promise is moved to when its date passes unpaid.
BROKEN_STATE = "broken"
#: The state a promise is moved to when it is honored.
HONORED_STATE = "honored"
#: The state a broken promise is moved to once a follow-up has actually gone out.
CHASED_STATE = "reevaluating"


def _utc_now() -> datetime:
    """Current UTC time, truncated to milliseconds to match what BSON stores."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def as_record(document: dict) -> PromiseToPayDocument:
    """Validate a stored promise, converting `promised_date` back to a date."""
    fields = dict(document)
    fields["promised_date"] = store.decode_date(fields["promised_date"])
    return PromiseToPayDocument.from_document(fields)


# ---------------------------------------------------------------------------
# Creation.
# ---------------------------------------------------------------------------


async def create_promise(request: PromiseRequest) -> tuple[PromiseToPayDocument, bool]:
    """Record a commitment to pay, and move its event to `awaiting_promise`.

    Returns:
        The stored promise, and whether this call created it. False means an
        identical promise already existed — same event, same date, same amount —
        and is being returned unchanged.

    Raises:
        store.EventNotFound / store.EventSettled: the referential guard refused.
        store.DuplicatePromise: a promise exists for this event and date for a
            DIFFERENT amount. A genuine conflict, not a retry: two amounts cannot
            both be what was promised for one date.
    """
    promise = PromiseToPay(
        event_id=request.event_id,
        promised_amount=request.promised_amount,
        promised_date=request.promised_date,
    )

    try:
        promise_id = await store.insert(promise)
    except store.DuplicatePromise as duplicate:
        existing = as_record(duplicate.existing)
        if existing.promised_amount == promise.promised_amount:
            logger.info(
                "Promise for event %r on %s already recorded; returning it unchanged",
                promise.event_id,
                promise.promised_date.isoformat(),
            )
            return existing, False
        raise

    # The event lifecycle is moved by the one guarded implementation of that write,
    # in `app/webhooks/store.py`. Reusing it rather than writing a second `$set`
    # here is the same reasoning as reusing the policy gate below: a status field
    # with two writers is a status field that can go backwards.
    transition = await transition_event_status(
        event_id=promise.event_id, target=AWAITING_PROMISE_STATUS
    )
    if not transition.changed:
        # Not an error. An event already `recovered` is terminal, and an event
        # already `awaiting_promise` has nothing to move. Both are worth a line in
        # the log and neither should stop the promise being recorded.
        logger.info(
            "Promise recorded for event %r but its status did not move: %s",
            promise.event_id,
            transition.detail,
        )

    stored = await store.find_by_id(promise_id)
    assert stored is not None, f"promise {promise_id} vanished immediately after insert"
    return as_record(stored), True


# ---------------------------------------------------------------------------
# The follow-up. Reachable only with a confirmation.
# ---------------------------------------------------------------------------


async def send_follow_up(
    confirmation: UnpaidConfirmation, *, event_id: str
) -> FollowUpReport:
    """Route a follow-up for a broken promise through the existing policy gate.

    `confirmation` is positional and required. That is the enforcement of the
    safety rule: the signature cannot be satisfied without an object that only
    `confirm_still_unpaid` can mint, and it is re-checked here for the right event
    and for freshness before anything is sent.

    Raises:
        MismatchedConfirmation: the confirmation names a different event.
        StaleConfirmation: the confirmation is too old to act on.
        HTTPException: propagated from the policy or execution endpoint — a missing
            decision (404) or a reference race (409). Not caught, because they mean
            the follow-up genuinely could not be evaluated, which is different from
            being evaluated and refused.
    """
    confirmation.assert_matches(event_id)
    confirmation.assert_fresh()

    # THE POLICY GATE. The same function `POST /authorize/{event_id}` is.
    verdict = await authorize_event(event_id)

    if verdict.verdict != "authorized":
        logger.info(
            "PTP follow-up for event %r suppressed: verdict v%s is %r because %r",
            event_id,
            verdict.version,
            verdict.verdict,
            verdict.reason,
        )
        return FollowUpReport(
            sent=False,
            policy_verdict_id=verdict.id,
            policy_verdict=verdict.verdict,
            policy_reason=verdict.reason,
            detail=(
                f"follow-up suppressed by policy: {verdict.verdict} because "
                f"{verdict.reason}. The promise stays broken and will be "
                "re-attempted on a later check"
            ),
        )

    # THE EXECUTOR. The same function `POST /execute/{event_id}` is. The `Response`
    # it takes only carries the 201-vs-200 distinction back to HTTP, which is of no
    # interest here, so a throwaway one is passed.
    record = await execute_event(event_id, Response())
    sent = record.status == "completed"

    logger.info(
        "PTP follow-up for event %r: %s %s (execution %s, status %s)",
        event_id,
        record.intervention,
        record.action_type,
        record.id,
        record.status,
    )
    return FollowUpReport(
        sent=sent,
        policy_verdict_id=verdict.id,
        policy_verdict=verdict.verdict,
        policy_reason=verdict.reason,
        execution_id=record.id,
        action_type=record.action_type,
        intervention=record.intervention,
        detail=(
            f"follow-up executed: {record.intervention} via {record.action_type}"
            if sent
            else (
                f"follow-up attempt failed ({record.failure_reason}); the promise "
                "stays broken so a later check can retry"
            )
        ),
    )


# ---------------------------------------------------------------------------
# The check. Payment first, always.
# ---------------------------------------------------------------------------


async def check_promise(event_id: str) -> PromiseCheck:
    """Resolve an event's current promise against reality.

    Raises:
        store.PromiseNotFound: the event has no promise to check.
    """
    document = await store.find_latest(event_id)
    if document is None:
        raise store.PromiseNotFound(
            f"event {event_id!r} has no promise to check; record one with POST "
            "/promises first"
        )

    promise_id = str(document["_id"])
    state_before = document["state"]
    promised_date = store.decode_date(document["promised_date"])
    passed = deadline_passed(promised_date)

    # -----------------------------------------------------------------------
    # STEP 1 — THE MANDATORY PAYMENT RE-CHECK.
    #
    # Before the deadline is looked at, before the state is touched, and before
    # any thought of a follow-up. Note that there is no `if` guarding this: an
    # already-honored promise runs it too, because "is the money in" is exactly the
    # question that makes a promise honored, and giving that state its own shortcut
    # would create a second answer to it.
    # -----------------------------------------------------------------------
    try:
        confirmation = await confirm_still_unpaid(event_id)
    except AlreadyRecovered as recovered:
        # ---- THE MONEY IS IN. NO FOLLOW-UP IS SENT, AND NONE CAN BE. ----
        # `confirmation` was never bound, so `send_follow_up` below is not merely
        # skipped by this branch — it has nothing to be called with.
        transition = await store.apply_transition(
            promise_id=promise_id, target=HONORED_STATE, resolved_at=_utc_now()
        )
        verification_id = str(recovered.verification.get("_id"))
        logger.info(
            "Promise %s for event %r honored by verification %s; no follow-up sent",
            promise_id,
            event_id,
            verification_id,
        )
        return PromiseCheck(
            event_id=event_id,
            promise_id=promise_id,
            state_before=state_before,
            state=HONORED_STATE if transition.changed else state_before,
            changed=transition.changed,
            payment_rechecked_at=recovered.checked_at,
            verifications_examined=recovered.verifications_examined,
            recovered_verification_id=verification_id,
            deadline_passed=passed,
            follow_up=None,
            detail=(
                f"payment confirmed by verification {verification_id}; promise "
                f"{'honored' if transition.changed else transition.detail}. "
                "NO follow-up was sent — the re-check found the money before the "
                "deadline was even considered"
            ),
        )

    # -----------------------------------------------------------------------
    # From here on `confirmation` exists, and it is the only way to be here.
    # -----------------------------------------------------------------------
    state = state_before
    changed = False

    if not passed:
        return PromiseCheck(
            event_id=event_id,
            promise_id=promise_id,
            state_before=state_before,
            state=state,
            changed=False,
            payment_rechecked_at=confirmation.checked_at,
            verifications_examined=confirmation.verifications_examined,
            deadline_passed=False,
            follow_up=None,
            detail=(
                f"unpaid, but {promised_date.isoformat()} has not passed; the "
                "commitment is still open and nothing is due"
            ),
        )

    # STEP 2 — the deadline has passed with no payment. The promise is broken.
    if state == OPEN_PROMISE_STATE:
        transition = await store.apply_transition(
            promise_id=promise_id, target=BROKEN_STATE, resolved_at=_utc_now()
        )
        if transition.changed:
            state, changed = BROKEN_STATE, True

    # STEP 3 — the follow-up, which structurally requires the confirmation above.
    follow_up: FollowUpReport | None = None
    if document.get("follow_up_sent"):
        detail = (
            f"promise broke on {promised_date.isoformat()} and has already been "
            "followed up once; no second contact is attempted from here"
        )
    elif state != BROKEN_STATE:
        detail = (
            f"promise is {state!r}, which does not call for a follow-up "
            f"({promised_date.isoformat()} has passed and nothing has been paid)"
        )
    else:
        follow_up = await send_follow_up(confirmation, event_id=event_id)
        if follow_up.sent:
            transition = await store.apply_transition(
                promise_id=promise_id,
                target=CHASED_STATE,
                resolved_at=_utc_now(),
                follow_up_sent=True,
            )
            if transition.changed:
                state, changed = CHASED_STATE, True
        detail = (
            f"promise broke on {promised_date.isoformat()}; {follow_up.detail}"
        )

    return PromiseCheck(
        event_id=event_id,
        promise_id=promise_id,
        state_before=state_before,
        state=state,
        changed=changed,
        payment_rechecked_at=confirmation.checked_at,
        verifications_examined=confirmation.verifications_examined,
        deadline_passed=True,
        follow_up=follow_up,
        detail=detail,
    )
