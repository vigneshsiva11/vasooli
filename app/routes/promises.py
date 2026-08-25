"""Stage 6 Part B — Promise-to-pay endpoints.

`POST /promises` records a commitment. `GET /promises` reads them back.
`POST /promises/{event_id}/check` resolves one against reality.

Two things this router deliberately does not offer:

* **no way to set a promise's state.** There is no `PATCH /promises/{id}` and no
  `state` field on the create request. States are reached only by the transitions
  in `app/models/promise.py`, each of which is produced by evidence — a
  verification record, or a date having passed. A promise that could be marked
  `honored` by hand would make the honored state mean nothing;
* **no way to send a follow-up.** There is no `POST /promises/{id}/follow-up`. A
  contact only happens as a consequence of `check`, which re-checks payment status
  first and routes through the policy gate — see `app/ptp/service.py`. An endpoint
  that sent a message on request would be exactly the ungated path this stage
  exists to avoid.

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
