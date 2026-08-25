"""Stage 6 Part A — Verification (inbound webhooks).

Receives Razorpay webhooks, verifies their signature against the raw request bytes,
and reconciles each one against the executed action that produced the payment link
it reports on. The output is a `VerificationRecord`: a statement about an outcome,
and nothing more.

The public surface, in the order a request travels it:

* `accept` — verify a raw body and produce a `VerifiedWebhook`, or refuse. The only
  constructor of that type;
* `reconcile` — match, record, transit. Accepts `VerifiedWebhook` and no other type,
  which is what makes "verify before processing" a property of the code's shape
  rather than of the order two statements happen to be written in;
* `has_recovered` — the single definition of "has this event's money come back",
  used by promise-to-pay's mandatory re-check in Part B;
* `ensure_indexes`, `list_verifications`, `find_by_razorpay_event_id`,
  `transition_event_status` — persistence.

Nothing here diagnoses, decides, or consults policy. Those stages already ran.
"""

from app.models.verification import (
    ALLOWED_OUTCOMES,
    AMOUNT_TOLERANCE,
    OUTCOME_FOR_EVENT,
    RECOVERED_OUTCOME,
    SUBSCRIBED_EVENTS,
    RazorpayLinkEvent,
    VerificationOutcome,
    VerificationRecord,
    VerificationRecordDocument,
    WebhookAck,
    amounts_differ,
)
from app.webhooks.service import (
    STATUS_FOR_OUTCOME,
    from_minor_units,
    has_recovered,
    link_entity,
    reconcile,
)
from app.webhooks.signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    BodyTooLarge,
    MalformedBody,
    MalformedSignature,
    MissingEventId,
    MissingSignature,
    SignatureMismatch,
    VerifiedWebhook,
    WebhookRejected,
    WebhookSecretNotConfigured,
    accept,
    expected_signature,
)
from app.webhooks.store import (
    COLLECTION_NAME,
    DanglingExecutionReference,
    DuplicateVerification,
    LinkMismatch,
    StatusTransition,
    VerificationReferenceError,
    current_event_status,
    ensure_indexes,
    find_by_razorpay_event_id,
    find_execution_by_link_id,
    list_for_event,
    list_verifications,
    transition_event_status,
)

__all__ = [
    "ALLOWED_OUTCOMES",
    "AMOUNT_TOLERANCE",
    "COLLECTION_NAME",
    "EVENT_ID_HEADER",
    "OUTCOME_FOR_EVENT",
    "RECOVERED_OUTCOME",
    "SIGNATURE_HEADER",
    "STATUS_FOR_OUTCOME",
    "SUBSCRIBED_EVENTS",
    "BodyTooLarge",
    "DanglingExecutionReference",
    "DuplicateVerification",
    "LinkMismatch",
    "MalformedBody",
    "MalformedSignature",
    "MissingEventId",
    "MissingSignature",
    "RazorpayLinkEvent",
    "SignatureMismatch",
    "StatusTransition",
    "VerificationOutcome",
    "VerificationRecord",
    "VerificationRecordDocument",
    "VerificationReferenceError",
    "VerifiedWebhook",
    "WebhookAck",
    "WebhookRejected",
    "WebhookSecretNotConfigured",
    "accept",
    "amounts_differ",
    "current_event_status",
    "ensure_indexes",
    "expected_signature",
    "find_by_razorpay_event_id",
    "find_execution_by_link_id",
    "from_minor_units",
    "has_recovered",
    "link_entity",
    "list_for_event",
    "list_verifications",
    "reconcile",
    "transition_event_status",
]
