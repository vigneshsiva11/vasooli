"""Domain models for policy (Stage 4 — whether a recommendation is permitted).

The boundary this module enforces:

* `verdict` and `reason` are both `Literal`s, and `REASON_VERDICT` pins each
  reason to exactly one permitted verdict. `authorized` + `customer_opted_out`
  is not a state this model can hold.
* `checks_performed` must cover every check in `POLICY_CHECKS` exactly once, in
  a parseable format. A partial trail is not storable, so "we only recorded the
  first failure" cannot happen silently.
* The reported `reason` must correspond to a check that actually FAILED, and
  `authorized` requires that nothing failed. The verdict cannot disagree with
  its own evidence.
* There is no field for execution: no payment-link id, no `executed`, no
  `amount_charged`, no recipient. With `extra="forbid"`, one cannot be added by
  a caller. This stage grants permission; performing the action is Stage 5.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ---------------------------------------------------------------------------
# Verdicts and reason codes.
# ---------------------------------------------------------------------------

#: `requires_manual_review` is deliberately distinct from `blocked`. A blocked
#: recommendation is refused on grounds that a human should not casually
#: override (consent, contact limits, economics). A reviewable one is merely
#: above the autonomy ceiling, and a future Stage 4b could approve it. Collapsing
#: the two would close off that capability.
PolicyVerdictName = Literal["authorized", "blocked", "requires_manual_review"]

PolicyReason = Literal[
    "ok",
    # Nothing was recommended, so there is nothing to permit.
    "no_action_recommended",
    # Consent.
    "customer_opted_out",
    # Customer-protection limits.
    "contact_cap_exceeded",
    "cooldown_active",
    # Economics.
    "erv_below_minimum",
    # Autonomy ceiling — routing to a human, not a refusal.
    "amount_never_auto",
    "amount_requires_approval",
]

ALLOWED_VERDICTS: frozenset[str] = frozenset(get_args(PolicyVerdictName))
ALLOWED_REASONS: frozenset[str] = frozenset(get_args(PolicyReason))

#: The only verdict each reason may carry. This is what makes a contradictory
#: verdict unconstructable rather than merely discouraged.
REASON_VERDICT: dict[str, str] = {
    "ok": "authorized",
    "no_action_recommended": "blocked",
    "customer_opted_out": "blocked",
    "contact_cap_exceeded": "blocked",
    "cooldown_active": "blocked",
    "erv_below_minimum": "blocked",
    "amount_never_auto": "requires_manual_review",
    "amount_requires_approval": "requires_manual_review",
}

#: Which reason is reported when several checks fail at once, most binding
#: first. Ratified ordering: every `blocked` reason outranks every
#: `requires_manual_review` reason, because a reviewable verdict can later be
#: approved by a human and a genuine refusal must not be downgraded into
#: something overridable. Within blocks: consent, then customer protection,
#: then economics.
#:
#: `no_action_recommended` leads because if nothing was recommended, every other
#: reason is describing a hypothetical action.
REASON_PRECEDENCE: tuple[str, ...] = (
    "no_action_recommended",
    "customer_opted_out",
    "contact_cap_exceeded",
    "cooldown_active",
    "erv_below_minimum",
    "amount_never_auto",
    "amount_requires_approval",
)

# ---------------------------------------------------------------------------
# The evaluation trail.
# ---------------------------------------------------------------------------

#: Every check the policy engine performs, always, in trail order. Requiring the
#: stored trail to cover exactly this set is what enforces "record every rule
#: evaluated, not just the one that blocked" at the contract level rather than
#: relying on the engine to be well behaved.
POLICY_CHECKS: tuple[str, ...] = (
    "decision_is_actionable",
    "customer_opt_out",
    "contact_cap",
    "contact_cooldown",
    "erv_minimum",
    "amount_tier",
)

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"

#: The check whose failure justifies each reason. A reason may only be reported
#: if its own check is recorded as FAIL.
CHECK_FOR_REASON: dict[str, str] = {
    "no_action_recommended": "decision_is_actionable",
    "customer_opted_out": "customer_opt_out",
    "contact_cap_exceeded": "contact_cap",
    "cooldown_active": "contact_cooldown",
    "erv_below_minimum": "erv_minimum",
    "amount_never_auto": "amount_tier",
    "amount_requires_approval": "amount_tier",
}

#: `<check_name>: PASS|FAIL (<detail>)` — machine-readable so the validators
#: below can check the trail against the verdict, not merely that it is non-empty.
CHECK_ENTRY_PATTERN = r"^[a-z][a-z0-9_]*: (?:PASS|FAIL) \(.+\)$"
_CHECK_ENTRY = re.compile(CHECK_ENTRY_PATTERN)

_OBJECT_ID_PATTERN = r"^[0-9a-fA-F]{24}$"

CheckEntry = Annotated[
    str,
    StringConstraints(min_length=1, max_length=400, pattern=CHECK_ENTRY_PATTERN),
]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime, to the millisecond.

    Truncated because BSON stores datetimes at millisecond precision: minting a
    microsecond-precision value would mean the API reported `...622179Z` on
    creation and `...622000Z` on every subsequent read of the same fact. A record
    the system asks you to trust should not change when you read it back, and
    sub-millisecond precision is meaningless to rules measured in hours.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def format_check(name: str, passed: bool, detail: str) -> str:
    """Render one trail entry in the canonical format.

    Defined here, beside the pattern that validates it, so the producer and the
    validator cannot drift apart.
    """
    if name not in POLICY_CHECKS:
        raise ValueError(f"{name!r} is not a declared policy check")
    collapsed = " ".join(detail.split())
    if not collapsed:
        raise ValueError(f"check {name!r} was recorded without a detail")
    return f"{name}: {CHECK_PASS if passed else CHECK_FAIL} ({collapsed})"


def check_name(entry: str) -> str:
    """Return the check name from a trail entry."""
    return entry.split(":", 1)[0]


def check_failed(entry: str) -> bool:
    """Whether a trail entry records a failure."""
    return entry.split(": ", 1)[1].startswith(CHECK_FAIL)


def primary_reason(failed_reasons: set[str]) -> str:
    """Return the highest-precedence reason among those that failed.

    Returns `"ok"` when nothing failed.
    """
    for reason in REASON_PRECEDENCE:
        if reason in failed_reasons:
            return reason
    return "ok"


# ---------------------------------------------------------------------------
# The policy contract.
# ---------------------------------------------------------------------------


class PolicyVerdict(BaseModel):
    """Whether one specific `Decision` is permitted to proceed to execution.

    Note what is absent, deliberately. No payment-link id, no `executed`, no
    `amount_charged`, no recipient, no send timestamp. This model can say "this
    is allowed"; it has no vocabulary for "this was done".
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="The `RevenueEvent.event_id` this verdict concerns.",
    )
    decision_id: str = Field(
        ...,
        pattern=_OBJECT_ID_PATTERN,
        description=(
            "MongoDB id of the exact decision document this verdict evaluates. "
            "Decisions are append-only, so pinning the id is what prevents a "
            "verdict from being read as permission for a later, different "
            "recommendation."
        ),
    )
    decision_version: int = Field(
        ...,
        ge=1,
        description="Version of that decision, carried for human readability.",
    )
    verdict: PolicyVerdictName = Field(
        ...,
        description="authorized, blocked, or requires_manual_review.",
    )
    reason: PolicyReason = Field(
        ...,
        description="Primary reason code, from the fixed set.",
    )
    checks_performed: list[CheckEntry] = Field(
        ...,
        min_length=len(POLICY_CHECKS),
        max_length=len(POLICY_CHECKS),
        description=(
            "The complete evaluation trail: every check in POLICY_CHECKS, "
            "whether it passed or failed."
        ),
    )
    evaluated_at: datetime = Field(
        default_factory=_utc_now,
        description="When the verdict was produced (UTC).",
    )

    @model_validator(mode="after")
    def _trail_must_be_complete(self) -> "PolicyVerdict":
        """Every declared check must appear exactly once.

        This is the contract-level version of "don't short-circuit": a trail
        missing a check cannot be stored, so a verdict that stopped evaluating
        at the first failure is not representable.
        """
        names = [check_name(entry) for entry in self.checks_performed]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"checks_performed repeats {sorted(duplicates)}")
        missing = set(POLICY_CHECKS) - set(names)
        if missing:
            raise ValueError(
                f"checks_performed is incomplete; missing {sorted(missing)}. "
                "Every check must be recorded, including ones that passed."
            )
        unknown = set(names) - set(POLICY_CHECKS)
        if unknown:
            raise ValueError(f"checks_performed names undeclared checks {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def _reason_must_match_verdict(self) -> "PolicyVerdict":
        """Reject a reason paired with a verdict it cannot carry."""
        required = REASON_VERDICT[self.reason]
        if self.verdict != required:
            raise ValueError(
                f"reason {self.reason!r} requires verdict {required!r}, not "
                f"{self.verdict!r}"
            )
        return self

    @model_validator(mode="after")
    def _verdict_must_match_its_evidence(self) -> "PolicyVerdict":
        """An authorization requires a clean trail; a refusal requires a failure."""
        failures = [entry for entry in self.checks_performed if check_failed(entry)]

        if self.verdict == "authorized" and failures:
            raise ValueError(
                f"verdict 'authorized' but {len(failures)} check(s) failed: "
                f"{[check_name(entry) for entry in failures]}"
            )
        if self.verdict != "authorized" and not failures:
            raise ValueError(
                f"verdict {self.verdict!r} but every check passed; a refusal "
                "must be justified by a failed check"
            )
        return self

    @model_validator(mode="after")
    def _reported_reason_must_have_failed(self) -> "PolicyVerdict":
        """The reported reason's own check must be the one that failed.

        Without this, a verdict could be blocked for a real failure but report a
        different, more palatable reason code.
        """
        if self.reason == "ok":
            return self

        expected_check = CHECK_FOR_REASON[self.reason]
        for entry in self.checks_performed:
            if check_name(entry) == expected_check:
                if not check_failed(entry):
                    raise ValueError(
                        f"reason {self.reason!r} requires check "
                        f"{expected_check!r} to have failed, but it passed"
                    )
                return self
        raise ValueError(  # pragma: no cover - completeness validator precedes this
            f"reason {self.reason!r} refers to absent check {expected_check!r}"
        )

    @model_validator(mode="after")
    def _reason_must_be_the_highest_precedence_failure(self) -> "PolicyVerdict":
        """Reject a reason that a more binding failure should have outranked.

        Ratified precedence is not advisory: if consent failed, the verdict may
        not report the amount tier instead.
        """
        failed_checks = {
            check_name(entry) for entry in self.checks_performed if check_failed(entry)
        }
        failed_reasons = {
            reason
            for reason, check in CHECK_FOR_REASON.items()
            if check in failed_checks
        }
        # `amount_tier` maps to two reasons; only the one actually reported can
        # be assumed to have applied, so drop the other from the comparison.
        for reason in ("amount_never_auto", "amount_requires_approval"):
            if reason != self.reason:
                failed_reasons.discard(reason)

        expected = primary_reason(failed_reasons)
        if expected != self.reason:
            raise ValueError(
                f"reason {self.reason!r} is outranked by {expected!r} under the "
                f"ratified precedence ordering"
            )
        return self


class PolicyVerdictRecord(PolicyVerdict):
    """A stored `PolicyVerdict`, with its document id and append-only version."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")
    version: int = Field(
        ...,
        ge=1,
        description="1 for the first verdict on an event, incrementing thereafter.",
    )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PolicyVerdictRecord":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class CustomerOptOut(BaseModel):
    """A customer who must not be contacted.

    A placeholder for a real customer-preference service. It holds consent only:
    there is no contact detail here, and no field describing anything sent.
    """

    model_config = ConfigDict(extra="forbid")

    customer_ref: str = Field(
        ...,
        min_length=1,
        description="Merchant-side customer reference, matching `RevenueEvent`.",
    )
    reason: str = Field(
        default="requested",
        min_length=1,
        max_length=200,
        description="Free-text note on why the customer is on the list.",
    )
    opted_out_at: datetime = Field(
        default_factory=_utc_now,
        description="When the opt-out was recorded (UTC).",
    )


class OptOutRequest(BaseModel):
    """Optional body for `POST /opt-out/{customer_ref}`."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="requested",
        min_length=1,
        max_length=200,
        description="Why this customer is being added to the do-not-contact list.",
    )


class OptOutResponse(CustomerOptOut):
    """The stored opt-out, plus whether this call is what created it."""

    created: bool = Field(
        ...,
        description=(
            "True if this call added the customer. False if they were already "
            "opted out, in which case `opted_out_at` is the original timestamp — "
            "when consent was withdrawn is a fact, and asking twice does not "
            "move it."
        ),
    )

