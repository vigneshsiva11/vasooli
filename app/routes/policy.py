"""Stage 4 — Policy endpoints.

Decides whether an event's current recommendation is permitted, reads verdicts
back, and maintains the do-not-contact list.

There is deliberately no endpoint here that executes an authorized action, and no
endpoint that manually approves a `requires_manual_review` verdict. The first is
Stage 5. The second is a real future capability that the verdict vocabulary keeps
open, but approving on a human's behalf is not something this stage invents.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app import decision as decision_stage
from app import ingestion
from app import policy as policy_stage
from app.models import DecisionRecord, RevenueEvent
from app.models.policy import (
    CustomerOptOut,
    OptOutRequest,
    OptOutResponse,
    PolicyVerdictRecord,
)
from app.routes.events import database_ready

router = APIRouter(
    tags=["policy"],
    dependencies=[Depends(database_ready)],
)


@router.post(
    "/authorize/{event_id}",
    response_model=PolicyVerdictRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Authorize, block, or flag for review an event's current recommendation",
)
async def authorize_event(event_id: str) -> PolicyVerdictRecord:
    """Evaluate policy against an event's latest decision.

    Always evaluates every check, so the returned `checks_performed` is the full
    trail rather than the first thing that failed. Never executes anything: the
    verdict is permission, and acting on it is Stage 5.
    """
    event_document = await ingestion.get_event(event_id)
    if event_document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No event with event_id {event_id!r}.",
        )

    decision_document = await decision_stage.latest_decision(event_id)
    if decision_document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Event {event_id!r} has no decision yet. Run POST "
                f"/decide/{event_id} first — this stage authorizes an existing "
                "recommendation and will not invent one to authorize."
            ),
        )

    event = RevenueEvent.model_validate(
        {key: value for key, value in event_document.items() if key != "_id"}
    )
    decision = DecisionRecord.from_document(decision_document)

    context = await policy_stage.gather_context(
        decision=decision, customer_ref=event.customer_ref
    )
    verdict = policy_stage.evaluate(decision=decision, context=context)

    try:
        document_id, version = await policy_stage.append(verdict)
    except policy_stage.StaleDecisionReference as exc:
        # Reachable if the event is re-decided between reading the latest decision
        # above and writing the verdict. Refused rather than retried: authorizing
        # a recommendation that has just been superseded is exactly what the guard
        # exists to prevent.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except policy_stage.DanglingDecisionReference as exc:  # pragma: no cover
        # Unreachable via this route, which reads the decision it references.
        # Surfaced rather than swallowed in case a future caller is less careful.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return PolicyVerdictRecord(
        id=document_id,
        version=version,
        **verdict.model_dump(),
    )


@router.get(
    "/policy-verdicts",
    response_model=list[PolicyVerdictRecord],
    summary="List policy verdicts",
)
async def list_policy_verdicts(
    event_id: str | None = Query(
        default=None,
        description="Restrict to one event.",
    ),
    verdict: str | None = Query(
        default=None,
        description=(
            "Restrict to one verdict value: authorized, blocked, or "
            "requires_manual_review."
        ),
    ),
    history: bool = Query(
        default=False,
        description=(
            "False (default) returns only the current verdict per event. True "
            "returns every version, so a changed verdict can be compared with "
            "what it replaced."
        ),
    ),
) -> list[PolicyVerdictRecord]:
    """Return stored verdicts, newest first."""
    if verdict is not None and verdict not in policy_stage.ALLOWED_VERDICTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{verdict!r} is not a verdict value. Allowed: "
                f"{sorted(policy_stage.ALLOWED_VERDICTS)}."
            ),
        )

    documents = await policy_stage.list_verdicts(
        event_id=event_id, history=history, verdict=verdict
    )
    return [PolicyVerdictRecord.from_document(document) for document in documents]


@router.post(
    "/opt-out/{customer_ref}",
    response_model=OptOutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a customer to the do-not-contact list",
)
async def opt_out_customer(
    customer_ref: str,
    body: OptOutRequest = Body(default_factory=OptOutRequest),
) -> OptOutResponse:
    """Record that a customer must not be contacted.

    A stand-in for a real customer-preference service, sufficient to demonstrate
    that consent gates the contact-type interventions. Idempotent: opting out a
    customer twice keeps the original timestamp.
    """
    if not customer_ref.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="customer_ref must not be blank.",
        )

    opt_out = CustomerOptOut(customer_ref=customer_ref, reason=body.reason)
    created = await policy_stage.add_opt_out(opt_out)

    if created:
        return OptOutResponse(created=True, **opt_out.model_dump())

    existing = [
        record
        for record in await policy_stage.list_opt_outs()
        if record["customer_ref"] == customer_ref
    ]
    return OptOutResponse(created=False, **existing[0])


@router.get(
    "/opt-outs",
    response_model=list[CustomerOptOut],
    summary="List opted-out customers",
)
async def list_opted_out_customers() -> list[CustomerOptOut]:
    """Return every customer on the do-not-contact list, most recent first."""
    return [
        CustomerOptOut.model_validate(record)
        for record in await policy_stage.list_opt_outs()
    ]
