"""Stage 5 — Execution endpoints.

`POST /execute/{event_id}` performs the action an event's *current* verdict
authorizes, at most once. `GET /executions` reads the record back.

Three things this router deliberately does not offer:

* no way to execute a specific verdict by id. Execution follows the event's latest
  verdict, so a caller cannot reach past a revocation to an older authorization that
  still says yes;
* no way to choose the action. What happens is determined by the intervention the
  verdict authorized, which was determined by the decision, which was determined by
  the diagnosis. A caller supplies an event id and nothing else;
* no way to force, retry, or override. A failed execution is recovered by
  re-authorizing — `POST /authorize/{event_id}` — which is a policy decision and
  belongs behind the policy gate, not behind a `?force=true`.

Nothing here reports whether money came back. That is Stage 6.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import execution as execution_stage
from app import ingestion
from app import policy as policy_stage
from app.models.execution import ALLOWED_ACTION_TYPES, ExecutionRecordDocument
from app.routes.events import database_ready

router = APIRouter(
    tags=["execution"],
    dependencies=[Depends(database_ready)],
)

ALLOWED_STATUSES = frozenset({"completed", "failed"})


@router.post(
    "/execute/{event_id}",
    response_model=ExecutionRecordDocument,
    status_code=status.HTTP_201_CREATED,
    summary="Carry out the action this event's current verdict authorizes",
)
async def execute_event(event_id: str, response: Response) -> ExecutionRecordDocument:
    """Execute the event's latest policy verdict, once.

    Returns 201 when this call produced the side effect and 200 when it returned a
    record an earlier call had already written. That distinction is the entire
    externally visible behaviour of idempotency, so it is worth reading literally: a
    200 here means *nothing happened this time*.

    Raises:
        HTTPException 404: no such event, or no verdict for it yet.
        HTTPException 409: the latest verdict does not authorize execution. The
            detail names the verdict and its reason — not a guess.
        HTTPException 503: Razorpay credentials are not configured, so no attempt
            could be made. Deliberately not recorded as a failed execution: nothing
            was sent, and a stored failure would release a contact-cap slot on the
            strength of an operator misconfiguration.
        HTTPException 500: a structural error — the authorized intervention has no
            executable action, or a reference the verdict depends on has vanished.
    """
    if await ingestion.get_event(event_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No event with event_id {event_id!r}.",
        )

    verdicts = await policy_stage.list_verdicts(event_id=event_id)
    if not verdicts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Event {event_id!r} has no policy verdict yet. Run POST "
                f"/authorize/{event_id} first — this stage carries out an existing "
                "authorization and will not grant one."
            ),
        )
    latest = verdicts[0]

    try:
        authorized = execution_stage.require_authorized(latest)
    except execution_stage.NotAuthorized as exc:
        # 409, and the detail is the stored verdict and reason rather than a
        # paraphrase. "Blocked" and "awaiting a human" are different situations and
        # the caller has to be able to tell them apart.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Event {event_id!r} verdict v{latest.get('version')} is "
                f"{latest.get('verdict')!r} because {latest.get('reason')!r}; "
                "execution is not permitted. "
                f"({exc})"
            ),
        ) from exc

    try:
        outcome = await execution_stage.execute(authorized)
    except execution_stage.RazorpayNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except execution_stage.VerdictReferenceError as exc:
        # The write-time guard refused. Reachable if the event is re-authorized
        # between the read above and the insert, which is exactly the race the guard
        # exists for.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (execution_stage.ExecutionError, execution_stage.NoTemplate) as exc:
        # Not a failed send — a broken pipeline. 500 rather than a stored failure,
        # because there is no action to record the outcome of.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return outcome.record


@router.get(
    "/executions",
    response_model=list[ExecutionRecordDocument],
    summary="List execution records",
)
async def list_execution_records(
    event_id: str | None = Query(
        default=None,
        description="Restrict to one event.",
    ),
    execution_status: str | None = Query(
        default=None,
        alias="status",
        description="Restrict to 'completed' or 'failed'.",
    ),
    action_type: str | None = Query(
        default=None,
        description=(
            "Restrict to one action type: payment_link_generated, retry_simulated, "
            "or contact_logged."
        ),
    ),
    history: bool = Query(
        default=False,
        description=(
            "False (default) returns only the most recent execution per event. True "
            "returns every one, so a failed attempt and the re-authorized execution "
            "that followed it are both visible."
        ),
    ),
) -> list[ExecutionRecordDocument]:
    """Return stored execution records, newest first."""
    if execution_status is not None and execution_status not in ALLOWED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{execution_status!r} is not an execution status. Allowed: "
                f"{sorted(ALLOWED_STATUSES)}."
            ),
        )
    if action_type is not None and action_type not in ALLOWED_ACTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{action_type!r} is not an action type. Allowed: "
                f"{sorted(ALLOWED_ACTION_TYPES)}."
            ),
        )

    documents = await execution_stage.list_executions(
        event_id=event_id, history=history, status=execution_status
    )
    records = [
        ExecutionRecordDocument.from_document(document) for document in documents
    ]
    if action_type is not None:
        # Filtered after the latest-per-event grouping rather than inside it, on
        # purpose: filtering first would promote an older execution to "latest" and
        # the default view would then show something that is not the current state.
        records = [record for record in records if record.action_type == action_type]
    return records
