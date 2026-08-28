"""Free-text promise extraction (Stage 10) — a parser in front of an unchanged path.

WHAT THIS STAGE IS
------------------
An alternative INPUT PATH to promise creation. `POST /promises` takes three
structured fields; `POST /promises/from-text` takes a customer's message and works
out what those three fields are. Both then call the same `create_promise`. There is
no second way for a promise to exist, and no second set of rules governing one once
it does.

The reuse is literal, not thematic. This module builds a `PromiseRequest` — the
exact model the structured endpoint's body validates into — and passes it to
`app.ptp.service.create_promise`. It does not insert a promise, does not touch the
promise state machine, does not transition an event status, and does not know how
either of those works. Everything downstream of a promise existing is identical
between the two paths because it is the same code.

THE ORDER, AND WHY EACH STEP IS WHERE IT IS
-------------------------------------------
    1. resolve the reference clock       (received_at, or now)
    2. CHECK THE EVENT CAN TAKE A PROMISE  <- before any API call
    3. ask the model                     (the one quota-spending step)
    4. evaluate the proposal             (pure; no I/O, no clock)
    5. WRITE THE AUDIT RECORD            <- before the promise, always
    6. create the promise, if accepted   (the existing, unchanged function)
    7. link the record to the promise

Step 2 is before step 3 for two reasons. It saves an API call on an event that could
never carry a promise anyway, which matters on a free tier. More importantly it
removes most of the ways step 6 can fail *after* a call has been paid for and an
audit record already written.

Step 5 is before step 6 so that the failure mode is an orphan and not a ghost. A
crash between them leaves an extraction with `accepted: true` and `promise_id: null`
— something an auditor can find and ask about. The reverse order would leave a
promise with no record of the text it came from, which is the one thing this stage
exists to prevent.

WHAT AN EXTRACTION CAN AND CANNOT DO
------------------------------------
It cannot pay, authorize, contact, or change any promise that already exists. It can
set a date, and a date has a consequence: it determines when a promise breaks, and
therefore when a follow-up is *considered*. `PromiseRequest`'s docstring raises
exactly this objection and it is answered rather than dismissed — the follow-up still
goes through `authorize_event`, still requires an `UnpaidConfirmation`, and still
obeys the opt-out, the contact cap and the cooldown. So the worst outcome of a wrong
extraction is a contact at the wrong *time*. Not an unauthorized contact, not a
different amount of money moving, and not a promise marked honored without a
verification behind it.

The bounds on how wrong the timing can get are the date window and the confidence
floor in `app/models/promise_extraction.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.models.promise import PromiseRequest
from app.models.promise_extraction import (
    CONFIDENCE_FLOOR,
    MAX_RAW_TEXT_CHARS,
    ExtractionOutcome,
    LLMPromiseProposal,
    PromiseExtraction,
    PromiseFromTextRequest,
    PromiseFromTextResponse,
    evaluate_proposal,
)
from app.ptp import extraction_store, gemini, store
from app.ptp.service import create_promise

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Current UTC time, truncated to milliseconds to match what BSON stores."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _event_amount(event: dict) -> tuple[float, str]:
    """Read the amount at risk and its currency off a stored event.

    Raises:
        RuntimeError: the event has no amount. That is a defect in stored data, not
            a bad request, and it should surface as a 500 rather than be papered over
            with a zero — an amount of zero would make every stated figure exceed it
            and turn every extraction into an `amount_exceeds_at_risk` refusal.
    """
    amount = event.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise RuntimeError(
            f"event {event.get('event_id')!r} has no usable `amount` "
            f"({amount!r}); an extraction cannot infer or bound a promised amount "
            "without one"
        )
    return float(amount), str(event.get("currency") or "INR")


async def _record_attempt(
    *,
    event_id: str,
    raw_text: str,
    received_at: datetime,
    outcome: ExtractionOutcome,
    llm_model: str | None,
    raw_response: str | None,
) -> str:
    """Write the audit record for one attempt and return its id.

    Called on every path — accepted, refused, and unreachable — which is the whole
    argument for this collection existing separately from `PromiseToPay`. A refusal
    has no promise to be a field on, and refusals are the records that show the
    guardrail working.
    """
    extraction = PromiseExtraction(
        event_id=event_id,
        raw_text=raw_text,
        received_at=received_at,
        accepted=outcome.accepted,
        promise_id=None,
        promised_date=(
            outcome.promised_date.isoformat()
            if outcome.promised_date is not None
            else None
        ),
        promised_amount=outcome.promised_amount,
        amount_inferred=outcome.amount_inferred,
        confidence=outcome.confidence,
        confidence_floor=CONFIDENCE_FLOOR,
        quote=outcome.quote,
        quote_verified=outcome.quote_verified,
        refusal_reason=outcome.refusal_reason,
        llm_model=llm_model,
        raw_response=(
            raw_response[:MAX_RAW_TEXT_CHARS] if raw_response is not None else None
        ),
    )
    return await extraction_store.insert(extraction)


def _unavailable_outcome(reason: str, detail: str) -> ExtractionOutcome:
    """The outcome for a refusal that happened before any response was validated.

    Zero confidence and no quote, because there is no proposal behind it — a number
    here would be a number with no source, which
    `PromiseExtraction._pre_proposal_refusals_have_no_extracted_detail` refuses to
    store.
    """
    return ExtractionOutcome(
        accepted=False,
        promised_date=None,
        promised_amount=None,
        amount_inferred=False,
        confidence=0.0,
        quote=None,
        quote_verified=False,
        refusal_reason=reason,
        detail=detail,
    ).assert_consistent()


async def extract_promise(request: PromiseFromTextRequest) -> PromiseFromTextResponse:
    """Read a commitment out of a customer's message and record it as a promise.

    Returns:
        The outcome, including a refusal. A refusal is a successful call: no
        commitment was found, so none was invented, and the response says which check
        declined. Callers distinguish the two on `commitment_found`, not on status.

    Raises:
        store.EventNotFound: no such event. Checked before any API call is spent.
        store.EventSettled: the event is already recovered, so there is nothing left
            to promise to pay.
        store.DuplicatePromise: a different amount is already promised for the
            extracted date. A genuine conflict; the attempt record is kept, and it
            will show `accepted: true` with no linked promise.
    """
    received_at = request.received_at or _utc_now()

    # --- step 2: refuse before spending a call ------------------------------
    event = await store.assert_event_promisable(request.event_id)
    amount_at_risk, currency = _event_amount(event)

    # --- step 3: the one step that costs anything --------------------------
    model_name = get_settings().gemini_model if gemini.is_configured() else None
    proposal: LLMPromiseProposal | None = None
    raw_response: str | None = None

    try:
        proposal, raw_response = await gemini.propose_promise(
            raw_text=request.raw_text,
            received_at=received_at,
            amount_at_risk=amount_at_risk,
            currency=currency,
        )
    except gemini.PromiseExtractionUnavailable as exc:
        logger.warning(
            "Promise extraction for event %r could not run (%s): %s",
            request.event_id,
            exc.reason,
            exc,
        )
        outcome = _unavailable_outcome(exc.reason, str(exc))
        # The raw response is kept even here: an `unparseable_response` refusal is
        # only auditable against what the model actually said.
        extraction_id = await _record_attempt(
            event_id=request.event_id,
            raw_text=request.raw_text,
            received_at=received_at,
            outcome=outcome,
            llm_model=model_name,
            raw_response=str(exc),
        )
        return _respond(
            request=request,
            received_at=received_at,
            extraction_id=extraction_id,
            outcome=outcome,
            promise=None,
            created=False,
            llm_model=model_name,
        )

    # --- step 4: pure evaluation -------------------------------------------
    outcome = evaluate_proposal(
        proposal,
        raw_text=request.raw_text,
        received_at=received_at,
        amount_at_risk=amount_at_risk,
        currency=currency,
    )

    # --- step 5: the audit record, before the promise ----------------------
    extraction_id = await _record_attempt(
        event_id=request.event_id,
        raw_text=request.raw_text,
        received_at=received_at,
        outcome=outcome,
        llm_model=model_name,
        raw_response=raw_response,
    )

    if not outcome.accepted:
        logger.info(
            "No promise created for event %r: %s",
            request.event_id,
            outcome.refusal_reason,
        )
        return _respond(
            request=request,
            received_at=received_at,
            extraction_id=extraction_id,
            outcome=outcome,
            promise=None,
            created=False,
            llm_model=model_name,
        )

    # --- step 6: THE EXISTING PATH, unchanged ------------------------------
    # `PromiseRequest` is the same model `POST /promises` validates its body into,
    # and `create_promise` is the same function that endpoint calls. Nothing about
    # the promise that comes out of here differs from a structured one, because
    # nothing about how it is made differs.
    assert outcome.promised_date is not None and outcome.promised_amount is not None
    promise, created = await create_promise(
        PromiseRequest(
            event_id=request.event_id,
            promised_amount=outcome.promised_amount,
            promised_date=outcome.promised_date,
        )
    )

    # --- step 7: link them -------------------------------------------------
    await extraction_store.link_promise(
        extraction_id=extraction_id, promise_id=promise.id
    )

    return _respond(
        request=request,
        received_at=received_at,
        extraction_id=extraction_id,
        outcome=outcome,
        promise=promise,
        created=created,
        llm_model=model_name,
    )


def _respond(
    *,
    request: PromiseFromTextRequest,
    received_at: datetime,
    extraction_id: str,
    outcome: ExtractionOutcome,
    promise,
    created: bool,
    llm_model: str | None,
) -> PromiseFromTextResponse:
    """Assemble the response. One construction site, so every path reports the same shape."""
    detail = outcome.detail
    if outcome.accepted and promise is not None:
        detail = (
            f"{outcome.detail}. Promise {promise.id} "
            f"{'recorded' if created else 'already existed and is unchanged'}"
        )

    return PromiseFromTextResponse(
        event_id=request.event_id,
        commitment_found=outcome.accepted,
        created=created,
        extraction_id=extraction_id,
        raw_text=request.raw_text,
        received_at=received_at,
        promise=promise,
        promised_amount_inferred=outcome.amount_inferred,
        confidence=outcome.confidence,
        confidence_floor=CONFIDENCE_FLOOR,
        quote=outcome.quote,
        quote_verified=outcome.quote_verified,
        refusal_reason=outcome.refusal_reason,
        llm_model=llm_model,
        detail=detail,
    )
