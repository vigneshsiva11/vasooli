"""Response models for metrics and audit (Stage 7 — reading, and nothing else).

This is the only stage with no write path, and the models are shaped to make that
visible rather than merely true:

* every model here is a *response*. There is no request body model in this module,
  because no endpoint in this stage accepts one. A field a caller could set is a
  field a caller could use to change something;
* the aggregates that could be misread as claims carry the claim's own provenance
  beside them. `BaselineComparison` separates `kind="real"` from
  `kind="simulated"` at the type level, and `MetricsSummary` reports how many
  verification records it *ignored* as well as how many it counted, so a headline
  number cannot be quoted without the arithmetic that produced it;
* a rate whose denominator is zero is `None`, not `0.0`. Nothing in a dashboard
  distinguishes "0% of 0" from "0% of 400" once it has been rendered as a bar, and
  the first is not a measurement.

`extra="forbid"` throughout, for the same reason the earlier stages use it: a
field nobody declared is a field nobody ratified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.decision import DecisionRecord
from app.models.diagnosis import DiagnosisRecord
from app.models.events import RevenueEventRecord
from app.models.execution import ExecutionRecordDocument
from app.models.policy import PolicyVerdictRecord
from app.models.promise import PromiseToPayDocument
from app.models.verification import VerificationRecordDocument

# ---------------------------------------------------------------------------
# Part A — core recovery metrics.
# ---------------------------------------------------------------------------


class MetricsSummary(BaseModel):
    """Headline recovery numbers, computed on every request.

    Nothing here is cached or stored. The five fields the dashboard displays are
    the first five; the rest exist so the first five can be checked rather than
    trusted.
    """

    model_config = ConfigDict(extra="forbid")

    total_revenue_at_risk: float = Field(
        ...,
        description="Sum of `amount` over every stored RevenueEvent. Nothing excluded.",
    )
    total_revenue_recovered: float = Field(
        ...,
        description=(
            "Money that actually came back, counted once per execution. See "
            "`duplicate_verification_records_ignored` and `methodology` — the raw "
            "sum over recovered verification records is higher and is not a "
            "quantity of money."
        ),
    )
    recovery_rate: float | None = Field(
        ...,
        description=(
            "total_revenue_recovered / total_revenue_at_risk, as a percentage. "
            "Null when nothing is at risk."
        ),
    )
    events_by_status: dict[str, int] = Field(
        ...,
        description=(
            "Count per `RevenueEvent.status`. Every declared status is present, "
            "including the ones at zero, so a caller never has to distinguish "
            "'none' from 'key absent'."
        ),
    )
    total_events_processed: int = Field(
        ...,
        description="Distinct events with at least one Decision.",
    )

    # --- the arithmetic behind the headline numbers ---

    total_events: int = Field(..., description="Every stored event, processed or not.")
    events_without_decision: int = Field(
        ...,
        description=(
            "total_events - total_events_processed. An event ingested but never "
            "decided on is not a failure of recovery; it is work not yet done."
        ),
    )
    currencies: list[str] = Field(
        ...,
        description=(
            "Every distinct currency in the event set. The money totals above are "
            "plain sums, so they are only meaningful while this has one entry."
        ),
    )
    non_recoverable_at_risk: float = Field(
        ...,
        description=(
            "Of `total_revenue_at_risk`, how much sits on events whose latest "
            "diagnosis says recoverable=False — fraud, disputes, deliberate "
            "cancellation. Money the system decided not to chase, reported "
            "separately because leaving it in the denominator depresses the "
            "recovery rate and removing it silently would flatter it."
        ),
    )
    recovered_verification_records: int = Field(
        ...,
        description="Verification records with outcome='recovered'.",
    )
    distinct_recoveries_counted: int = Field(
        ...,
        description="Of those, how many distinct executions they cover.",
    )
    duplicate_verification_records_ignored: int = Field(
        ...,
        description=(
            "The difference. Each is a legitimate append-only record of a webhook "
            "delivery, but a payment link is paid once, so summing them counts the "
            "same money repeatedly."
        ),
    )

    # --- how the recovery was established (Stage 9) ---
    #
    # `total_revenue_recovered` is the sum of the two amounts below and is reported
    # unsplit as well, because it is the honest answer to "how much came back". The
    # split exists because the two are not equally well evidenced, and a dashboard
    # that added them into one bar would present a merchant's assertion as the
    # gateway's word.

    gateway_verified_recovered: float = Field(
        ...,
        description=(
            "Of `total_revenue_recovered`, the portion Razorpay confirmed by signed "
            "webhook about a payment link it hosts. This is the number to quote when "
            "the claim is 'money verifiably returned'."
        ),
    )
    manually_asserted_recovered: float = Field(
        ...,
        description=(
            "Of `total_revenue_recovered`, the portion a merchant asserted through "
            "POST /executions/{id}/confirm-payment after a contact-type "
            "intervention. Real recovery of real receivables, and NOT gateway-"
            "verified: no third party attests to it. Kept separate so it can never "
            "be quoted as verified money by accident."
        ),
    )
    recovery_rate_gateway_verified: float | None = Field(
        ...,
        description=(
            "gateway_verified_recovered / total_revenue_at_risk, as a percentage. "
            "The conservative reading of `recovery_rate`: identical to it before any "
            "manual confirmation exists, and lower afterwards. Null when nothing is "
            "at risk."
        ),
    )
    distinct_recoveries_gateway_verified: int = Field(
        ...,
        description=(
            "Of `distinct_recoveries_counted`, how many rest on a webhook. Records "
            "written before Stage 9 carry no `source` field and are all webhook "
            "records, so they count here."
        ),
    )
    distinct_recoveries_manually_asserted: int = Field(
        ...,
        description="Of `distinct_recoveries_counted`, how many rest on an assertion.",
    )
    methodology: str = Field(
        ..., description="How these numbers were derived, in plain words."
    )
    computed_at: datetime = Field(
        ..., description="When this response was computed (UTC). Not a cache stamp."
    )


class RootCauseMetrics(BaseModel):
    """One row of the by-root-cause table."""

    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(..., description="The root cause, across all surfaces.")
    surfaces: list[str] = Field(
        ...,
        description=(
            "Which surfaces contributed. Several root causes are shared — "
            "`card_expired` and `insufficient_funds` occur on both payment and "
            "subscription — so a row is not a surface."
        ),
    )
    events: int = Field(
        ...,
        description=(
            "Events whose LATEST diagnosis names this cause. Counted on the latest "
            "rather than on every version, so a re-diagnosed event appears once."
        ),
    )
    revenue_at_risk: float = Field(..., description="Sum of those events' amounts.")
    revenue_recovered: float = Field(
        ..., description="Deduplicated recovered amount on those events."
    )
    recovery_rate: float | None = Field(
        ...,
        description="recovered / at_risk as a percentage; null when nothing at risk.",
    )
    superseded_only: bool = Field(
        ...,
        description=(
            "True when this cause appears only in diagnoses that were later "
            "replaced. Such a row is real history with zero current events, and "
            "saying so is why it is not silently dropped."
        ),
    )


class InterventionMetrics(BaseModel):
    """One row of the intervention-performance table."""

    model_config = ConfigDict(extra="forbid")

    intervention: str = Field(..., description="From the fixed Stage 3 catalogue.")
    times_recommended: int = Field(
        ...,
        description=(
            "Decision documents naming it. Every version counts: a re-decision is "
            "a second occasion on which this was the recommendation."
        ),
    )
    times_authorized: int = Field(
        ...,
        description=(
            "Policy verdicts with verdict='authorized' whose decision names it. "
            "Verdicts carry no intervention of their own, so this is resolved "
            "through `decision_id`."
        ),
    )
    times_executed: int = Field(
        ..., description="Execution records with status='completed'."
    )
    recovery_rate: float | None = Field(
        ...,
        description=(
            "Distinct recoveries / times_executed, as a percentage. Null when "
            "nothing was executed — which is every no-action variant, by "
            "construction."
        ),
    )

    # --- what the rate does and does not measure ---

    times_execution_failed: int = Field(
        ...,
        description="Execution records with status='failed'. Excluded from the rate.",
    )
    recoveries: int = Field(
        ..., description="The rate's numerator: deduplicated recovered executions."
    )
    revenue_recovered: float = Field(
        ..., description="Money attributed to executions of this intervention."
    )
    action_type: str | None = Field(
        ...,
        description="What executing it produces. Null for the no-action variants.",
    )
    verifiable: bool = Field(
        ...,
        description=(
            "Whether an execution of this intervention can be verified by the "
            "GATEWAY. Only link-type actions produce a Razorpay artifact a webhook "
            "can report on; a logged contact produces none. The field name predates "
            "Stage 9 and its value is unchanged — read it as `gateway_verifiable`, "
            "and read `manually_confirmable` beside it, because a contact-type "
            "intervention is no longer unverifiable outright. See "
            "docs/data-corrections.md."
        ),
    )
    manually_confirmable: bool = Field(
        ...,
        description=(
            "Whether an execution of this intervention can be confirmed by the "
            "merchant through POST /executions/{id}/confirm-payment. True for the "
            "contact-type interventions, and the exact complement of `verifiable` "
            "for anything with an action_type: the two channels are exclusive by "
            "design, because a manual override on a channel that has real "
            "verification available is the same as not having it. Both are false for "
            "the no-action variants, which execute nothing."
        ),
    )

    # --- how this intervention's recoveries were established (Stage 9) ---

    recoveries_gateway_verified: int = Field(
        ...,
        description=(
            "Of `recoveries`, how many rest on a signed Razorpay webhook. Equal to "
            "`recoveries` for every link-type intervention."
        ),
    )
    recoveries_manually_asserted: int = Field(
        ...,
        description=(
            "Of `recoveries`, how many rest on a merchant's assertion. Necessarily 0 "
            "for a link-type intervention, since the manual path refuses those."
        ),
    )
    revenue_recovered_gateway_verified: float = Field(
        ...,
        description="Of `revenue_recovered`, the gateway-confirmed portion.",
    )
    revenue_recovered_manually_asserted: float = Field(
        ...,
        description=(
            "Of `revenue_recovered`, the asserted portion. A non-zero figure on a "
            "contact-type row is the whole point of Stage 9; it is also money no "
            "third party attests to, which is why it is a separate column and not "
            "folded into the one above."
        ),
    )
    recovery_rate_gateway_verified: float | None = Field(
        ...,
        description=(
            "recoveries_gateway_verified / times_executed, as a percentage. The "
            "conservative reading of `recovery_rate`, which counts both sources. "
            "Structurally 0 for a contact-type intervention — that is what "
            "`verifiable: false` means, and it is 'unobservable by the gateway', not "
            "'ineffective'."
        ),
    )


class PromiseMetrics(BaseModel):
    """Promise-to-pay outcomes."""

    model_config = ConfigDict(extra="forbid")

    total_promises: int = Field(
        ...,
        description=(
            "Every promise document. An event that broke one and committed again "
            "has two, and both are real commitments."
        ),
    )
    honored: int = Field(..., description="Promises money arrived for.")
    broken: int = Field(
        ..., description="Deadline passed, nothing arrived, not yet chased."
    )
    still_open: int = Field(..., description="promised + reevaluating.")
    honor_rate: float | None = Field(
        ...,
        description=(
            "honored / (honored + broken) as a percentage. Still-open promises are "
            "excluded because they have not resolved. Null when nothing has "
            "resolved either way."
        ),
    )

    # --- the split behind `still_open` ---

    promised: int = Field(..., description="Open and not yet due, or due today.")
    reevaluating: int = Field(
        ...,
        description=(
            "Broken, then chased through the policy gate, now waiting again. "
            "Counted as still-open per the ratio's definition even though the "
            "original date was already missed — which makes honor_rate the "
            "optimistic reading. `reevaluating` is stated here so the pessimistic "
            "one can be computed by anyone who prefers it."
        ),
    )
    methodology: str = Field(..., description="What the ratio does and does not count.")


# ---------------------------------------------------------------------------
# Part B — baseline comparison. Simulation, labelled as such at the type level.
# ---------------------------------------------------------------------------


class EventBasis(BaseModel):
    """The event set all four figures below are computed over.

    A comparison across different denominators is not a comparison, so this is
    stated once and shared rather than restated per strategy.
    """

    model_config = ConfigDict(extra="forbid")

    total_events: int = Field(..., description="Every stored event.")
    events_with_diagnosis: int = Field(
        ...,
        description=(
            "Events a root cause is known for. An undiagnosed event is excluded "
            "from every strategy, including Vasooli's — there is nothing to apply."
        ),
    )
    eligible_events: int = Field(
        ...,
        description=(
            "Of those, the ones not hard-blocked by the recoverable=false gate. "
            "The basis for all four figures."
        ),
    )
    excluded_non_recoverable: int = Field(
        ..., description="Fraud, dispute, voluntary churn, revoked mandate."
    )
    excluded_undiagnosed: int = Field(..., description="No diagnosis, so no root cause.")
    eligible_revenue_at_risk: float = Field(
        ..., description="Sum of the eligible events' amounts. The ceiling on all four."
    )


class SimulatedBaseline(BaseModel):
    """A what-if figure. `kind` is a `Literal`, so it cannot be relabelled real."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["simulated"] = Field(
        ...,
        description=(
            "Always 'simulated'. Nothing was sent, nothing was charged, and no "
            "money moved to produce this number."
        ),
    )
    strategy: str = Field(..., description="What this baseline does, in one line.")
    intervention_family: list[str] = Field(
        ...,
        description=(
            "The interventions this baseline is allowed to draw a probability "
            "from. Where a pair offers more than one, the highest is used — see "
            "`methodology`."
        ),
    )
    gross_expected_recovery: float = Field(
        ...,
        description=(
            "Sum over eligible events of probability x amount. GROSS: no "
            "intervention cost subtracted, so this isolates the effect of "
            "intervention choice from economics."
        ),
    )
    events_scored: int = Field(..., description="Eligible events, all of them.")
    events_with_defined_probability: int = Field(
        ...,
        description=(
            "Events where the Stage 3 matrix actually defines an intervention of "
            "this family for their (surface, root_cause) pair."
        ),
    )
    events_scored_zero_no_defined_pairing: int = Field(
        ...,
        description=(
            "Events scored at probability 0 because the matrix defines no such "
            "pairing. That silence is the matrix's ratified judgement — it says "
            "retrying an expired card cannot help, and that an abandoned cart has "
            "no charge to retry — so 0 is its position, not an invented number. "
            "Reported because a baseline scoring zero on most of the set is not a "
            "broadly-applicable strategy, and the headline figure alone hides that."
        ),
    )
    probability_source: str = Field(
        ..., description="Where the probabilities came from. Never a new estimate."
    )


class VasooliExpected(BaseModel):
    """Vasooli's own what-if figure, on the same basis as the two baselines."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["simulated"] = Field(
        ..., description="Always 'simulated'. This is what-if math, not an outcome."
    )
    strategy: str = Field(..., description="What was applied, in one line.")
    expected_recovery_value_net: float = Field(
        ...,
        description=(
            "Sum of `expected_recovery_value` across the latest real Decision per "
            "eligible event. NET of intervention cost, because that is what the "
            "stored ERV is — the number Stage 4 authorized against."
        ),
    )
    gross_expected_recovery: float = Field(
        ...,
        description=(
            "The same decisions re-scored as probability x amount, cost excluded. "
            "This, not the net figure, is the like-for-like comparison against the "
            "two gross baselines."
        ),
    )
    total_intervention_cost: float = Field(
        ..., description="The difference between the two figures above."
    )
    decisions_counted: int = Field(
        ...,
        description=(
            "Latest decision per eligible event, so one action per event — the "
            "same shape as the baselines. Earlier versions are excluded; counting "
            "all versions would give Vasooli more attempts than the baselines get."
        ),
    )
    no_action_decisions: int = Field(
        ...,
        description=(
            "Of those, how many chose to do nothing. They contribute 0 to both "
            "figures, and choosing not to chase is the capability the baselines "
            "lack."
        ),
    )


class VasooliActual(BaseModel):
    """What actually happened. The only real figure in the comparison."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["real"] = Field(
        ...,
        description=(
            "Always 'real'. Not simulated — every rupee below was requested through "
            "Razorpay test mode and then confirmed, so this is not comparable to the "
            "three figures above without saying so. 'Real' is the opposite of "
            "simulated and does NOT mean every rupee is gateway-attested: read the "
            "two source columns below for that."
        ),
    )
    revenue_recovered: float = Field(
        ...,
        description=(
            "Deduplicated recovered amount from VerificationRecords, across both "
            "verification sources. The same number as `total_revenue_recovered` on "
            "/metrics/summary, and the exact sum of the two fields below."
        ),
    )
    revenue_recovered_gateway_verified: float = Field(
        ...,
        description=(
            "Of the above, the portion Razorpay confirmed by signed webhook. The "
            "figure to quote against the simulated baselines when the claim is that "
            "a third party attests to the outcome."
        ),
    )
    revenue_recovered_manually_asserted: float = Field(
        ...,
        description=(
            "Of the above, the portion a merchant asserted after a contact-type "
            "intervention (Stage 9). Real recovery, no gateway attestation — split "
            "out here for the same reason it is split on /metrics/summary."
        ),
    )
    events_recovered: int = Field(..., description="Distinct events money came back on.")
    executions_verified_recovered: int = Field(
        ..., description="Distinct executions a recovery was verified for."
    )
    caveat: str = Field(
        ...,
        description=(
            "Why this figure is far below the three simulated ones, stated in the "
            "response so the gap is never read as underperformance."
        ),
    )


class BaselineComparison(BaseModel):
    """Three simulated strategies and one real outcome, on one event basis."""

    model_config = ConfigDict(extra="forbid")

    methodology: str = Field(
        ...,
        description=(
            "What is real, what is simulated, and what each number may be used to "
            "claim. Read before quoting any figure below."
        ),
    )
    event_basis: EventBasis = Field(..., description="The shared denominator.")
    baseline_retry_everything: SimulatedBaseline = Field(
        ..., description="BASELINE A — one retry per eligible event."
    )
    baseline_generic_reminder: SimulatedBaseline = Field(
        ..., description="BASELINE B — one generic reminder per eligible event."
    )
    vasooli_expected: VasooliExpected = Field(
        ..., description="SIMULATED — the real decisions' expected value."
    )
    vasooli_actual: VasooliActual = Field(
        ..., description="REAL — money that came back."
    )
    computed_at: datetime = Field(..., description="When this was computed (UTC).")


# ---------------------------------------------------------------------------
# Part C — the audit trail.
# ---------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    """One thing that happened to an event, in the order it happened."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(..., description="The record's own timestamp (UTC).")
    stage: str = Field(..., description="Which stage produced it, e.g. 'diagnosis'.")
    record_id: str = Field(..., description="Document id of the full record below.")
    summary: str = Field(
        ...,
        description=(
            "One line, assembled from the record's own fields. A rendering of "
            "stored data, not an interpretation of it."
        ),
    )


class FingerprintUse(BaseModel):
    """One rulebook fingerprint appearing in this event's verdicts."""

    model_config = ConfigDict(extra="forbid")

    rulebook_fingerprint: str = Field(..., description="The fingerprint itself.")
    source: str = Field(
        ..., description="evaluated, reconstructed, or backfilled."
    )
    verdict_versions: list[int] = Field(
        ..., description="Which verdict versions carry it."
    )
    attests_to_rulebook_in_force: bool = Field(
        ...,
        description=(
            "False for `backfilled`: the verdict predates fingerprinting and its "
            "true rulebook is unrecoverable, so re-deriving it proves only that it "
            "is consistent with a rulebook, not with the one that judged it."
        ),
    )


class AuditTrail(BaseModel):
    """One event's complete history, gathered from every stage that touched it.

    Assembly only. Every record here is returned as the stage that owns it stored
    it — no recomputation, no re-derivation, and no field this stage invented.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="The event this trail is for.")
    event: RevenueEventRecord = Field(..., description="Stage 1 — what was ingested.")
    diagnoses: list[DiagnosisRecord] = Field(
        ..., description="Stage 2 — every version, oldest first."
    )
    decisions: list[DecisionRecord] = Field(
        ..., description="Stage 3 — every version, oldest first."
    )
    policy_verdicts: list[PolicyVerdictRecord] = Field(
        ...,
        description=(
            "Stage 4 — every version, oldest first, each carrying the fingerprint "
            "of the rulebook that judged it."
        ),
    )
    executions: list[ExecutionRecordDocument] = Field(
        ..., description="Stage 5 — every action taken, oldest first."
    )
    verifications: list[VerificationRecordDocument] = Field(
        ...,
        description=(
            "Stage 6 — every webhook processed, oldest first. More than one may "
            "report the same payment; see `record_counts`."
        ),
    )
    promises: list[PromiseToPayDocument] = Field(
        ..., description="Stage 6 Part B — every commitment, oldest first."
    )
    timeline: list[TimelineEntry] = Field(
        ...,
        description=(
            "All of the above interleaved by timestamp — the story in one list. "
            "The full records are still above; this is a merge, not a substitute."
        ),
    )
    rulebook_fingerprints: list[FingerprintUse] = Field(
        ...,
        description="Which rulebooks judged this event, and whether each attests.",
    )
    record_counts: dict[str, int] = Field(
        ..., description="How many records of each kind, for a quick shape check."
    )
    distinct_recoveries: int = Field(
        ...,
        description=(
            "Recovered verifications after deduplication by execution. Below "
            "`record_counts['verifications_recovered']` when a webhook was "
            "delivered more than once."
        ),
    )
    revenue_recovered: float = Field(
        ..., description="Deduplicated recovered amount for this event."
    )
    assembled_at: datetime = Field(..., description="When this was assembled (UTC).")
