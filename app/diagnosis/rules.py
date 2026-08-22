"""Rule-based root-cause classification.

Deterministic, offline, and cheap: this handles the clear-cut cases so the LLM is
only consulted when the signal is genuinely ambiguous. Every rule maps a signal we
already hold on the event to one of the surface's allowed root causes.

Contains no LLM call and no network I/O by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import RevenueEvent

#: A rules classification at or above this confidence is trusted outright and the
#: LLM is never called. Below it, the case is escalated for reasoning.
RULE_CONFIDENCE_FLOOR = 0.80


@dataclass(frozen=True)
class RuleMatch:
    """A rules-derived classification."""

    root_cause: str
    confidence: float
    evidence: tuple[str, ...]

    @property
    def is_confident(self) -> bool:
        """Whether this match is strong enough to skip the LLM."""
        return self.confidence >= RULE_CONFIDENCE_FLOOR


#: Canonical failure codes, matched exactly after normalisation. These are the
#: values gateways actually emit, so an exact hit is near-certain.
_EXACT_CODES: dict[str, dict[str, tuple[str, float]]] = {
    "payment": {
        "insufficient_funds": ("insufficient_funds", 0.97),
        "card_expired": ("card_expired", 0.97),
        "expired_card": ("card_expired", 0.97),
        "issuer_declined": ("issuer_declined", 0.95),
        "card_declined_by_issuer": ("issuer_declined", 0.95),
        "do_not_honour": ("issuer_declined", 0.93),
        "do_not_honor": ("issuer_declined", 0.93),
        "gateway_error": ("temporary_processing_error", 0.92),
        "gateway_timeout": ("temporary_processing_error", 0.92),
        "network_error": ("temporary_processing_error", 0.92),
        "suspected_fraud": ("suspected_fraud", 0.96),
        "fraud_suspected": ("suspected_fraud", 0.96),
        "stolen_card": ("suspected_fraud", 0.96),
        "lost_card": ("suspected_fraud", 0.96),
    },
    "subscription": {
        "mandate_expired": ("mandate_expired", 0.96),
        "mandate_revoked": ("mandate_revoked", 0.96),
        "mandate_cancelled": ("mandate_revoked", 0.95),
        "mandate_canceled": ("mandate_revoked", 0.95),
        "card_expired": ("card_expired", 0.96),
        "insufficient_funds": ("insufficient_funds", 0.96),
        "issuer_declined": ("issuer_declined", 0.94),
        "subscription_cancelled": ("voluntary_churn", 0.94),
        "subscription_canceled": ("voluntary_churn", 0.94),
        "cancelled_by_customer": ("voluntary_churn", 0.94),
        "retries_exhausted": ("dunning_exhausted", 0.94),
        "max_retries_reached": ("dunning_exhausted", 0.94),
    },
    "receivable": {
        "disputed": ("payment_dispute", 0.94),
        "invoice_disputed": ("payment_dispute", 0.94),
        "no_response": ("non_responsive", 0.90),
        "unreachable": ("non_responsive", 0.90),
    },
    "checkout": {
        "payment_method_unavailable": ("payment_method_unavailable", 0.94),
        "otp_failed": ("checkout_friction", 0.88),
        "session_timeout": ("technical_error", 0.88),
        "gateway_error": ("technical_error", 0.90),
    },
}

#: Keyword patterns for free-text reasons. Ordered — the first match wins, so more
#: specific patterns must precede broader ones. Confidence is lower than an exact
#: code hit because free text is inherently less certain.
_KEYWORD_RULES: dict[str, tuple[tuple[str, str, float], ...]] = {
    "payment": (
        (r"\b(?:insufficient|inadequate|low)\s+(?:funds?|balance)\b", "insufficient_funds", 0.92),
        (r"\bnot\s+enough\s+(?:money|funds?|balance)\b", "insufficient_funds", 0.90),
        (r"\b(?:expired|expiry|expiration)\b.*\bcard\b|\bcard\b.*\b(?:expired|expiry)\b", "card_expired", 0.90),
        (r"\b(?:fraud|fraudulent|stolen|suspicious|blocked\s+by\s+risk)\b", "suspected_fraud", 0.90),
        (r"\b(?:timeout|timed\s+out|unavailable|try\s+again|temporar)", "temporary_processing_error", 0.88),
        (r"\b(?:gateway|network|connection|server)\s+(?:error|failure|issue)\b", "temporary_processing_error", 0.88),
        (r"\b(?:declined|rejected|refused)\b.*\b(?:issuer|bank)\b", "issuer_declined", 0.90),
        (r"\b(?:issuer|bank)\b.*\b(?:declined|rejected|refused)\b", "issuer_declined", 0.90),
        # Authentication failures often clear on a fresh attempt, so they are
        # treated as transient rather than a hard issuer decline.
        (r"\b(?:otp|3ds|two\s*factor|authentication)\b.*\bfail", "temporary_processing_error", 0.82),
        (r"\bdo\s+not\s+hono[u]?r\b", "issuer_declined", 0.90),
    ),
    "subscription": (
        (r"\bmandate\b.*\b(?:expired|lapsed)\b", "mandate_expired", 0.92),
        (r"\bmandate\b.*\b(?:revoked|cancelled|canceled|withdrawn)\b", "mandate_revoked", 0.92),
        (r"\b(?:insufficient|low)\s+(?:funds?|balance)\b", "insufficient_funds", 0.92),
        (r"\bcard\b.*\bexpired\b|\bexpired\b.*\bcard\b", "card_expired", 0.90),
        (r"\b(?:cancelled|canceled|unsubscrib|churn|not\s+renewing|don'?t\s+want)\b", "voluntary_churn", 0.88),
        (r"\b(?:all\s+)?retr(?:y|ies)\b.*\b(?:exhausted|failed|attempted)\b", "dunning_exhausted", 0.90),
    ),
    "receivable": (
        (r"\b(?:disput|contest|disagree|wrong\s+amount|incorrect\s+invoice|billing\s+error)", "payment_dispute", 0.90),
        (r"\b(?:no\s+response|not\s+respond|unreachable|no\s+reply|ignoring|bounced)\b", "non_responsive", 0.88),
        (r"\b(?:cash\s*flow|delay|late|next\s+week|end\s+of\s+month|will\s+pay|processing\s+internally|approval\s+pending)\b", "genuine_delay", 0.84),
    ),
    "checkout": (
        (r"\b(?:too\s+expensive|price|pricing|cost|shipping\s+charge|delivery\s+fee|discount|coupon)\b", "price_sensitivity", 0.84),
        (r"\b(?:upi|netbanking|net\s+banking|wallet|emi|cod|cash\s+on\s+delivery)\b.*\b(?:unavailable|not\s+available|missing|not\s+offered|no\s+option)\b", "payment_method_unavailable", 0.90),
        (r"\b(?:error|crash|broke|failed\s+to\s+load|blank\s+screen|timeout|timed\s+out)\b", "technical_error", 0.88),
        (r"\b(?:otp|form|too\s+many\s+steps|confusing|slow|redirect)\b", "checkout_friction", 0.84),
        (r"\b(?:just\s+browsing|comparing|window\s+shopping|not\s+ready)\b", "low_purchase_intent", 0.86),
    ),
}


def _normalise(reason: str) -> str:
    """Lower-case and collapse punctuation so codes and free text compare cleanly."""
    return re.sub(r"[\s\-]+", "_", reason.strip().lower())


def classify(event: RevenueEvent, prior_event_count: int = 0) -> RuleMatch | None:
    """Classify an event from signals already on it.

    Args:
        event: The event to classify.
        prior_event_count: How many earlier at-risk events exist for this customer,
            used only as supporting evidence, never to override the failure reason.

    Returns:
        A `RuleMatch`, or None when the rules have nothing to say — in which case
        the caller escalates to the LLM.
    """
    history_evidence = _history_evidence(event, prior_event_count)

    reason = (event.raw_failure_reason or "").strip()
    if not reason:
        # No failure text at all. Common for abandoned checkouts, where absence of
        # a reason is itself uninformative, so there is nothing to be confident about.
        return None

    normalised = _normalise(reason)

    exact = _EXACT_CODES.get(event.surface, {}).get(normalised)
    if exact is not None:
        root_cause, confidence = exact
        return RuleMatch(
            root_cause=root_cause,
            confidence=confidence,
            evidence=(
                f"gateway reported canonical code '{reason}'",
                *history_evidence,
            ),
        )

    lowered = reason.lower()
    for pattern, root_cause, confidence in _KEYWORD_RULES.get(event.surface, ()):
        if re.search(pattern, lowered):
            return RuleMatch(
                root_cause=root_cause,
                confidence=confidence,
                evidence=(
                    f"failure text matched a known {root_cause} pattern: '{_truncate(reason)}'",
                    *history_evidence,
                ),
            )

    return None


def _history_evidence(event: RevenueEvent, prior_event_count: int) -> tuple[str, ...]:
    """Build supporting evidence from history and amount, if either is notable."""
    evidence: list[str] = []

    if prior_event_count == 1:
        evidence.append("one earlier at-risk event for this customer")
    elif prior_event_count > 1:
        evidence.append(
            f"{prior_event_count} earlier at-risk events for this customer"
        )

    if event.amount >= 50_000:
        evidence.append(f"high-value event ({event.currency} {event.amount:,.2f})")

    return tuple(evidence)


def _truncate(text: str, limit: int = 120) -> str:
    """Shorten text for inclusion in evidence."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
