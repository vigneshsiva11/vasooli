"""The Razorpay test-mode client — the only module in the project that calls out.

Every other stage reasons; this one has an effect on a system we do not control.
Three consequences shape what is here.

**Notifications are switched off, always.** `notify.sms`, `notify.email` and
`reminder_enable` are hard-coded false, and there is no parameter to turn them on.
The system holds a `customer_ref` and no address of any kind — no email, no phone,
nowhere in any model — so there is no consented destination to send to. Letting
Razorpay deliver would mean either fabricating a recipient or letting the gateway
pick one, and the opt-out machinery in Stage 4 would be gating messages while this
module sent them by another route. The link is created and returned; delivering it
is a separate capability this build does not have.

**`reference_id` is derived from the verdict id.** Razorpay enforces uniqueness on
it, so a duplicate is refused at the provider — a second idempotency layer that
holds even if the unique index in `app/execution/store.py` were bypassed or two
requests raced past the pre-flight check. It is the only one of our three layers
that is enforced on the side where the side effect actually happens.

**No secret ever reaches a stored record or a log line.** `failure_reason` is
built from the status code and Razorpay's own error description; the request is
never echoed, and `_redact` is applied to anything derived from an exception.

The `razorpay` SDK is deliberately not used. It is synchronous, so it would need a
thread to avoid blocking the event loop, and this module makes exactly one kind of
POST — `httpx`, already a dependency, expresses that directly.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links"

#: Prefix on `reference_id`, so links minted by this system are identifiable in the
#: Razorpay dashboard alongside anything else the test account has.
REFERENCE_PREFIX = "vsl"

#: Razorpay caps `reference_id` at 40 characters. `vsl_` plus a 24-character
#: ObjectId is 28, so the assertion below is a guard against a future prefix
#: change rather than a live concern.
MAX_REFERENCE_LENGTH = 40

REQUEST_TIMEOUT_SECONDS = 20.0

#: Razorpay works in the minor unit — paise for INR.
MINOR_UNITS_PER_MAJOR = 100


class RazorpayNotConfigured(RuntimeError):
    """Raised when no API credentials are available.

    Its own type because it is an operator problem, not a gateway problem: nothing
    was attempted, so it should not be recorded as a failed execution with a
    misleading reason.
    """


class RazorpayCallFailed(RuntimeError):
    """Raised when the API call was made and did not succeed.

    Carries a message safe to store verbatim in `ExecutionRecord.failure_reason`.
    """


class RazorpayCredentials(NamedTuple):
    """A key pair.

    Passed as an argument rather than read from settings inside the request, so a
    failure test can supply a deliberately wrong key without mutating a module
    global. Stage 4 learned that lesson the hard way: patching a global reaches
    whichever readers happen to look it up late, and proves nothing about the ones
    that had already bound the old value.
    """

    key_id: str
    key_secret: str


class PaymentLink(NamedTuple):
    """The part of Razorpay's response this system keeps."""

    id: str
    short_url: str
    reference_id: str


def credentials_from_settings() -> RazorpayCredentials:
    """Read the configured test-mode credentials.

    Raises:
        RazorpayNotConfigured: if either value is missing.
    """
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        missing = [
            name
            for name, value in (
                ("RAZORPAY_KEY_ID", settings.razorpay_key_id),
                ("RAZORPAY_KEY_SECRET", settings.razorpay_key_secret),
            )
            if not value
        ]
        raise RazorpayNotConfigured(
            f"{', '.join(missing)} not set in the environment; execution cannot "
            "reach Razorpay. Set them in .env and restart."
        )
    return RazorpayCredentials(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
    )


def reference_id_for(policy_verdict_id: str) -> str:
    """The provider-side idempotency token for one verdict's link.

    Derived rather than random, so it is the same value on a retry and Razorpay can
    recognise the duplicate. Deterministic from the verdict id alone, which means
    it can also be reconstructed by an auditor holding only the stored record.
    """
    reference = f"{REFERENCE_PREFIX}_{policy_verdict_id}"
    if len(reference) > MAX_REFERENCE_LENGTH:  # pragma: no cover - see MAX_REFERENCE
        raise ValueError(
            f"reference_id {reference!r} is {len(reference)} characters; Razorpay "
            f"allows {MAX_REFERENCE_LENGTH}"
        )
    return reference


def to_minor_units(amount: float) -> int:
    """Convert a major-unit amount to the integer minor unit Razorpay expects.

    Rounded rather than truncated, and returned as an int, so 1234.56 is 123456
    paise and not 123455 or a float that serialises with an exponent.
    """
    return int(round(amount * MINOR_UNITS_PER_MAJOR))


def _redact(text: str, credentials: RazorpayCredentials) -> str:
    """Remove anything credential-shaped from text destined for storage or logs."""
    cleaned = text
    for secret in (credentials.key_secret, credentials.key_id):
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


def _describe_error(response: httpx.Response, credentials: RazorpayCredentials) -> str:
    """Turn a non-2xx response into a reason string worth storing.

    Razorpay nests its message under `error.description`. Falling back to the raw
    body matters: a rate-limit page or a proxy error will not have that shape, and
    "HTTP 502" with no detail is exactly the sort of failure_reason that makes an
    incident unreadable a week later.
    """
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        detail = response.text[:200]
    else:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            parts = [
                str(error.get(key))
                for key in ("code", "description")
                if error.get(key)
            ]
            detail = ": ".join(parts)
        if not detail:
            detail = str(payload)[:200]

    return _redact(
        f"Razorpay returned HTTP {response.status_code}"
        + (f" — {detail}" if detail else ""),
        credentials,
    )


async def create_payment_link(
    *,
    amount: float,
    currency: str,
    description: str,
    reference_id: str,
    notes: dict[str, str],
    credentials: RazorpayCredentials | None = None,
) -> PaymentLink:
    """Create a Razorpay test-mode payment link.

    Args:
        amount: Major units (rupees). Converted to paise here.
        currency: ISO 4217 code, taken from the event.
        description: Shown on the payment page. Must not contain customer PII;
            callers build it from the root cause and the event id.
        reference_id: Provider-side idempotency token. See `reference_id_for`.
        notes: Key/value metadata stored on the link, used to tie it back to our
            records from the Razorpay side.
        credentials: Defaults to the configured pair.

    Raises:
        RazorpayNotConfigured: if credentials are absent.
        RazorpayCallFailed: on any network error or non-2xx response. The message
            is safe to store.
    """
    keys = credentials_from_settings() if credentials is None else credentials

    payload: dict[str, Any] = {
        "amount": to_minor_units(amount),
        "currency": currency,
        "description": description,
        "reference_id": reference_id,
        "accept_partial": False,
        # Hard false, with no way to override. See the module docstring: there is
        # no consented address in this system to deliver to.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": notes,
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                PAYMENT_LINKS_URL,
                json=payload,
                auth=(keys.key_id, keys.key_secret),
            )
    except httpx.HTTPError as exc:
        # Network-level: DNS, TLS, connect, read timeout. Distinguished from a
        # rejection because the request may or may not have been received, which is
        # precisely why a failed execution is terminal for its verdict rather than
        # retried against the same permission.
        raise RazorpayCallFailed(
            _redact(f"{type(exc).__name__}: {exc}", keys)[:400]
        ) from exc

    if response.status_code >= 400:
        raise RazorpayCallFailed(_describe_error(response, keys)[:400])

    body = response.json()
    link_id = body.get("id")
    short_url = body.get("short_url")
    if not link_id or not short_url:
        # A 2xx without the two fields the record requires. Treated as a failure
        # rather than stored as a completed execution with empty artifact fields,
        # which the model would reject anyway.
        raise RazorpayCallFailed(
            f"Razorpay returned HTTP {response.status_code} without an id and "
            f"short_url; keys present: {sorted(body)[:12]}"
        )

    logger.info(
        "Created Razorpay payment link %s for reference %s", link_id, reference_id
    )
    return PaymentLink(
        id=str(link_id),
        short_url=str(short_url),
        reference_id=str(body.get("reference_id") or reference_id),
    )
