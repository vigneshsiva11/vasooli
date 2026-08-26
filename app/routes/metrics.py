"""Stage 7 — metrics and audit endpoints. Every route here is a GET.

This router deliberately offers nothing else. There is no `POST /metrics/refresh`,
no `PUT /metrics/summary`, and no `DELETE /audit-trail/{event_id}` — not as an
oversight to be filled in later, but because a dashboard data layer that could write
would be a second path into the stores that the policy gate does not sit in front of.
Every figure is recomputed from the collections on each request, so there is no cache
for a refresh endpoint to invalidate.

`app/metrics/verify_readonly.py` checks that mechanically against the running app's
OpenAPI schema, so the claim in this docstring is tested rather than trusted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app import metrics
from app.models.metrics import (
    AuditTrail,
    BaselineComparison,
    InterventionMetrics,
    MetricsSummary,
    PromiseMetrics,
    RootCauseMetrics,
)
from app.routes.events import database_ready

router = APIRouter(
    tags=["metrics"],
    dependencies=[Depends(database_ready)],
)


@router.get(
    "/metrics/summary",
    response_model=MetricsSummary,
    summary="Headline revenue-at-risk and recovery figures",
)
async def metrics_summary() -> MetricsSummary:
    """Compute the headline recovery numbers live.

    Nothing here is cached or stored, so two calls a second apart can legitimately
    differ if a webhook arrived between them.
    """
    return metrics.summarize(await metrics.load_snapshot())


@router.get(
    "/metrics/by-root-cause",
    response_model=list[RootCauseMetrics],
    summary="Revenue and recovery per diagnosed root cause",
)
async def metrics_by_root_cause() -> list[RootCauseMetrics]:
    """Return one row per root cause, ordered by revenue at risk descending."""
    return metrics.by_root_cause(await metrics.load_snapshot())


@router.get(
    "/metrics/by-intervention",
    response_model=list[InterventionMetrics],
    summary="Recommended, authorized, executed and recovered per intervention",
)
async def metrics_by_intervention() -> list[InterventionMetrics]:
    """Return the intervention-performance table, most-recommended first.

    `times_executed` can never exceed `times_authorized`: an execution requires an
    authorized verdict, enforced by a unique index on `policy_verdict_id` and by the
    write-time referential guard in `app/execution/store.py`. Read `verifiable`
    before reading `recovery_rate` — a contact-type intervention produces no
    Razorpay artifact, so no webhook can report a recovery for it and its rate is
    structurally zero.
    """
    return metrics.by_intervention(await metrics.load_snapshot())


@router.get(
    "/metrics/promise-to-pay",
    response_model=PromiseMetrics,
    summary="Promise outcomes and the honor rate over resolved promises",
)
async def metrics_promise_to_pay() -> PromiseMetrics:
    """Return promise counts and the honor rate, excluding still-open promises."""
    return metrics.promise_metrics(await metrics.load_snapshot())


@router.get(
    "/metrics/baseline-comparison",
    response_model=BaselineComparison,
    summary="SIMULATED naive strategies against Vasooli's real decisions",
)
async def metrics_baseline_comparison() -> BaselineComparison:
    """Compare two naive baselines against Vasooli, on one shared event set.

    THREE OF THE FOUR FIGURES ARE SIMULATED — what-if arithmetic over stored data,
    using the Stage 3 matrix's own probabilities applied differently. Only
    `vasooli_actual` is real money. The `methodology` field on the response says so
    too, because a number quoted out of this response should carry its own provenance.

    Nothing is executed, and no baseline writes a diagnosis, decision or verdict.
    """
    return metrics.compare(await metrics.load_snapshot())


@router.get(
    "/audit-trail/{event_id}",
    response_model=AuditTrail,
    summary="One event's complete history, ingestion to verification",
)
async def audit_trail(event_id: str) -> AuditTrail:
    """Assemble every record naming this event, in chronological order.

    A join across all seven stores and nothing more: each record is returned as its
    owning stage stored it, validated through that stage's own model.

    Raises:
        HTTPException 404: no event with this id.
        HTTPException 500: a stored record no longer satisfies its own stage's
            contract. Surfaced rather than silently coerced — this endpoint exists to
            report what the stores actually contain.
    """
    snapshot = await metrics.load_snapshot(event_id)
    try:
        return metrics.assemble(snapshot, event_id)
    except metrics.EventNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"A stored record for event {event_id!r} does not satisfy its own "
                f"stage's model, so the trail cannot be assembled honestly: {exc}"
            ),
        ) from exc
