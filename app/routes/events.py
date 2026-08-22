"""Stage 1 — Ingestion endpoints.

Accepts revenue-at-risk events and reads them back. Deliberately does nothing
else: no diagnosis, no scoring, no side effects beyond the write.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app import ingestion
from app.db import get_database
from app.models import EventCreatedResponse, RevenueEvent, RevenueEventRecord


async def database_ready() -> None:
    """Reject the request with 503 when MongoDB is not connected.

    Startup is deliberately non-fatal if the database is unreachable, so the routes
    that need it translate that into a clear service-unavailable response instead of
    an opaque 500.
    """
    try:
        get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable.",
        ) from exc


router = APIRouter(
    prefix="/events",
    tags=["events"],
    dependencies=[Depends(database_ready)],
)


@router.post(
    "",
    response_model=EventCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a revenue-at-risk event",
    responses={
        200: {"description": "Event already existed and was refreshed in place."},
        201: {"description": "Event was newly created."},
    },
)
async def create_event(event: RevenueEvent, response: Response) -> EventCreatedResponse:
    """Validate an incoming event and persist it, keyed on `event_id`.

    Re-posting a known `event_id` updates that event rather than creating a second
    one, and answers 200 instead of 201.
    """
    document_id, created = await ingestion.upsert_event(event)
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return EventCreatedResponse(id=document_id, event_id=event.event_id)


@router.get(
    "",
    response_model=list[RevenueEventRecord],
    summary="List ingested events",
)
async def list_events() -> list[RevenueEventRecord]:
    """Return every stored event, newest first."""
    documents = await ingestion.list_events()
    return [RevenueEventRecord.from_document(document) for document in documents]
