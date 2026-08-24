"""The policy engine (Stage 4) — pure, deterministic authorization.

This is the second half of the recommend/authorize split. Stage 3 answers "what
is the best action?"; this module answers "is that action permitted?", and the
two answers are produced by different code with different inputs.

Three properties are held deliberately:

* **No I/O.** `evaluate()` is a pure function of a `DecisionRecord` and a
  `PolicyContext`. There is no database handle, no HTTP client, and no LLM call
  in this module — the facts it needs about the world (consent, prior contacts)
  are gathered by `app.policy.store` and handed in. Policy must never reason in
  natural language, so nothing here can consult a model even by accident.
* **No short-circuiting.** Every check in `POLICY_CHECKS` is evaluated and
  recorded, whatever any earlier check concluded. A refused recommendation shows
  its full evaluation trail, not the first thing that went wrong.
* **No execution.** The return value is a `PolicyVerdict`, which has no field
  capable of expressing that anything was done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models import DecisionRecord, NO_ACTION_INTERVENTIONS
from app.models.policy import (
    POLICY_CHECKS,
    REASON_VERDICT,
    PolicyVerdict,
    format_check,
    primary_reason,
)
from app.policy.rules import (
    AUTO_AUTHORIZE_BELOW,
    COOLDOWN,
    COOLDOWN_HOURS,
    MAX_CONTACTS_PER_EVENT,
    MINIMUM_ERV,
    NEVER_AUTO_AT_OR_ABOVE,
    erv_floor_applies,
    is_contact_intervention,
    tier_for,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PolicyContext:
    """The facts about the world that policy needs but cannot derive.

    Assembled by `app.policy.store.gather_context`. Passing these in rather than
    querying for them is what keeps `evaluate()` pure and testable: every verdict
    below can be reproduced from a `DecisionRecord` and one of these, with no
    database present.
    """

    #: The event's customer, carried so the trail can name who was checked.
    customer_ref: str
    #: Whether that customer is on the do-not-contact list.
    customer_opted_out: bool
    #: Authorized contact-type verdicts already recorded for this event, across
    #: all decision and diagnosis versions.
    prior_authorized_contacts: int
    #: When the most recent of those was authorized, or None if there were none.
    last_authorized_contact_at: datetime | None
    #: Evaluation time, injected so a verdict is reproducible rather than
    #: depending on when the audit happens to run.
    now: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.customer_ref:
            raise ValueError("PolicyContext requires a customer_ref")
        if self.prior_authorized_contacts < 0:
            raise ValueError(
                f"prior_authorized_contacts cannot be negative "
                f"({self.prior_authorized_contacts})"
            )
        if self.now.tzinfo is None:
            raise ValueError("PolicyContext.now must be timezone-aware")
        if self.last_authorized_contact_at is not None:
            if self.last_authorized_contact_at.tzinfo is None:
                raise ValueError(
                    "last_authorized_contact_at must be timezone-aware"
                )
            if self.prior_authorized_contacts == 0:
                raise ValueError(
                    "last_authorized_contact_at is set but "
                    "prior_authorized_contacts is 0; the two describe the same "
                    "history and cannot disagree"
                )
        elif self.prior_authorized_contacts > 0:
            raise ValueError(
                f"prior_authorized_contacts is "
                f"{self.prior_authorized_contacts} but no timestamp was given "
                "for the most recent one"
            )


@dataclass(frozen=True)
class CheckOutcome:
    """One evaluated rule: whether it passed, why, and what it implies if not."""

    name: str
    passed: bool
    detail: str
    #: The reason code this check contributes when it fails. None when passed.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.passed and self.reason is not None:
            raise ValueError(f"check {self.name!r} passed but carries a reason code")
        if not self.passed and self.reason is None:
            raise ValueError(f"check {self.name!r} failed without a reason code")

    def entry(self) -> str:
        """Render this outcome as a `checks_performed` trail entry."""
        return format_check(self.name, self.passed, self.detail)


# ---------------------------------------------------------------------------
# The individual checks. Each is a pure function returning a CheckOutcome, and
# none of them looks at the result of any other — that independence is what makes
# "evaluate everything" the natural implementation rather than extra work.
# ---------------------------------------------------------------------------


def _check_actionable(decision: DecisionRecord) -> CheckOutcome:
    """Is there an action here at all?

    Stage 3 can conclude that nothing should be done. Authorizing that would
    hand Stage 5 a permission slip with nothing on it, so a no-action
    recommendation is refused: there is no action to permit.
    """
    intervention = decision.recommended_intervention
    if intervention in NO_ACTION_INTERVENTIONS:
        return CheckOutcome(
            name="decision_is_actionable",
            passed=False,
            detail=(
                f"decision recommends {intervention}, which is not an action; "
                "there is nothing to authorize"
            ),
            reason="no_action_recommended",
        )
    return CheckOutcome(
        name="decision_is_actionable",
        passed=True,
        detail=f"{intervention} is a real intervention",
    )


def _check_opt_out(decision: DecisionRecord, context: PolicyContext) -> CheckOutcome:
    """Has this customer asked not to be contacted?

    Applies only to contact-type interventions. A retry touches the payment rail,
    not the person, so an opt-out does not stand in its way.
    """
    intervention = decision.recommended_intervention
    if not is_contact_intervention(intervention):
        return CheckOutcome(
            name="customer_opt_out",
            passed=True,
            detail=(
                f"not applicable: {intervention} does not contact the customer"
            ),
        )
    if context.customer_opted_out:
        return CheckOutcome(
            name="customer_opt_out",
            passed=False,
            detail=(
                f"customer {context.customer_ref} is on the do-not-contact list "
                f"and {intervention} would contact them"
            ),
            reason="customer_opted_out",
        )
    return CheckOutcome(
        name="customer_opt_out",
        passed=True,
        detail=f"customer {context.customer_ref} has not opted out",
    )


def _check_contact_cap(
    decision: DecisionRecord, context: PolicyContext
) -> CheckOutcome:
    """Have we already contacted this customer about this event enough times?"""
    intervention = decision.recommended_intervention
    if not is_contact_intervention(intervention):
        return CheckOutcome(
            name="contact_cap",
            passed=True,
            detail=(
                f"not applicable: {intervention} does not count as a contact"
            ),
        )

    prior = context.prior_authorized_contacts
    if prior >= MAX_CONTACTS_PER_EVENT:
        return CheckOutcome(
            name="contact_cap",
            passed=False,
            detail=(
                f"{prior} contact(s) already authorized for event "
                f"{decision.event_id}, cap is {MAX_CONTACTS_PER_EVENT} per event "
                "across all decision versions"
            ),
            reason="contact_cap_exceeded",
        )
    return CheckOutcome(
        name="contact_cap",
        passed=True,
        detail=(
            f"{prior} of {MAX_CONTACTS_PER_EVENT} contacts used for event "
            f"{decision.event_id}"
        ),
    )


def _check_cooldown(decision: DecisionRecord, context: PolicyContext) -> CheckOutcome:
    """Was the last contact about this event too recent?"""
    intervention = decision.recommended_intervention
    if not is_contact_intervention(intervention):
        return CheckOutcome(
            name="contact_cooldown",
            passed=True,
            detail=f"not applicable: {intervention} does not contact the customer",
        )

    last = context.last_authorized_contact_at
    if last is None:
        return CheckOutcome(
            name="contact_cooldown",
            passed=True,
            detail=(
                f"no prior authorized contact for event {decision.event_id}, so "
                f"the {COOLDOWN_HOURS}h cooldown has not started"
            ),
        )

    elapsed = context.now - last
    elapsed_hours = elapsed.total_seconds() / 3600.0
    if elapsed < COOLDOWN:
        remaining_hours = (COOLDOWN - elapsed).total_seconds() / 3600.0
        return CheckOutcome(
            name="contact_cooldown",
            passed=False,
            detail=(
                f"last authorized contact was {elapsed_hours:.1f}h ago at "
                f"{last.isoformat()}, inside the {COOLDOWN_HOURS}h cooldown; "
                f"{remaining_hours:.1f}h remaining"
            ),
            reason="cooldown_active",
        )
    return CheckOutcome(
        name="contact_cooldown",
        passed=True,
        detail=(
            f"last authorized contact was {elapsed_hours:.1f}h ago, outside the "
            f"{COOLDOWN_HOURS}h cooldown"
        ),
    )


def _check_erv_minimum(decision: DecisionRecord) -> CheckOutcome:
    """Is the expected recovery worth the operational overhead?

    Zero-cost interventions are exempt by ratified policy: with nothing spent
    there is no downside to weigh, so declining a small positive expectation would
    simply forgo free money.
    """
    erv = decision.expected_recovery_value
    cost = decision.estimated_cost

    if not erv_floor_applies(cost):
        return CheckOutcome(
            name="erv_minimum",
            passed=True,
            detail=(
                f"exempt: {decision.recommended_intervention} costs "
                f"{cost:,.2f} so the {MINIMUM_ERV:,.2f} floor does not apply "
                f"(ERV {erv:,.2f})"
            ),
        )

    if erv < MINIMUM_ERV:
        return CheckOutcome(
            name="erv_minimum",
            passed=False,
            detail=(
                f"ERV {erv:,.2f} is below the {MINIMUM_ERV:,.2f} minimum for an "
                f"action costing {cost:,.2f}"
            ),
            reason="erv_below_minimum",
        )
    return CheckOutcome(
        name="erv_minimum",
        passed=True,
        detail=(
            f"ERV {erv:,.2f} clears the {MINIMUM_ERV:,.2f} minimum at a cost of "
            f"{cost:,.2f}"
        ),
    )


def _check_amount_tier(decision: DecisionRecord) -> CheckOutcome:
    """Is this amount inside the agent's autonomous authority?

    Failure here routes to `requires_manual_review`, not `blocked`. Nothing is
    wrong with the recommendation; it is simply larger than the agent is trusted
    to commit alone.
    """
    amount = decision.revenue_at_risk
    tier = tier_for(amount)

    if tier == "never_auto":
        return CheckOutcome(
            name="amount_tier",
            passed=False,
            detail=(
                f"{amount:,.2f} is at or above the {NEVER_AUTO_AT_OR_ABOVE:,.2f} "
                "never-auto ceiling; a human must decide this one"
            ),
            reason="amount_never_auto",
        )
    if tier == "approval_required":
        return CheckOutcome(
            name="amount_tier",
            passed=False,
            detail=(
                f"{amount:,.2f} is at or above the {AUTO_AUTHORIZE_BELOW:,.2f} "
                f"autonomous limit and below {NEVER_AUTO_AT_OR_ABOVE:,.2f}, so it "
                "needs approval"
            ),
            reason="amount_requires_approval",
        )
    return CheckOutcome(
        name="amount_tier",
        passed=True,
        detail=(
            f"{amount:,.2f} is below the {AUTO_AUTHORIZE_BELOW:,.2f} autonomous "
            "limit"
        ),
    )


# ---------------------------------------------------------------------------
# Evaluation.
# ---------------------------------------------------------------------------


def run_checks(
    *, decision: DecisionRecord, context: PolicyContext
) -> list[CheckOutcome]:
    """Evaluate every policy check, in trail order.

    All of them, always. This function contains no early return, which is why the
    trail cannot come back partial.
    """
    outcomes = [
        _check_actionable(decision),
        _check_opt_out(decision, context),
        _check_contact_cap(decision, context),
        _check_cooldown(decision, context),
        _check_erv_minimum(decision),
        _check_amount_tier(decision),
    ]

    produced = tuple(outcome.name for outcome in outcomes)
    if produced != POLICY_CHECKS:
        raise RuntimeError(
            "policy checks ran out of step with the declared trail: "
            f"ran {produced}, declared {POLICY_CHECKS}"
        )
    return outcomes


def evaluate(*, decision: DecisionRecord, context: PolicyContext) -> PolicyVerdict:
    """Decide whether one specific recommendation is permitted.

    The verdict is derived from the reason code rather than chosen alongside it,
    so `REASON_VERDICT` is the single place that determines whether a given
    failure blocks or routes for review.
    """
    outcomes = run_checks(decision=decision, context=context)

    failed_reasons = {
        outcome.reason for outcome in outcomes if outcome.reason is not None
    }
    reason = primary_reason(failed_reasons)
    verdict = REASON_VERDICT[reason]

    return PolicyVerdict(
        event_id=decision.event_id,
        decision_id=decision.id,
        decision_version=decision.version,
        verdict=verdict,
        reason=reason,
        checks_performed=[outcome.entry() for outcome in outcomes],
        evaluated_at=context.now,
    )
