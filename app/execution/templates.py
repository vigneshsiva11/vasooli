"""Deterministic message templates for contact-type interventions.

No LLM. The brief asks for templated content and it is the right call for more than
determinism: a reminder is a legal and reputational artifact, and the one thing worse
than a dull reminder is a generated one that invents a due date. Gemini is used for
diagnosis, where the output is a label from a fixed set that a validator checks.
Here the output would be prose sent to a customer, unbounded and uncheckable. The
LLM's contribution is already present — it chose the `root_cause` that selects the
phrasing below.

Two structural choices worth knowing about.

**The body is rendered but not stored.** `ExecutionRecord.contact_message_summary`
keeps the template id, its version, the channel and the rendered subject — enough to
say which message was sent and to reconstruct it — while the body stays out of the
record. Storing it would put a customer-facing paragraph in every audit dump and
grow the collection for a string that is a pure function of the template and the
event. The gap this leaves is real and flagged rather than solved: the *version* is
stored but old template text is not archived, so editing a template below without
bumping its version would make an old summary name a message that no longer exists.
That is the rulebook problem again, one stage down and much smaller, and it is not
in this stage's scope.

**Completeness is asserted at import.** Every root cause in the diagnosis vocabulary
must have a phrase. A missing one would otherwise surface as a crash inside an
execution — or worse, as a generic message that misrepresents the diagnosis — the
first time a real event carried it. Adding a root cause upstream now breaks the
import instead.
"""

from __future__ import annotations

from typing import NamedTuple

from app.models.diagnosis import ALLOWED_ROOT_CAUSES

#: Bumped when a template's *text* changes. Stored on every contact record; see the
#: module docstring for what it does and does not guarantee.
TEMPLATE_VERSION = 1

#: Hard cap on the stored summary, matching `ExecutionRecord.contact_message_summary`.
MAX_SUMMARY_LENGTH = 300


class ContactChannel:
    """The channels a contact record can name.

    Both are placeholders in the sense that nothing is delivered — the system holds
    a `customer_ref` and no address of any kind. They are distinguished anyway
    because they describe genuinely different acts.
    """

    #: Where a customer-facing message would go.
    EMAIL = "email"
    #: Where a manual escalation goes: a queue for a human on our side, not the
    #: customer. Stage 4 counts `manual_escalation` as contact-type, which is the
    #: cautious reading (it exists to produce human outreach), so an opted-out
    #: customer blocks it. Naming the channel honestly here keeps the record from
    #: claiming we emailed somebody when we filed a task. Flagged, not reconciled.
    INTERNAL_TASK = "internal_task"


class ContactMessage(NamedTuple):
    """A rendered message. Only the first four fields reach the database."""

    template_id: str
    template_version: int
    channel: str
    subject: str
    body: str


class Template(NamedTuple):
    """One message shell. `{...}` fields are filled by `render`."""

    template_id: str
    channel: str
    subject: str
    body: str


class NoTemplate(LookupError):
    """Raised when an intervention or root cause has no template.

    Loud rather than defaulted: a generic message attributed to a specific
    diagnosis is a false record of what the customer was told.
    """


# ---------------------------------------------------------------------------
# Why we are writing. One clause per root cause, in the customer's terms.
# ---------------------------------------------------------------------------
#
# Phrased to complete the sentence "our records show that ...". Deliberately plain:
# no blame, no urgency language, no invented dates or amounts beyond the two figures
# the event actually carries.

CAUSE_PHRASES: dict[str, str] = {
    # Payment / subscription — instrument problems.
    "card_expired": "the card on file has passed its expiry date",
    "insufficient_funds": "the payment was declined for insufficient funds",
    "issuer_declined": "the card issuer declined the payment",
    "temporary_processing_error": "a temporary processing error interrupted the payment",
    "mandate_expired": "the auto-debit mandate on this subscription has expired",
    "mandate_revoked": "the auto-debit mandate on this subscription was cancelled",
    "dunning_exhausted": "several scheduled retries have now been attempted without success",
    # Checkout — intent and mechanics.
    "checkout_friction": "the checkout was not completed",
    "low_purchase_intent": "an order was started but not completed",
    "payment_method_unavailable": "the payment method selected at checkout was unavailable",
    "price_sensitivity": "an order was started but not completed",
    "technical_error": "a technical error interrupted the checkout",
    # Receivables.
    "genuine_delay": "this invoice is past its due date",
    "non_responsive": "this invoice remains unpaid and earlier messages have gone unanswered",
    "payment_dispute": "this invoice has been disputed",
    # Present for completeness, and unreachable in practice: policy refuses to
    # authorize anything for these, so no execution can name one. Included so the
    # completeness assertion below is a plain statement about the vocabulary rather
    # than one carrying a list of exceptions.
    "suspected_fraud": "this payment has been flagged for review",
    "voluntary_churn": "this subscription was cancelled",
    "unknown": "this payment did not complete",
}


# ---------------------------------------------------------------------------
# The templates.
# ---------------------------------------------------------------------------
#
# One per contact-type intervention. The escalating sequence renders only its FIRST
# message: this stage executes one action, and a sequence is executed by policy
# authorizing its next step later, not by sending three messages now. The record
# says which step it was.

TEMPLATES: dict[str, Template] = {
    "reminder": Template(
        template_id="reminder.v1",
        channel=ContactChannel.EMAIL,
        subject="Payment reminder — {currency} {amount} outstanding",
        body=(
            "Hello,\n\n"
            "This is a reminder about an outstanding amount of {currency} {amount}. "
            "Our records show that {cause}.\n\n"
            "If you have already arranged payment, no action is needed and you can "
            "disregard this message.\n\n"
            "Reference: {event_id}\n"
        ),
    ),
    "escalating_reminder_sequence": Template(
        template_id="escalating_reminder.step1.v1",
        channel=ContactChannel.EMAIL,
        subject="Second notice — {currency} {amount} outstanding",
        body=(
            "Hello,\n\n"
            "We have not yet been able to collect {currency} {amount}. Our records "
            "show that {cause}.\n\n"
            "We would like to resolve this with you directly. If there is a problem "
            "with the amount or the timing, replying to this message is the fastest "
            "way to sort it out.\n\n"
            "Reference: {event_id}\n"
        ),
    ),
    "manual_escalation": Template(
        template_id="manual_escalation.v1",
        channel=ContactChannel.INTERNAL_TASK,
        # Addressed to a colleague, not a customer. The subject is written to be
        # readable in a task queue.
        subject="Manual review — {event_id}, {currency} {amount} at risk",
        body=(
            "Automated recovery has been exhausted for this event and it needs a "
            "person.\n\n"
            "Event: {event_id}\n"
            "Customer: {customer_ref}\n"
            "Amount at risk: {currency} {amount}\n"
            "Diagnosed cause: {root_cause} — {cause}\n\n"
            "No further automated contact will be attempted for this event while "
            "this task is open.\n"
        ),
    ),
}


def _assert_every_root_cause_has_a_phrase() -> None:
    """Fail at import if the diagnosis vocabulary has outgrown the phrase table."""
    known = {cause for causes in ALLOWED_ROOT_CAUSES.values() for cause in causes}
    missing = sorted(known - set(CAUSE_PHRASES))
    if missing:  # pragma: no cover - a build error, not a runtime path
        raise RuntimeError(
            f"CAUSE_PHRASES is missing {missing}; a contact template cannot describe "
            "a diagnosis it has no words for. Add a phrase in app/execution/templates.py."
        )
    stale = sorted(set(CAUSE_PHRASES) - known)
    if stale:  # pragma: no cover - same
        raise RuntimeError(
            f"CAUSE_PHRASES names {stale}, which the diagnosis vocabulary no longer "
            "contains; remove them so the table cannot describe an impossible cause."
        )


_assert_every_root_cause_has_a_phrase()


def render(
    *,
    intervention: str,
    root_cause: str,
    event_id: str,
    customer_ref: str,
    amount_at_risk: float,
    currency: str,
) -> ContactMessage:
    """Render the message for one contact-type intervention.

    Raises:
        NoTemplate: if the intervention has no template, or the root cause no
            phrase. Both mean the caller is executing something this module was
            never told how to describe.
    """
    template = TEMPLATES.get(intervention)
    if template is None:
        raise NoTemplate(
            f"no contact template for intervention {intervention!r}; "
            f"templated interventions are {sorted(TEMPLATES)}"
        )
    phrase = CAUSE_PHRASES.get(root_cause)
    if phrase is None:
        raise NoTemplate(
            f"no phrase for root cause {root_cause!r}; a contact message must "
            "describe the diagnosis it was sent for"
        )

    values = {
        "event_id": event_id,
        "customer_ref": customer_ref,
        "amount": f"{amount_at_risk:,.2f}",
        "currency": currency,
        "cause": phrase,
        "root_cause": root_cause,
    }
    return ContactMessage(
        template_id=template.template_id,
        template_version=TEMPLATE_VERSION,
        channel=template.channel,
        subject=template.subject.format(**values),
        body=template.body.format(**values),
    )


def summarise(message: ContactMessage) -> str:
    """The one line that goes into `ExecutionRecord.contact_message_summary`.

    Template id, version, channel, rendered subject. Everything needed to say which
    message was sent without storing the body. Truncated with an explicit ellipsis
    rather than by the field's `max_length`, so a long subject degrades visibly
    instead of raising a validation error at the end of a completed side effect.
    """
    line = (
        f"{message.template_id} v{message.template_version} "
        f"via {message.channel} — {message.subject}"
    )
    if len(line) > MAX_SUMMARY_LENGTH:
        return line[: MAX_SUMMARY_LENGTH - 1] + "…"
    return line
