"""Part B — baseline comparison. A SIMULATION, and labelled as one throughout.

What this does: takes the real event set, and asks what a naive strategy would have
been worth on it. What it does not do: re-run the pipeline. No diagnosis is
recomputed, no decision is made, no verdict is issued, nothing is executed, and
nothing is written. Three of the four figures it produces are arithmetic over stored
data; the fourth is the real recovered amount, and the response says which is which
at the type level (`kind: Literal["real"] | Literal["simulated"]`).

The probability question, which is the whole integrity of this comparison
-------------------------------------------------------------------------
Both baselines draw every probability from `app/decision/matrix.py`. None is
invented, adjusted, or interpolated. Two consequences worth stating plainly, because
they are what make the numbers honest rather than flattering:

* where a pair offers more than one intervention of the baseline's family, the
  HIGHEST probability is used. The baseline gets its best case, deliberately: if
  Vasooli still wins, the win is not an artifact of a hobbled comparison;
* where the matrix defines no intervention of that family for a pair, the event
  scores ZERO. This is the load-bearing choice, and it is not an omission being
  papered over. The matrix's silence is its ratified judgement, stated in its own
  assumption strings — retrying a card that has expired "cannot help: the expiry
  date will not change"; an abandoned cart has no charge to retry; a receivable has
  no failed authorization to re-attempt. Assigning any positive number would be
  inventing a probability, which is exactly what this stage was told not to do.

The second point makes both baselines score zero on much of the set, and Baseline B
on nearly all of it — `reminder` and `escalating_reminder_sequence` between them
appear at only three pairs, all on `receivable`. A single headline number would hide
that, so `events_scored_zero_no_defined_pairing` is reported beside it and the
per-strategy coverage is in the response.

Gross, not net
--------------
Both baselines are probability x amount with no cost subtracted, to isolate the
effect of intervention CHOICE from economics. Vasooli's stored ERV is net of cost by
definition, so it is reported BOTH ways: `expected_recovery_value_net` is the real
stored figure, and `gross_expected_recovery` is the like-for-like one. Comparing the
net figure against gross baselines would understate Vasooli by its own costs.
"""

from __future__ import annotations

from app.decision.matrix import candidates_for
from app.metrics.aggregate import is_eligible, split_by_source
from app.metrics.reader import (
    Snapshot,
    distinct_recoveries,
    latest_per_event,
    money,
)
from app.models.decision import NO_ACTION_INTERVENTIONS
from app.models.metrics import (
    BaselineComparison,
    EventBasis,
    SimulatedBaseline,
    VasooliActual,
    VasooliExpected,
)

#: The two families a baseline may draw from. Named by membership in the Stage 3
#: catalogue rather than by string matching on "retry", so adding an intervention
#: forces a decision about which family it joins.
RETRY_INTERVENTIONS: frozenset[str] = frozenset({"immediate_retry", "delayed_retry"})
REMINDER_INTERVENTIONS: frozenset[str] = frozenset(
    {"reminder", "escalating_reminder_sequence"}
)

PROBABILITY_SOURCE = (
    "app/decision/matrix.py, unmodified. Where a (surface, root_cause) pair offers "
    "more than one intervention of this family, the highest probability is used — "
    "the baseline is given its best case on purpose. Where the matrix defines none, "
    "the event scores 0, because the matrix's silence is its stated judgement that "
    "this kind of action cannot work on that failure, and substituting a number "
    "would be inventing one."
)

METHODOLOGY = (
    "THREE OF THESE FOUR FIGURES ARE SIMULATED. baseline_retry_everything, "
    "baseline_generic_reminder and vasooli_expected are arithmetic over stored data: "
    "no action was taken to produce them, no money moved, and they are what-if "
    "estimates built from the Stage 3 matrix's calibrated (not measured) "
    "probabilities. ONLY vasooli_actual is real — money that was actually requested "
    "through Razorpay test mode and then confirmed, either by signed webhook or, for "
    "a contact-type intervention that produces no link for a webhook to report on, by "
    "the merchant; vasooli_actual splits the two and only the webhook portion is "
    "attested by a third party. The two are not "
    "comparable as achievement: the simulated figures score every eligible event, "
    "while the real one reflects the small number of events actually driven through "
    "execution and verification during development. The comparison that IS valid is "
    "simulated-against-simulated, on the same event set, with the same probabilities "
    "applied differently: that isolates the effect of choosing an intervention by "
    "root cause versus applying one uniformly. All baselines are GROSS (probability "
    "x amount, no cost subtracted) to keep that comparison about intervention "
    "choice rather than economics; vasooli_expected is reported both gross and net "
    "so it can be compared like for like."
)

ACTUAL_CAVEAT = (
    "This is far below the three simulated figures and that gap is not "
    "underperformance. The simulated figures score every eligible event; this one "
    "counts only events actually executed and then confirmed during development. Two "
    "structural limits also apply: contact-type interventions produce no Razorpay "
    "artifact, so no webhook can ever report a recovery for them (see "
    "docs/data-corrections.md); and each recovery is counted once per execution "
    "rather than once per verification record. Since Stage 9 that first limit is a "
    "limit on GATEWAY verification only — a contact-type recovery can be confirmed by "
    "the merchant instead, which is why revenue_recovered is split into "
    "revenue_recovered_gateway_verified and revenue_recovered_manually_asserted. "
    "Only the first is attested by a third party, and it is the one to compare "
    "against the simulated baselines."
)


def _best_probability(
    surface: str, root_cause: str, family: frozenset[str]
) -> tuple[str, float] | None:
    """Highest probability the matrix gives this pair for any intervention in `family`.

    Returns None when the matrix defines no such intervention for the pair — which
    the caller must score as zero rather than substituting a default.
    """
    try:
        candidates = candidates_for(surface, root_cause)
    except KeyError:  # pragma: no cover - the matrix's import-time check covers all pairs
        return None
    eligible = [
        candidate
        for candidate in candidates
        if candidate.intervention in family
        and candidate.intervention not in NO_ACTION_INTERVENTIONS
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda candidate: candidate.recovery_probability)
    return best.intervention, best.recovery_probability


def _simulate(
    eligible: list[tuple[dict, dict]],
    *,
    family: frozenset[str],
    strategy: str,
) -> SimulatedBaseline:
    """Score one naive strategy over the eligible events.

    Args:
        eligible: (event document, latest diagnosis document) pairs.
        family: the interventions this strategy is allowed to draw a probability from.
    """
    gross = 0.0
    with_probability = 0
    for event, diagnosis in eligible:
        best = _best_probability(event["surface"], diagnosis["root_cause"], family)
        if best is None:
            continue
        with_probability += 1
        gross += event["amount"] * best[1]

    return SimulatedBaseline(
        kind="simulated",
        strategy=strategy,
        intervention_family=sorted(family),
        gross_expected_recovery=money(gross),
        events_scored=len(eligible),
        events_with_defined_probability=with_probability,
        events_scored_zero_no_defined_pairing=len(eligible) - with_probability,
        probability_source=PROBABILITY_SOURCE,
    )


def compare(snapshot: Snapshot) -> BaselineComparison:
    """Build the four-way comparison over one shared event basis."""
    events_by_id = snapshot.events_by_id()
    latest_diagnoses = latest_per_event(snapshot.diagnoses)

    # The shared basis. An undiagnosed event is excluded from every strategy
    # including Vasooli's: with no root cause there is nothing for any of them to
    # apply, and scoring it under one but not another would break the comparison.
    diagnosed = {
        event_id: diagnosis
        for event_id, diagnosis in latest_diagnoses.items()
        if event_id in events_by_id
    }
    eligible = [
        (events_by_id[event_id], diagnosis)
        for event_id, diagnosis in sorted(diagnosed.items())
        if is_eligible(diagnosis)
    ]
    eligible_ids = {event["event_id"] for event, _ in eligible}

    basis = EventBasis(
        total_events=len(snapshot.events),
        events_with_diagnosis=len(diagnosed),
        eligible_events=len(eligible),
        excluded_non_recoverable=len(diagnosed) - len(eligible),
        excluded_undiagnosed=len(snapshot.events) - len(diagnosed),
        eligible_revenue_at_risk=money(sum(event["amount"] for event, _ in eligible)),
    )

    baseline_a = _simulate(
        eligible,
        family=RETRY_INTERVENTIONS,
        strategy=(
            "Retry everything once: apply a single retry to every eligible event, "
            "ignoring what actually went wrong."
        ),
    )
    baseline_b = _simulate(
        eligible,
        family=REMINDER_INTERVENTIONS,
        strategy=(
            "Generic reminder to everything: send one reminder to every eligible "
            "event, ignoring surface and root cause."
        ),
    )

    # Vasooli's own expected value, on the same basis and the same one-action-per-
    # event shape: the LATEST decision per eligible event. Counting every version
    # would give Vasooli more attempts than either baseline gets.
    latest_decisions = latest_per_event(snapshot.decisions)
    counted = [
        decision
        for event_id, decision in sorted(latest_decisions.items())
        if event_id in eligible_ids
    ]
    net = sum(decision["expected_recovery_value"] for decision in counted)
    gross = sum(
        decision["revenue_at_risk"] * decision["recovery_probability"]
        for decision in counted
    )
    cost = sum(decision["estimated_cost"] for decision in counted)

    expected = VasooliExpected(
        kind="simulated",
        strategy=(
            "The real decisions the pipeline actually made: one intervention per "
            "event, chosen by root cause and surface from the same matrix, then "
            "scored by the same arithmetic."
        ),
        expected_recovery_value_net=money(net),
        gross_expected_recovery=money(gross),
        total_intervention_cost=money(cost),
        decisions_counted=len(counted),
        no_action_decisions=sum(
            1
            for decision in counted
            if decision["recommended_intervention"] in NO_ACTION_INTERVENTIONS
        ),
    )

    survivors, _ = distinct_recoveries(snapshot.verifications)
    # The same partition `summarize` applies, from the same helper, so the two
    # endpoints cannot disagree about which recoveries a gateway attests to.
    gateway_records, asserted_records = split_by_source(survivors)
    gateway_recovered = money(
        sum(document["amount_recovered"] for document in gateway_records)
    )
    asserted_recovered = money(
        sum(document["amount_recovered"] for document in asserted_records)
    )
    actual = VasooliActual(
        kind="real",
        revenue_recovered=money(gateway_recovered + asserted_recovered),
        revenue_recovered_gateway_verified=gateway_recovered,
        revenue_recovered_manually_asserted=asserted_recovered,
        events_recovered=len({document["event_id"] for document in survivors.values()}),
        executions_verified_recovered=len(survivors),
        caveat=ACTUAL_CAVEAT,
    )

    return BaselineComparison(
        methodology=METHODOLOGY,
        event_basis=basis,
        baseline_retry_everything=baseline_a,
        baseline_generic_reminder=baseline_b,
        vasooli_expected=expected,
        vasooli_actual=actual,
        computed_at=snapshot.read_at,
    )
