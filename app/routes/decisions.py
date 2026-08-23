"""Stage 3 — Decision endpoints.

Produces a recommendation from an event's latest diagnosis and reads
recommendations back. Writes only to the `decisions` collection.

There is deliberately no endpoint here that approves, rejects, or executes a
recommendation. Authorization is Stage 4 and lives behind its own surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import decision as decision_stage
from app import diagnosis as diagnosis_stage
from app import ingestion
from app.models import DecisionRecord, DiagnosisRecord, RevenueEvent
from app.routes.events import database_ready

router = APIRouter(
    tags=["decision"],
    dependencies=[Depends(database_ready)],
)


@router.post(
    "/decide/{event_id}",
    response_model=DecisionRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Recommend an intervention for an event's latest diagnosis",
)
async def decide_event(event_id: str) -> DecisionRecord:
    """Score the permitted interventions for an event and recommend the best.

    Decides from the *latest* diagnosis version and pins that version's document
    id, so re-diagnosing an event and re-deciding it produces a new decision
    rather than silently changing the basis of the old one.
    """
    document = await ingestion.get_event(event_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No event with event_id {event_id!r}.",
        )

    diagnosis_document = await diagnosis_stage.latest_diagnosis(event_id)
    if diagnosis_document is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Event {event_id!r} has no diagnosis yet. Run POST "
                f"/diagnose/{event_id} first — a decision without an explanation "
                "to base it on is not something this stage will invent."
            ),
        )

    event = RevenueEvent.model_validate(
        {key: value for key, value in document.items() if key != "_id"}
    )
    diagnosis = DiagnosisRecord.from_document(diagnosis_document)

    decision = decision_stage.decide(diagnosis=diagnosis, event=event)

    try:
        document_id, version = await decision_stage.append(decision)
    except decision_stage.DanglingDiagnosisReference as exc:  # pragma: no cover
        # Unreachable via this route, which reads the diagnosis it references.
        # Surfaced rather than swallowed in case a future caller is less careful.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return DecisionRecord(
        id=document_id,
        version=version,
        **decision.model_dump(),
    )


@router.get(
    "/decisions",
    response_model=list[DecisionRecord],
    summary="List decisions",
)
async def list_decisions(
    event_id: str | None = Query(
        default=None,
        description="Restrict to one event.",
    ),
    history: bool = Query(
        default=False,
        description=(
            "False (default) returns only the current recommendation per event. "
            "True returns every version, so a re-decision can be compared with "
            "what it replaced."
        ),
    ),
) -> list[DecisionRecord]:
    """Return stored decisions, newest first."""
    documents = await decision_stage.list_decisions(event_id=event_id, history=history)
    return [DecisionRecord.from_document(document) for document in documents]
