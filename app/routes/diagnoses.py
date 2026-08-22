"""Stage 2 — Diagnosis endpoints.

Runs diagnosis on a stored event and reads diagnoses back. Writes only to the
`diagnoses` collection: the originating `RevenueEvent` is read and left untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import diagnosis as diagnosis_stage
from app import ingestion
from app.models import DiagnosisRecord, RevenueEvent
from app.routes.events import database_ready

router = APIRouter(
    tags=["diagnosis"],
    dependencies=[Depends(database_ready)],
)


@router.post(
    "/diagnose/{event_id}",
    response_model=DiagnosisRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Diagnose why a stored event is at risk",
)
async def diagnose_event(event_id: str) -> DiagnosisRecord:
    """Explain one event, and append the explanation to its diagnosis history.

    Rules classify the clear-cut cases; Gemini is consulted only when they cannot.
    Either way the result is validated against the surface's closed root-cause set
    before it is stored.
    """
    document = await ingestion.get_event(event_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No event with event_id {event_id!r}.",
        )

    event = RevenueEvent.model_validate(
        {key: value for key, value in document.items() if key != "_id"}
    )

    prior_event_count = await ingestion.count_prior_events(
        event.customer_ref, event.created_at, event.event_id
    )

    diagnosis, method = await diagnosis_stage.diagnose(
        event, prior_event_count=prior_event_count
    )
    document_id, version = await diagnosis_stage.append(diagnosis, method)

    return DiagnosisRecord(
        id=document_id,
        version=version,
        method=method,
        **diagnosis.model_dump(),
    )


@router.get(
    "/diagnoses",
    response_model=list[DiagnosisRecord],
    summary="List diagnoses",
)
async def list_diagnoses(
    event_id: str | None = Query(
        default=None,
        description="Restrict to one event's diagnosis history.",
    ),
) -> list[DiagnosisRecord]:
    """Return stored diagnoses, newest first, across all versions."""
    documents = await diagnosis_stage.list_diagnoses(event_id=event_id)
    return [DiagnosisRecord.from_document(document) for document in documents]
