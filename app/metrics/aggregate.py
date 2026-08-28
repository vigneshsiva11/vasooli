"""Part A — the four core recovery aggregations.

Pure functions over a `Snapshot`. Nothing here queries, so nothing here can write;
the database boundary is `app/metrics/reader.py` and this module cannot reach past
it.

Two decisions run through all four, and both are visible in the responses rather
than buried here:

1. **An event contributes once.** Diagnoses, decisions and verdicts are append-only,
   so every money total and every count of "events" is computed over the LATEST
   version per event. Counting all versions would let a re-diagnosed event appear
   under two root causes and put its amount in the at-risk column twice.
2. **A payment is counted once.** Recovered verification records are collapsed per
   execution — see `distinct_recoveries` for why there are more records than
   payments — and the number set aside is reported beside every figure that depends
   on it.
3. **Recovered money is split by how it was established.** Stage 9 added a second
   verification source: a merchant confirming payment after a contact-type
   intervention, where no Razorpay artifact exists for a webhook to report on. Both
   sources are real recovery and both are counted, but they are not equally
   evidenced, so every money and count figure that includes asserted money also
   appears with only the gateway-verified portion. Nothing here adds the two into a
   single unlabelled number.
"""

from __future__ import annotations

from app.decision.matrix import candidates_for
from app.metrics.reader import (
    Snapshot,
    distinct_recoveries,
    latest_per_event,
    money,
    percentage,
    recovered_amount_per_event,
)
from app.models.decision import ALLOWED_INTERVENTIONS, NO_ACTION_INTERVENTIONS
from app.models.diagnosis import NON_RECOVERABLE_ROOT_CAUSES
from app.models.events import ALLOWED_EVENT_STATUSES
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    CONTACT_ACTION_TYPES,
    LINK_ACTION_TYPES,
)
from app.models.metrics import (
    InterventionMetrics,
    MetricsSummary,
    PromiseMetrics,
    RootCauseMetrics,
)
from app.models.promise import OPEN_PROMISE_STATE, REQUIRES_FOLLOW_UP_SENT
from app.models.verification import MANUAL_SOURCE, source_of

SUMMARY_METHODOLOGY = (
    "total_revenue_at_risk is a plain sum over every stored event, with nothing "
    "excluded: every event in this dataset is a synthetic fixture, so any partial "
    "exclusion of 'test' events would be an arbitrary line through uniformly "
    "synthetic data. non_recoverable_at_risk reports the portion the system "
    "deliberately declined to chase, so the rate can be read either way. "
    "total_revenue_recovered counts each recovered payment once, per execution: "
    "recovered_verification_records is the raw count of verification records and is "
    "higher, because one payment link can be reported by several distinct Razorpay "
    "events, and summing those would report money that never existed. "
    "total_revenue_recovered spans BOTH verification sources and is the exact sum of "
    "gateway_verified_recovered (Razorpay's signed word about a payment link) and "
    "manually_asserted_recovered (a merchant's confirmation after a contact-type "
    "intervention, which produces no gateway artifact for a webhook to report on). "
    "Both are real recovery; only the first is attested by a third party, so "
    "recovery_rate_gateway_verified is reported alongside recovery_rate and is the "
    "conservative figure to quote. Verification records written before Stage 9 carry "
    "no source field, are all webhook records, and count as gateway-verified. All "
    "figures are computed live from the collections on every request; nothing is "
    "cached."
)

PROMISE_METHODOLOGY = (
    "honor_rate excludes still-open promises, which have not resolved either way. "
    "Note that 'reevaluating' is counted as still-open per that definition even "
    "though such a promise already missed its date and was chased — so this is the "
    "optimistic reading. The pessimistic one, (honored)/(honored+broken+"
    "reevaluating), is computable from the fields below."
)


# ---------------------------------------------------------------------------
# 1. GET /metrics/summary
# ---------------------------------------------------------------------------


def _is_manual(document: dict) -> bool:
    """Whether a stored verification rests on an assertion rather than a webhook.

    Reads `source_of` rather than `document["source"]` so the 42 records written
    before Stage 9 — which have no `source` field at all — are classified the same
    way the model layer classifies them when it parses them. Two definitions of
    "which source is this" would eventually disagree, and the figure that would go
    wrong is the one separating verified money from asserted money.
    """
    return source_of(document) == MANUAL_SOURCE


def split_by_source(
    survivors: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Partition deduplicated recoveries into (gateway-verified, manually asserted).

    A partition, not two filters: every record lands in exactly one list, so the two
    always sum back to the whole and no figure derived from them can quietly omit a
    record whose source is something neither branch recognised.

    Public because `app/metrics/baseline.py` applies the same split to the same
    records. One definition, shared, so `/metrics/summary` and
    `/metrics/baseline-comparison` cannot disagree about which recoveries a gateway
    attests to.
    """
    gateway: list[dict] = []
    asserted: list[dict] = []
    for document in survivors.values():
        (asserted if _is_manual(document) else gateway).append(document)
    return gateway, asserted


def summarize(snapshot: Snapshot) -> MetricsSummary:
    """Compute the headline recovery numbers."""
    at_risk = money(sum(event["amount"] for event in snapshot.events))

    survivors, duplicates_ignored = distinct_recoveries(snapshot.verifications)
    gateway_records, asserted_records = split_by_source(survivors)
    gateway_recovered = money(
        sum(document["amount_recovered"] for document in gateway_records)
    )
    asserted_recovered = money(
        sum(document["amount_recovered"] for document in asserted_records)
    )
    # Summed from the two reported parts rather than over `survivors` again, so the
    # total a dashboard prints is exactly the sum of the two figures printed beside
    # it. Rounding twice on separate subsets and once on the whole can differ by a
    # paisa, and a total that does not equal its own split invites the reader to
    # assume one of the three numbers is measuring something else.
    recovered = money(gateway_recovered + asserted_recovered)

    # Every declared status is present, including the ones at zero: a dashboard
    # should not have to tell "no events in this state" apart from "this state was
    # not in the response".
    counts = {status: 0 for status in sorted(ALLOWED_EVENT_STATUSES)}
    for event in snapshot.events:
        status = event.get("status")
        if status in counts:
            counts[status] += 1
        else:  # pragma: no cover - the model's Literal prevents this
            counts[str(status)] = counts.get(str(status), 0) + 1

    decided = {document["event_id"] for document in snapshot.decisions}
    events_by_id = snapshot.events_by_id()

    # Non-recoverable money, from the diagnosis's own `recoverable` flag rather than
    # from a list maintained here — the flag is Stage 2's stored judgement, and
    # re-deriving it would be this stage forming an opinion about which causes are
    # worth chasing.
    latest_diagnoses = latest_per_event(snapshot.diagnoses)
    non_recoverable = money(
        sum(
            events_by_id[event_id]["amount"]
            for event_id, diagnosis in latest_diagnoses.items()
            if event_id in events_by_id and not diagnosis.get("recoverable", True)
        )
    )

    return MetricsSummary(
        total_revenue_at_risk=at_risk,
        total_revenue_recovered=recovered,
        recovery_rate=percentage(recovered, at_risk),
        events_by_status=counts,
        total_events_processed=len(decided & set(events_by_id)),
        total_events=len(snapshot.events),
        events_without_decision=len(set(events_by_id) - decided),
        currencies=sorted({event["currency"] for event in snapshot.events}),
        non_recoverable_at_risk=non_recoverable,
        recovered_verification_records=len(survivors) + duplicates_ignored,
        distinct_recoveries_counted=len(survivors),
        duplicate_verification_records_ignored=duplicates_ignored,
        gateway_verified_recovered=gateway_recovered,
        manually_asserted_recovered=asserted_recovered,
        recovery_rate_gateway_verified=percentage(gateway_recovered, at_risk),
        distinct_recoveries_gateway_verified=len(gateway_records),
        distinct_recoveries_manually_asserted=len(asserted_records),
        methodology=SUMMARY_METHODOLOGY,
        computed_at=snapshot.read_at,
    )


# ---------------------------------------------------------------------------
# 2. GET /metrics/by-root-cause
# ---------------------------------------------------------------------------


def by_root_cause(snapshot: Snapshot) -> list[RootCauseMetrics]:
    """Per root cause: how many events, how much money, how much came back.

    Grouped by root cause across surfaces, as asked. Several causes are shared —
    `card_expired` and `insufficient_funds` occur on payment and subscription both —
    so each row names the surfaces that contributed to it.

    The key set is every cause appearing in ANY diagnosis, but events and money are
    attributed from the LATEST diagnosis per event. A cause that only survives in a
    superseded version therefore appears with zero events, flagged, rather than
    vanishing: it happened, and a table that hid it would misrepresent the history.
    """
    events_by_id = snapshot.events_by_id()
    recovered_per_event = recovered_amount_per_event(snapshot.verifications)
    latest = latest_per_event(snapshot.diagnoses)

    ever_seen = {document["root_cause"] for document in snapshot.diagnoses}
    currently_named: dict[str, list[str]] = {}
    for event_id, diagnosis in latest.items():
        if event_id not in events_by_id:
            continue
        currently_named.setdefault(diagnosis["root_cause"], []).append(event_id)

    rows: list[RootCauseMetrics] = []
    for root_cause in ever_seen:
        event_ids = currently_named.get(root_cause, [])
        at_risk = money(sum(events_by_id[event_id]["amount"] for event_id in event_ids))
        recovered = money(
            sum(recovered_per_event.get(event_id, 0.0) for event_id in event_ids)
        )
        surfaces = sorted(
            {events_by_id[event_id]["surface"] for event_id in event_ids}
        ) or sorted(
            {
                document["surface"]
                for document in snapshot.diagnoses
                if document["root_cause"] == root_cause
            }
        )
        rows.append(
            RootCauseMetrics(
                root_cause=root_cause,
                surfaces=surfaces,
                events=len(event_ids),
                revenue_at_risk=at_risk,
                revenue_recovered=recovered,
                recovery_rate=percentage(recovered, at_risk),
                superseded_only=not event_ids,
            )
        )

    # Sorted by revenue_at_risk descending, as asked. Root cause breaks the tie so
    # the ordering is stable across requests rather than dependent on set iteration.
    rows.sort(key=lambda row: (-row.revenue_at_risk, row.root_cause))
    return rows


# ---------------------------------------------------------------------------
# 3. GET /metrics/by-intervention
# ---------------------------------------------------------------------------


def by_intervention(snapshot: Snapshot) -> list[InterventionMetrics]:
    """Per intervention: recommended, authorized, executed, and whether it worked.

    `times_authorized` needs a join. A `PolicyVerdict` records `decision_id` and has
    no intervention field of its own — deliberately, since Stage 4 grants permission
    for one specific recommendation — so the intervention is resolved through the
    decision the verdict names.

    Every count here is over ALL versions, not the latest: `times_recommended` is
    how many times this was the recommendation, and a re-decision genuinely is a
    second occasion. That is why these counts do not sum to the event totals in
    `summarize`, which are deliberately per-event.

    `recovery_rate` counts recoveries from both verification sources.
    `recovery_rate_gateway_verified` counts only the webhook-attested ones, and is
    the figure to read when the question is what a third party confirms. On a
    contact-type row the two now differ, which is exactly what Stage 9 changed: such
    a row used to be able to report nothing but zero.
    """
    decisions_by_oid = snapshot.decisions_by_object_id()
    executions_by_oid = snapshot.executions_by_object_id()
    survivors, _ = distinct_recoveries(snapshot.verifications)

    recommended: dict[str, int] = {}
    for document in snapshot.decisions:
        name = document["recommended_intervention"]
        recommended[name] = recommended.get(name, 0) + 1

    authorized: dict[str, int] = {}
    for verdict in snapshot.verdicts:
        if verdict.get("verdict") != "authorized":
            continue
        decision = decisions_by_oid.get(verdict.get("decision_id", ""))
        if decision is None:
            # A verdict naming a decision that is not in the snapshot. Skipped
            # rather than guessed at, and reported by the verification harness —
            # silently attributing it to some intervention would be inventing data.
            continue
        name = decision["recommended_intervention"]
        authorized[name] = authorized.get(name, 0) + 1

    executed: dict[str, int] = {}
    failed: dict[str, int] = {}
    for execution in snapshot.executions:
        name = execution["intervention"]
        bucket = executed if execution.get("status") == "completed" else failed
        bucket[name] = bucket.get(name, 0) + 1

    recoveries: dict[str, int] = {}
    recovered_money: dict[str, float] = {}
    gateway_recoveries: dict[str, int] = {}
    gateway_money: dict[str, float] = {}
    asserted_recoveries: dict[str, int] = {}
    asserted_money: dict[str, float] = {}
    for verification in survivors.values():
        execution = executions_by_oid.get(verification.get("execution_id", ""))
        if execution is None:  # pragma: no cover - a write-time guard prevents this
            continue
        name = execution["intervention"]
        amount = verification["amount_recovered"]
        recoveries[name] = recoveries.get(name, 0) + 1
        recovered_money[name] = recovered_money.get(name, 0.0) + amount
        # The same partition `summarize` uses, applied per intervention. Counted here
        # rather than re-derived from the totals so a row's split cannot disagree with
        # the headline's.
        if _is_manual(verification):
            asserted_recoveries[name] = asserted_recoveries.get(name, 0) + 1
            asserted_money[name] = asserted_money.get(name, 0.0) + amount
        else:
            gateway_recoveries[name] = gateway_recoveries.get(name, 0) + 1
            gateway_money[name] = gateway_money.get(name, 0.0) + amount

    rows: list[InterventionMetrics] = []
    for name in sorted(set(recommended) | set(authorized) | set(executed) | set(failed)):
        action_type = ACTION_FOR_INTERVENTION.get(name)
        completed = executed.get(name, 0)
        rows.append(
            InterventionMetrics(
                intervention=name,
                times_recommended=recommended.get(name, 0),
                times_authorized=authorized.get(name, 0),
                times_executed=completed,
                recovery_rate=percentage(recoveries.get(name, 0), completed),
                times_execution_failed=failed.get(name, 0),
                recoveries=recoveries.get(name, 0),
                revenue_recovered=money(recovered_money.get(name, 0.0)),
                action_type=action_type,
                # A logged contact creates no Razorpay artifact, so no webhook can
                # ever report on it. Its GATEWAY rate is therefore structurally zero,
                # which is what this flag says and has always said. Since Stage 9 it
                # is no longer the whole story — `manually_confirmable` names the
                # channel such an intervention does have — so the two are reported
                # together and neither is left to be misread as ineffectiveness.
                verifiable=action_type in LINK_ACTION_TYPES,
                manually_confirmable=action_type in CONTACT_ACTION_TYPES,
                recoveries_gateway_verified=gateway_recoveries.get(name, 0),
                recoveries_manually_asserted=asserted_recoveries.get(name, 0),
                revenue_recovered_gateway_verified=money(gateway_money.get(name, 0.0)),
                revenue_recovered_manually_asserted=money(asserted_money.get(name, 0.0)),
                recovery_rate_gateway_verified=percentage(
                    gateway_recoveries.get(name, 0), completed
                ),
            )
        )

    rows.sort(key=lambda row: (-row.times_recommended, row.intervention))
    return rows


# ---------------------------------------------------------------------------
# 4. GET /metrics/promise-to-pay
# ---------------------------------------------------------------------------


def promise_metrics(snapshot: Snapshot) -> PromiseMetrics:
    """Promise outcomes, and the honor rate over resolved promises only.

    Every promise document counts, not the latest per event: a customer who broke
    one commitment and made another made two commitments, and collapsing them would
    erase the broken one from the denominator.
    """
    states: dict[str, int] = {}
    for promise in snapshot.promises:
        state = promise.get("state", "")
        states[state] = states.get(state, 0) + 1

    honored = states.get("honored", 0)
    broken = states.get("broken", 0)
    open_now = states.get(OPEN_PROMISE_STATE, 0)
    chased = sum(states.get(state, 0) for state in sorted(REQUIRES_FOLLOW_UP_SENT))

    return PromiseMetrics(
        total_promises=len(snapshot.promises),
        honored=honored,
        broken=broken,
        still_open=open_now + chased,
        honor_rate=percentage(honored, honored + broken),
        promised=open_now,
        reevaluating=chased,
        methodology=PROMISE_METHODOLOGY,
    )


# ---------------------------------------------------------------------------
# Shared by Part B, kept here because Part A's eligibility rule is the same one.
# ---------------------------------------------------------------------------


def is_eligible(diagnosis: dict) -> bool:
    """Whether an event may be acted on at all.

    The Stage 3 hard gate, read from the diagnosis's stored `recoverable` flag with
    the root-cause set as a fallback for any document written before that field
    existed. Fraud and disputes are out; everything else is in.
    """
    recoverable = diagnosis.get("recoverable")
    if recoverable is not None:
        return bool(recoverable)
    return diagnosis.get("root_cause") not in NON_RECOVERABLE_ROOT_CAUSES


def has_matrix_entry(surface: str, root_cause: str) -> bool:
    """Whether the Stage 3 matrix defines any candidate for this pair."""
    try:
        candidates_for(surface, root_cause)
    except KeyError:
        return False
    return True


assert NO_ACTION_INTERVENTIONS <= ALLOWED_INTERVENTIONS
