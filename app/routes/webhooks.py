"""Stage 6 Part A — the inbound webhook endpoint and the verification read.

Two routers, and they are separate on purpose.

`webhook_router` carries **no** `database_ready` dependency. Signature rejection has
to be unconditional: if Mongo is down, an unsigned or forged request must still be
answered 400 rather than 503. A 503 would tell an attacker probing the endpoint that
the request got as far as the database, and it would tell an operator reading logs
that a forgery was an infrastructure blip. The database is reached only after the
digest matches, and a database failure at that point surfaces as a 500, which is
what Razorpay should retry on.

`router` carries the dependency, because `GET /verifications` is a plain read and has
nothing to say without a database.

**The raw body.** The handler takes `Request` and reads `await request.body()`, not a
Pydantic model. FastAPI would otherwise parse the JSON and hand over a dict, and the
signature is over the bytes — see `app/webhooks/signature.py` for why re-serialising
them is not a round trip. There is no `response_model` coercion on the way in for the
same reason.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.models.verification import (
    ALLOWED_OUTCOMES,
    VerificationRecordDocument,
    WebhookAck,
)
from app.routes.events import database_ready
from app.webhooks import service, signature, store

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["verification"])

router = APIRouter(
    tags=["verification"],
    dependencies=[Depends(database_ready)],
)


@webhook_router.post(
    "/webhooks/razorpay",
    response_model=WebhookAck,
    summary="Receive a signature-verified Razorpay payment-link webhook",
)
async def receive_razorpay_webhook(request: Request) -> WebhookAck:
    """Verify, then reconcile, an inbound Razorpay webhook.

    The two steps are in that order and cannot be reordered: `service.reconcile`
    accepts a `VerifiedWebhook`, which only `signature.accept` can produce.

    Status codes, and the reasoning behind each:

    * **400** — missing, malformed or non-matching signature; a verified body that
      is not a webhook; a verified body whose payment-link payload cannot be read;
      or a missing `x-razorpay-event-id`. Nothing is processed and nothing is
      written. Razorpay will retry, which is correct for all of these: either the
      request was not Razorpay's, or it was and this build genuinely cannot read it.
    * **503** — `RAZORPAY_WEBHOOK_SECRET` is not configured. Not 400: the request may
      be perfectly valid and the fault is entirely ours. Answering 400 would be
      indistinguishable in the logs from a forgery.
    * **200** — the request was authentic. Includes the cases where nothing was
      recorded: a duplicate delivery, an unsubscribed event, or a link no execution
      claims. Razorpay treats a non-2xx as a delivery failure and retries with
      backoff for 24 hours before disabling the endpoint, so acknowledging an
      authentic request this system cannot act on is the only sane answer. The body
      says which case it was; `processed` is the field to read, not the status code.
    """
    body = await request.body()

    try:
        verified = signature.accept(
            body=body,
            signature=request.headers.get(signature.SIGNATURE_HEADER),
            razorpay_event_id=request.headers.get(signature.EVENT_ID_HEADER),
        )
    except signature.WebhookSecretNotConfigured as exc:
        logger.error("Inbound webhook could not be verified: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except signature.WebhookRejected as exc:
        # One log line, one 400. The reason is the exception's class name so the log
        # distinguishes unsigned from wrongly-signed without printing the digest that
        # would have worked.
        logger.warning(
            "Rejected inbound webhook (%s): %s",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    try:
        return await service.reconcile(verified)
    except signature.MalformedBody as exc:
        # Raised by the reconciler for a subscribed event whose payload it cannot
        # read. Verified as Razorpay's, and still a 400 — see `service.link_entity`.
        logger.warning(
            "Verified webhook %s could not be read: %s", verified.razorpay_event_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@router.get(
    "/verifications",
    response_model=list[VerificationRecordDocument],
    summary="List verification records",
)
async def list_verification_records(
    event_id: str | None = Query(
        default=None,
        description="Restrict to one event.",
    ),
    outcome: str | None = Query(
        default=None,
        description="Restrict to one outcome: recovered, expired, cancelled, not_recovered.",
    ),
    history: bool = Query(
        default=False,
        description=(
            "False (default) returns only the most recent verification per event. "
            "True returns every one, so a link that expired and a later link that "
            "was paid are both visible."
        ),
    ),
) -> list[VerificationRecordDocument]:
    """Return stored verifications, newest first."""
    if outcome is not None and outcome not in ALLOWED_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{outcome!r} is not a verification outcome. Allowed: "
                f"{sorted(ALLOWED_OUTCOMES)}."
            ),
        )

    documents = await store.list_verifications(
        event_id=event_id, history=history, outcome=outcome
    )
    return [
        VerificationRecordDocument.from_document(document) for document in documents
    ]
