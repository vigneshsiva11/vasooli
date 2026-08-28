"""Stage 6 Part A — Verification (inbound webhooks), plus Stage 9's receivable path.

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

**Stage 9 adds a second path, not a second door into the first one.**
`confirm_payment` records a merchant's assertion that a contact-type intervention was
paid — the case a webhook structurally cannot report on, because a logged contact
creates no Razorpay artifact. It is exported beside `reconcile` rather than hidden
behind it, and the two are kept apart on purpose: `reconcile` still takes a
`VerifiedWebhook` and nothing else. `VerificationRecord` is now a discriminated union
over `source`, so every reader can tell the gateway's word from a human's, and
`verification_document` is the way to parse a stored one (a union is not a class and
carries no `from_document`).

Nothing here diagnoses, decides, or consults policy. Those stages already ran.
"""

from app.models.verification import (
    ALLOWED_OUTCOMES,
    ALLOWED_SOURCES,
    AMOUNT_TOLERANCE,
    MANUAL_CONFIRMATION_CHANNEL,
    MANUAL_SOURCE,
    OUTCOME_FOR_EVENT,
    RECOVERED_OUTCOME,
    SUBSCRIBED_EVENTS,
    WEBHOOK_SOURCE,
    ManualConfirmationAck,
    ManualPaymentConfirmation,
    ManualVerification,
    ManualVerificationDocument,
    RazorpayLinkEvent,
    VerificationOutcome,
    VerificationRecord,
    VerificationRecordDocument,
    VerificationSource,
    WebhookAck,
    WebhookVerification,
    WebhookVerificationDocument,
    amounts_differ,
    confirmation_id_for,
    source_of,
    verification_document,
)
from app.webhooks.manual import (
    EventAlreadyRecovered,
    ExecutionNotFound,
    ExpectedAmountUnavailable,
    ManualConfirmationError,
    confirm_payment,
)
from app.webhooks.service import (
    STATUS_FOR_OUTCOME,
    expected_amount,
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
    NotManuallyConfirmable,
    StatusTransition,
    VerificationReferenceError,
    current_event_status,
    ensure_indexes,
    find_by_confirmation_id,
    find_by_razorpay_event_id,
    find_execution_by_link_id,
    list_for_event,
    list_verifications,
    transition_event_status,
)

__all__ = [
    "ALLOWED_OUTCOMES",
    "ALLOWED_SOURCES",
    "AMOUNT_TOLERANCE",
    "COLLECTION_NAME",
    "EVENT_ID_HEADER",
    "MANUAL_CONFIRMATION_CHANNEL",
    "MANUAL_SOURCE",
    "OUTCOME_FOR_EVENT",
    "RECOVERED_OUTCOME",
    "SIGNATURE_HEADER",
    "STATUS_FOR_OUTCOME",
    "SUBSCRIBED_EVENTS",
    "WEBHOOK_SOURCE",
    "BodyTooLarge",
    "DanglingExecutionReference",
    "DuplicateVerification",
    "EventAlreadyRecovered",
    "ExecutionNotFound",
    "ExpectedAmountUnavailable",
    "LinkMismatch",
    "MalformedBody",
    "MalformedSignature",
    "ManualConfirmationAck",
    "ManualConfirmationError",
    "ManualPaymentConfirmation",
    "ManualVerification",
    "ManualVerificationDocument",
    "MissingEventId",
    "MissingSignature",
    "NotManuallyConfirmable",
    "RazorpayLinkEvent",
    "SignatureMismatch",
    "StatusTransition",
    "VerificationOutcome",
    "VerificationRecord",
    "VerificationRecordDocument",
    "VerificationReferenceError",
    "VerificationSource",
    "VerifiedWebhook",
    "WebhookAck",
    "WebhookRejected",
    "WebhookSecretNotConfigured",
    "WebhookVerification",
    "WebhookVerificationDocument",
    "accept",
    "amounts_differ",
    "confirm_payment",
    "confirmation_id_for",
    "current_event_status",
    "ensure_indexes",
    "expected_amount",
    "expected_signature",
    "find_by_confirmation_id",
    "find_by_razorpay_event_id",
    "find_execution_by_link_id",
    "from_minor_units",
    "has_recovered",
    "link_entity",
    "list_for_event",
    "list_verifications",
    "reconcile",
    "source_of",
    "transition_event_status",
    "verification_document",
]
