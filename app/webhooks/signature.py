"""Webhook signature verification. The gate everything inbound passes through.

This is the only place in the project that decides whether an inbound request is
genuinely from Razorpay, and it was deliberately deferred from Stage 1 with a note
saying "add before production". This is that.

**The signature is checked against the raw bytes.** Razorpay's own documentation is
explicit — "ensure that the webhook body passed as an argument is the raw webhook
request body. Do not parse or cast the webhook request body" — and the reason is
concrete: `json.loads` followed by `json.dumps` is not the identity function. Key
order, whitespace, unicode escaping and float formatting all move, and any of those
would change the digest. So `accept()` takes `bytes` and never a dict, and the
route hands it `await request.body()` before anything looks inside.

**Verification precedes parsing, structurally.** `VerifiedWebhook` is the only type
the reconciler accepts, and the only code that constructs one is `accept()` in this
module, after `hmac.compare_digest` has returned true. The ordering is therefore a
property of the type graph rather than of the order somebody wrote two statements
in — the same device `AuthorizedVerdict` uses in Stage 5.

**Nothing about the secret or the expected digest is ever logged.** A log line
containing the expected signature for a body an attacker controls is a signing
oracle. Failures record that verification failed and the event id claimed; they do
not record what the correct answer would have been.

The contract, from https://razorpay.com/docs/webhooks/validate-test/ :

* header `X-Razorpay-Signature` carries a lowercase hex HMAC-SHA256 digest,
  keyed with the webhook secret, over the raw request body;
* header `x-razorpay-event-id` is unique per event and is what deduplication must
  key on. Note that the *body* carries no event id at all — the sample payloads
  have no top-level `id` — so this header is the only source for it, and a request
  without it cannot be safely processed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Razorpay's spelling. Read case-insensitively by Starlette either way.
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

#: A hex SHA-256 digest is 64 characters. Checked before comparing so a
#: wrong-length header fails with a clear reason rather than a bare mismatch.
DIGEST_HEX_LENGTH = 64

#: Refuse to hash a body larger than this. Razorpay's payment-link payloads are a
#: few kilobytes; a multi-megabyte body on this endpoint is not a webhook.
MAX_BODY_BYTES = 256 * 1024


class WebhookRejected(ValueError):
    """Base for inbound requests that must be answered 400 and not processed."""


class MissingSignature(WebhookRejected):
    """No `X-Razorpay-Signature` header, or an empty one.

    Its own type because "unsigned" and "wrongly signed" are different events
    operationally: the first is usually a misconfigured sender or a probe, the
    second is a secret mismatch or a tampered body.
    """


class MalformedSignature(WebhookRejected):
    """The header is present but is not a hex SHA-256 digest."""


class SignatureMismatch(WebhookRejected):
    """The digest did not match. The body is not from Razorpay, or was altered."""


class MissingEventId(WebhookRejected):
    """No `x-razorpay-event-id` header.

    Rejected rather than tolerated. It is the only idempotency key available — the
    body has none — so processing a request without it would mean accepting a
    payload that can be replayed an unbounded number of times, each replay
    inserting another record.
    """


class BodyTooLarge(WebhookRejected):
    """The request body exceeds `MAX_BODY_BYTES`."""


class MalformedBody(WebhookRejected):
    """The body is not a JSON object, or does not have a webhook's shape.

    Only ever raised *after* the signature has matched, so reaching it means
    Razorpay sent something this build does not understand rather than that
    somebody sent us junk. The route still answers 400 for it, because a 200 would
    claim we understood.
    """


class WebhookSecretNotConfigured(RuntimeError):
    """No `RAZORPAY_WEBHOOK_SECRET` is set.

    Not a `WebhookRejected`: this is an operator problem, not a bad request, and it
    must not be answered 400. Answering 400 would tell Razorpay to keep retrying a
    request that is fine, and — worse — would be indistinguishable in the logs from
    a genuine forgery. It fails closed: with no secret there is no way to verify
    anything, so nothing is processed.
    """


@dataclass(frozen=True)
class VerifiedWebhook:
    """A webhook body whose signature has been checked against the raw bytes.

    Constructed in exactly one place — `accept()` below, after
    `hmac.compare_digest` returns true. `app/webhooks/service.py` accepts this type
    and no other, so there is no route into reconciliation that skips verification.
    That claim is asserted mechanically by `scripts/s6_adversarial.py`, which greps
    for every construction site: a type is only a guarantee for as long as nobody
    adds a second constructor.

    Frozen, because the reconciler must reason about the bytes that were signed and
    not about a mutated copy of them.
    """

    #: Razorpay's per-event id, from the header. The deduplication key.
    razorpay_event_id: str
    #: The event name from the body, e.g. `payment_link.paid`. Not yet checked
    #: against the subscribed set — that is the reconciler's business.
    event: str
    #: The body's `payload` object, keyed by entity name.
    payload: dict[str, Any]
    #: Razorpay's `created_at`, a unix timestamp, when present.
    created_at: int | None
    #: Length of the verified body, for logging. The bytes themselves are not
    #: retained: everything downstream needs is already extracted.
    body_bytes: int


def webhook_secret() -> str:
    """Return the configured webhook secret.

    Raises:
        WebhookSecretNotConfigured: if it is absent. Fails closed.
    """
    secret = get_settings().razorpay_webhook_secret
    if not secret:
        raise WebhookSecretNotConfigured(
            "RAZORPAY_WEBHOOK_SECRET is not set in the environment; inbound "
            "webhooks cannot be verified and will not be processed. Set it in .env "
            "and restart."
        )
    return secret


def expected_signature(*, body: bytes, secret: str) -> str:
    """The digest Razorpay should have sent for these exact bytes.

    Exposed because the test harness needs to sign a body the same way Razorpay
    does. That is the point: the harness signs with the real secret and the real
    algorithm, so a test that passes is evidence about the verifier and not about a
    second implementation of it agreeing with the first.
    """
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


def _assert_signature(*, body: bytes, signature: str | None, secret: str) -> None:
    """Compare the header against the digest of the raw body, in constant time.

    Raises:
        MissingSignature, MalformedSignature, SignatureMismatch.
    """
    if signature is None or not signature.strip():
        raise MissingSignature(
            f"No {SIGNATURE_HEADER} header. Unsigned requests are refused before "
            "the body is read as anything but bytes."
        )

    candidate = signature.strip()
    if len(candidate) != DIGEST_HEX_LENGTH:
        raise MalformedSignature(
            f"{SIGNATURE_HEADER} is {len(candidate)} characters; a hex SHA-256 "
            f"digest is {DIGEST_HEX_LENGTH}"
        )
    try:
        bytes.fromhex(candidate)
    except ValueError as exc:
        raise MalformedSignature(
            f"{SIGNATURE_HEADER} is not hexadecimal"
        ) from exc

    expected = expected_signature(body=body, secret=secret)
    # Constant-time, on bytes. Comparing with `==` would leak how many leading
    # characters matched, which is enough to forge a digest one byte at a time.
    if not hmac.compare_digest(expected.encode("ascii"), candidate.encode("ascii")):
        raise SignatureMismatch(
            "Signature verification failed: the digest does not match the request "
            "body under the configured webhook secret. The body was not processed. "
            "(If the secret was rotated recently, Razorpay signs retries of older "
            "events with the previous secret.)"
        )


def accept(
    *,
    body: bytes,
    signature: str | None,
    razorpay_event_id: str | None,
    secret: str | None = None,
) -> VerifiedWebhook:
    """Verify an inbound request and return it as a `VerifiedWebhook`.

    Order is load-bearing and is the order of the code below: size, then signature,
    then — only then — JSON parsing. Nothing about the body's *content* is
    interpreted until the digest has matched.

    Args:
        body: The raw request bytes, exactly as received.
        signature: The `X-Razorpay-Signature` header value.
        razorpay_event_id: The `x-razorpay-event-id` header value.
        secret: Defaults to the configured secret. An explicit value is how a test
            drives a deliberate mismatch without mutating a module global — the
            lesson `app/execution/razorpay.py` records about patching globals.

    Raises:
        WebhookSecretNotConfigured: no secret; answer 503, not 400.
        WebhookRejected: any subclass; answer 400 and process nothing.
    """
    key = webhook_secret() if secret is None else secret

    if len(body) > MAX_BODY_BYTES:
        raise BodyTooLarge(
            f"Request body is {len(body)} bytes; the limit on this endpoint is "
            f"{MAX_BODY_BYTES}. Not hashed, not parsed."
        )

    _assert_signature(body=body, signature=signature, secret=key)

    # Past this line the bytes are known to be Razorpay's. Everything before it
    # treated them as opaque.
    if razorpay_event_id is None or not razorpay_event_id.strip():
        raise MissingEventId(
            f"Signature verified, but no {EVENT_ID_HEADER} header. That header is "
            "the only per-event identifier Razorpay sends — the payload carries "
            "none — so without it a re-delivery is indistinguishable from a new "
            "event and cannot be deduplicated. Refusing to process."
        )

    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedBody(
            f"Signature verified but the body is not valid JSON: {exc}"
        ) from exc

    if not isinstance(decoded, dict):
        raise MalformedBody(
            f"Signature verified but the body is a {type(decoded).__name__}, not a "
            "JSON object"
        )

    event = decoded.get("event")
    payload = decoded.get("payload")
    if not isinstance(event, str) or not event:
        raise MalformedBody(
            "Signature verified but the body has no 'event' string; every Razorpay "
            f"webhook body carries one. Keys present: {sorted(decoded)[:12]}"
        )
    if not isinstance(payload, dict):
        raise MalformedBody(
            f"Signature verified but 'payload' is {type(payload).__name__}, not an "
            f"object. Keys present: {sorted(decoded)[:12]}"
        )

    created_at = decoded.get("created_at")

    logger.info(
        "Verified webhook %s event=%s (%d bytes)",
        razorpay_event_id.strip(),
        event,
        len(body),
    )
    return VerifiedWebhook(
        razorpay_event_id=razorpay_event_id.strip(),
        event=event,
        payload=payload,
        created_at=created_at if isinstance(created_at, int) else None,
        body_bytes=len(body),
    )
