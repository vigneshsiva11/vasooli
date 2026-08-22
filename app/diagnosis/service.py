"""Diagnosis orchestration: rules first, Gemini only for the ambiguous remainder.

This module is where an LLM proposal becomes a `Diagnosis` — and the only place
that conversion happens. The conversion is deliberately narrow:

* `event_id`, `surface` and `diagnosed_at` are taken from the event and the clock,
  never from the model.
* `root_cause` is checked against the surface's closed set; anything outside it is
  discarded in favour of "unknown".
* `recoverable` is a deterministic table lookup, not a model judgement.

Result: the worst a misbehaving model can do is produce a low-confidence "unknown".
"""

from __future__ import annotations

import logging

from app.diagnosis import gemini, rules
from app.models import (
    ALLOWED_ROOT_CAUSES,
    MAX_EVIDENCE_ITEMS,
    UNKNOWN_ROOT_CAUSE,
    Diagnosis,
    DiagnosisMethod,
    RevenueEvent,
    is_recoverable,
)

logger = logging.getLogger(__name__)

#: Confidence assigned when we fall back to "unknown". Low, but not zero: we know
#: the event is at risk, we just cannot say why.
FALLBACK_CONFIDENCE = 0.20

#: Ceiling applied to a model's stated confidence. Gemini is a classifier here, not
#: an oracle, and a rules exact-code hit should always outrank it.
LLM_CONFIDENCE_CEILING = 0.90


def normalise_root_cause(candidate: str) -> str:
    """Normalise a proposed root cause for comparison against the allowed set.

    Handles cosmetic drift only — case, surrounding whitespace, and spaces or
    hyphens used in place of underscores, so "Card Expired" resolves to
    "card_expired". Deliberately does no fuzzy or nearest-match resolution: a
    genuinely invented category must fail, not be quietly mapped onto a real one.
    """
    return candidate.strip().lower().replace(" ", "_").replace("-", "_")


def _finalise(
    *,
    event: RevenueEvent,
    root_cause: str,
    confidence: float,
    evidence: list[str],
    method: DiagnosisMethod,
) -> tuple[Diagnosis, DiagnosisMethod]:
    """Assemble a validated `Diagnosis` from a classification."""
    diagnosis = Diagnosis(
        event_id=event.event_id,
        surface=event.surface,
        root_cause=root_cause,
        confidence=round(confidence, 4),
        # Truncate rather than let a long evidence list fail validation: evidence is
        # supporting detail, and losing the sixth item is better than losing the
        # whole classification.
        evidence=evidence[:MAX_EVIDENCE_ITEMS],
        recoverable=is_recoverable(root_cause),
        # diagnosed_at intentionally left to its default_factory: system clock only.
    )
    return diagnosis, method


def _fallback(
    event: RevenueEvent, reason: str
) -> tuple[Diagnosis, DiagnosisMethod]:
    """Build the safe default diagnosis: unknown cause, low confidence."""
    return _finalise(
        event=event,
        root_cause=UNKNOWN_ROOT_CAUSE,
        confidence=FALLBACK_CONFIDENCE,
        evidence=[reason],
        method="fallback",
    )


async def diagnose(
    event: RevenueEvent, prior_event_count: int = 0
) -> tuple[Diagnosis, DiagnosisMethod]:
    """Diagnose one event.

    Args:
        event: The event to explain.
        prior_event_count: Earlier at-risk events for the same customer, used as
            supporting evidence.

    Returns:
        The diagnosis, and which path produced it.
    """
    match = rules.classify(event, prior_event_count=prior_event_count)

    if match is not None and match.is_confident:
        logger.info(
            "Rules classified %s as %s (confidence %.2f); no LLM call",
            event.event_id,
            match.root_cause,
            match.confidence,
        )
        return _finalise(
            event=event,
            root_cause=match.root_cause,
            confidence=match.confidence,
            evidence=list(match.evidence),
            method="rules",
        )

    if not gemini.is_configured():
        logger.warning(
            "Event %s needs LLM reasoning but GEMINI_API_KEY is not set", event.event_id
        )
        if match is not None:
            # A weak rules match still beats knowing nothing; keep it, but keep its
            # low confidence and say why it was not corroborated.
            return _finalise(
                event=event,
                root_cause=match.root_cause,
                confidence=match.confidence,
                evidence=[*match.evidence, "low-confidence rules match, LLM unavailable"],
                method="fallback",
            )
        return _fallback(event, "no rule matched and LLM is not configured")

    try:
        proposal, raw_text = await gemini.propose_diagnosis(
            surface=event.surface,
            amount=event.amount,
            currency=event.currency,
            raw_failure_reason=event.raw_failure_reason,
            prior_event_count=prior_event_count,
        )
    except gemini.GeminiUnavailable as exc:
        logger.warning("Gemini unavailable for %s: %s", event.event_id, exc)
        return _fallback(event, f"LLM unavailable: {type(exc).__name__}")

    allowed = ALLOWED_ROOT_CAUSES[event.surface]
    candidate = normalise_root_cause(proposal.root_cause)

    if candidate != proposal.root_cause:
        logger.info(
            "Normalised Gemini root_cause %r to %r for %s",
            proposal.root_cause,
            candidate,
            event.event_id,
        )

    if candidate not in allowed:
        # The response schema should have prevented this. It reaching us means the
        # API-level constraint was bypassed or changed, which is exactly why the
        # application-level check exists.
        logger.error(
            "Gemini proposed disallowed root_cause %r for surface %r on event %s; "
            "falling back to unknown. Raw response: %s",
            proposal.root_cause,
            event.surface,
            event.event_id,
            raw_text[:500],
        )
        return _finalise(
            event=event,
            root_cause=UNKNOWN_ROOT_CAUSE,
            confidence=FALLBACK_CONFIDENCE,
            evidence=[
                "LLM proposed a root cause outside the allowed set; rejected",
                *proposal.evidence[: 2],
            ],
            method="fallback",
        )

    confidence = min(proposal.confidence, LLM_CONFIDENCE_CEILING)
    evidence = list(proposal.evidence)
    if match is not None:
        evidence.append(
            f"weak rules signal also suggested {match.root_cause} "
            f"({match.confidence:.2f})"
        )

    logger.info(
        "Gemini classified %s as %s (confidence %.2f)",
        event.event_id,
        candidate,
        confidence,
    )
    return _finalise(
        event=event,
        root_cause=candidate,
        confidence=confidence,
        evidence=evidence,
        method="llm",
    )
