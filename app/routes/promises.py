"""Stage 6 Part B / Stage 10 — Promise-to-pay endpoints.

`POST /promises` records a commitment from structured fields. `POST
/promises/from-text` works those fields out of a customer's message first and then
records it the same way. `GET /promises` reads them back, `GET
/promise-extractions` reads back what was extracted and from what, and
`POST /promises/{event_id}/check` resolves a promise against reality.

Two things this router deliberately does not offer:

* **no way to set a promise's state.** There is no `PATCH /promises/{id}` and no
  `state` field on either create request. States are reached only by the transitions
  in `app/models/promise.py`, each of which is produced by evidence — a
  verification record, or a date having passed. A promise that could be marked
  `honored` by hand would make the honored state mean nothing;
* **no way to send a follow-up.** There is no `POST /promises/{id}/follow-up`. A
  contact only happens as a consequence of `check`, which re-checks payment status
  first and routes through the policy gate — see `app/ptp/service.py`. An endpoint
  that sent a message on request would be exactly the ungated path this stage
  exists to avoid.

The two create endpoints are alternative inputs, not alternative rules. Both build a
`PromiseRequest` and both hand it to the same `ptp.create_promise`, so a promise
extracted from free text is indistinguishable downstream from one typed in by hand —
see `app/ptp/extraction.py`.

`POST .../check` stands in for a scheduled job. In production a sweep would call it
for every open promise whose date has passed; the difference is only what triggers
it, because the payment re-check lives inside the function and not in its caller.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import ptp
from app.models.promise import (
    ALLOWED_PROMISE_STATES,
    PromiseCheck,
    PromiseRequest,
    PromiseToPayDocument,
)
from app.models.promise_extraction import (
    PromiseExtractionDocument,
    PromiseFromTextRequest,
    PromiseFromTextResponse,
)
from app.routes.events import database_ready

router = APIRouter(
    tags=["promise-to-pay"],
    dependencies=[Depends(database_ready)],
)


@router.post(
    "/promises",
    response_model=PromiseToPayDocument,
    status_code=status.HTTP_201_CREATED,
    summary="Record a customer's commitment to pay by a date",
)
async def create_promise(
    body: PromiseRequest, response: Response
) -> PromiseToPayDocument:
    """Record a promise to pay and move its event to `awaiting_promise`.

    Returns 201 when this call recorded the promise and 200 when it returned an
    identical one that already existed — the same idempotency convention
    `POST /execute/{event_id}` uses.

    Raises:
        HTTPException 404: no such event.
        HTTPException 409: a promise already exists for this event and date, for a
            different amount. Two amounts cannot both be what was promised.
        HTTPException 422: the event has already been recovered, so there is nothing
            left to promise to pay.
    """
    try:
        record, created = await ptp.create_promise(body)
    except ptp.EventNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ptp.EventSettled as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ptp.DuplicatePromise as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{exc} — a second amount for the same date is a conflict, not a "
                "retry. Record a new date, or check the existing promise."
            ),
        ) from exc

    if not created:
        response.status_code = status.HTTP_200_OK
    return record


@router.post(
    "/promises/from-text",
    response_model=PromiseFromTextResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Extract a commitment from a customer's message, then record it",
)
async def create_promise_from_text(
    body: PromiseFromTextRequest, response: Response
) -> PromiseFromTextResponse:
    """Read a promise out of free text and record it through the existing path.

    An alternative input to `POST /promises`, not a replacement for it. The message
    is parsed into the same three fields that endpoint takes, and then handed to the
    same `create_promise` — so the resulting promise is identical in shape, state and
    downstream behaviour to a structured one.

    Relative dates are resolved against `received_at`, the message's own timestamp,
    not against today. A message recorded a week late still yields the date the
    customer meant, which means an extracted promise can legitimately be overdue the
    moment it is created.

    **A refusal is a 200, not an error.** When the message contains no defensible
    commitment — "I'm still thinking about it" — no promise is created,
    `commitment_found` is False, and `refusal_reason` names the check that declined.
    Declining to invent a commitment is this endpoint working. Every attempt is
    recorded either way, and `extraction_id` points at that record.

    Returns 201 when a new promise was written, 200 when an identical one already
    existed or when nothing was extracted — the same convention `POST /promises` and
    `POST /execute/{event_id}` use.

    Raises:
        HTTPException 404: no such event. Checked before any API call is spent.
        HTTPException 409: a different amount is already promised for the extracted
            date. The attempt is still recorded, showing an accepted extraction with
            no linked promise.
        HTTPException 422: the event has already been recovered, so there is nothing
            left to promise to pay; or the request body was refused — a naive or
            future `received_at`, or an empty or over-long `raw_text`.
        HTTPException 503: extraction could not run at all because no API key is
            configured. Distinct from a refusal: a refusal means the message was read
            and found wanting, this means it was never read. Use `POST /promises` to
            record the promise directly.
    """
    try:
        result = await ptp.extract_promise(body)
    except ptp.EventNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ptp.EventSettled as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ptp.DuplicatePromise as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{exc} — the message was read and a commitment extracted, but a "
                "different amount is already promised for that date. Two amounts "
                "cannot both be what was promised. The extraction attempt is "
                "recorded; check the existing promise."
            ),
        ) from exc

    # `llm_unavailable` is the one refusal that is not a judgement about the message.
    # Reporting it as a 200 alongside "no commitment was found" would tell a caller
    # the customer said nothing usable, when in fact nobody looked.
    if result.refusal_reason == "llm_unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{result.detail} The attempt is recorded as "
                f"{result.extraction_id}."
            ),
        )

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/promises",
    response_model=list[PromiseToPayDocument],
    summary="List recorded promises",
)
async def list_promises(
    event_id: str | None = Query(default=None, description="Restrict to one event."),
    state: str | None = Query(
        default=None,
        description="Restrict to one state: promised, honored, broken, or reevaluating.",
    ),
    history: bool = Query(
        default=False,
        description=(
            "False (default) returns only the most recent promise per event. True "
            "returns every one, so a broken promise and the commitment that "
            "replaced it are both visible."
        ),
    ),
) -> list[PromiseToPayDocument]:
    """Return stored promises, newest first."""
    if state is not None and state not in ALLOWED_PROMISE_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{state!r} is not a promise state. Allowed: "
                f"{sorted(ALLOWED_PROMISE_STATES)}."
            ),
        )

    documents = await ptp.list_promises(event_id=event_id, history=history, state=state)
    return [ptp.as_record(document) for document in documents]


@router.get(
    "/promise-extractions",
    response_model=list[PromiseExtractionDocument],
    summary="List free-text extraction attempts, accepted and refused",
)
async def list_promise_extractions(
    event_id: str | None = Query(default=None, description="Restrict to one event."),
    accepted: bool | None = Query(
        default=None,
        description=(
            "True returns only attempts that produced a promise, False only the "
            "refusals. Omit for both."
        ),
    ),
) -> list[PromiseExtractionDocument]:
    """Return what customers said and what was extracted from it, newest first.

    Every attempt, including the ones that produced nothing. That is the point of the
    collection: a refusal has no promise to be a field on, so an audit that lived on
    the promise record would lose exactly the cases where a guardrail fired.

    There is no latest-per-event collapse here, unlike `GET /promises`. Three attempts
    on one event are three things that happened, and showing only the last would hide
    the two refusals before it.
    """
    documents = await ptp.list_extractions(event_id=event_id, accepted=accepted)
    return [PromiseExtractionDocument.from_document(document) for document in documents]


@router.post(
    "/promises/{event_id}/check",
    response_model=PromiseCheck,
    summary="Re-check payment, then resolve the promise against its deadline",
)
async def check_promise(event_id: str) -> PromiseCheck:
    """Resolve an event's current promise.

    ALWAYS re-checks whether the money has arrived before anything else, and never
    sends a follow-up when it has. The response reports the re-check explicitly —
    `payment_rechecked_at` is never null — so a caller can see that it happened
    rather than trusting that it did.

    Raises:
        HTTPException 404: the event has no promise, or (from the policy gate) no
            decision to authorize a follow-up against.
        HTTPException 409: propagated from the policy or execution stage when a
            reference changed underneath the call.
    """
    try:
        return await ptp.check_promise(event_id)
    except ptp.PromiseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ptp.StaleConfirmation as exc:  # pragma: no cover - milliseconds apart here
        # Cannot happen on this path: the confirmation is minted and used within the
        # same call. Surfaced rather than swallowed in case a future scheduler holds
        # one for longer, which is precisely the case the freshness check exists for.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
