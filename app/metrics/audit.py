"""Part C — one event's complete history, gathered from every stage that touched it.

Assembly, not computation. Every record is returned exactly as the stage that owns it
stored it, validated through that stage's own model so a document that has drifted
from its contract fails loudly here instead of being reshaped into something
plausible. Nothing is re-derived: no diagnosis is recomputed, no ERV is recalculated,
no verdict is re-evaluated against the current rulebook.

Three things are added, all of them renderings of stored data:

* `timeline` — the same records interleaved by their own timestamps. The full records
  are still returned in their own lists; this is a merge so the story reads in one
  pass rather than requiring the caller to sort seven lists together;
* `rulebook_fingerprints` — which rulebooks judged this event, and whether each one
  actually attests to what was in force at the time. A `backfilled` fingerprint does
  not, and an audit that presented it alongside an `evaluated` one without saying so
  would overstate what the trail proves;
* `record_counts` and `distinct_recoveries` — the shape of the trail, including how
  many verification records describe the same payment.
"""

from __future__ import annotations

from app.metrics.reader import Snapshot, distinct_recoveries, money
from app.models.decision import DecisionRecord
from app.models.diagnosis import DiagnosisRecord
from app.models.events import RevenueEventRecord
from app.models.execution import ExecutionRecordDocument
from app.models.metrics import AuditTrail, FingerprintUse, TimelineEntry
from app.models.policy import UNATTESTED_FINGERPRINT_SOURCES, PolicyVerdictRecord
from app.models.promise import PromiseToPayDocument
from app.models.verification import RECOVERED_OUTCOME, VerificationRecordDocument


class EventNotFound(LookupError):
    """No event with the requested id.

    Its own type so the route can answer 404 without also catching a genuine
    validation failure in one of the stored records, which is a 500.
    """


def _money_str(value: float) -> str:
    """Render an amount for a one-line summary."""
    return f"{value:,.2f}"


def assemble(snapshot: Snapshot, event_id: str) -> AuditTrail:
    """Gather every record naming `event_id` into one chronological trail.

    Expects a snapshot already scoped to this event — `load_snapshot(event_id)`.

    Raises:
        EventNotFound: no such event.
    """
    if not snapshot.events:
        raise EventNotFound(f"No event with event_id {event_id!r}.")
    if len(snapshot.events) > 1:  # pragma: no cover - unique index prevents this
        raise EventNotFound(
            f"{len(snapshot.events)} events share event_id {event_id!r}; the trail "
            "would not be a single history."
        )

    event = RevenueEventRecord.from_document(snapshot.events[0])
    diagnoses = [DiagnosisRecord.from_document(d) for d in snapshot.diagnoses]
    decisions = [DecisionRecord.from_document(d) for d in snapshot.decisions]
    verdicts = [PolicyVerdictRecord.from_document(d) for d in snapshot.verdicts]
    executions = [ExecutionRecordDocument.from_document(d) for d in snapshot.executions]
    verifications = [
        VerificationRecordDocument.from_document(d) for d in snapshot.verifications
    ]
    promises = [PromiseToPayDocument.from_document(d) for d in snapshot.promises]

    # --- the merged timeline -------------------------------------------------

    entries: list[TimelineEntry] = [
        TimelineEntry(
            at=event.created_at,
            stage="1-ingestion",
            record_id=event.id,
            summary=(
                f"{event.surface} event ingested at {_money_str(event.amount)} "
                f"{event.currency} for customer {event.customer_ref}; "
                f"raw reason {event.raw_failure_reason!r}"
            ),
        )
    ]
    for diagnosis in diagnoses:
        entries.append(
            TimelineEntry(
                at=diagnosis.diagnosed_at,
                stage="2-diagnosis",
                record_id=diagnosis.id,
                summary=(
                    f"v{diagnosis.version} by {diagnosis.method}: "
                    f"{diagnosis.root_cause} (confidence {diagnosis.confidence}, "
                    f"recoverable={diagnosis.recoverable})"
                ),
            )
        )
    for decision in decisions:
        entries.append(
            TimelineEntry(
                at=decision.decided_at,
                stage="3-decision",
                record_id=decision.id,
                summary=(
                    f"v{decision.version} recommends {decision.recommended_intervention} "
                    f"— p={decision.recovery_probability}, cost="
                    f"{_money_str(decision.estimated_cost)}, ERV="
                    f"{_money_str(decision.expected_recovery_value)} "
                    f"(from diagnosis v{decision.diagnosis_version})"
                ),
            )
        )
    for verdict in verdicts:
        entries.append(
            TimelineEntry(
                at=verdict.evaluated_at,
                stage="4-policy",
                record_id=verdict.id,
                summary=(
                    f"v{verdict.version} {verdict.verdict.upper()} because "
                    f"{verdict.reason} — on decision v{verdict.decision_version}, "
                    f"rulebook {verdict.rulebook_fingerprint} "
                    f"({verdict.rulebook_fingerprint_source})"
                ),
            )
        )
    for execution in executions:
        artifact = (
            f"link {execution.razorpay_payment_link_id}"
            if execution.razorpay_payment_link_id
            else execution.contact_message_summary or "no artifact"
        )
        entries.append(
            TimelineEntry(
                at=execution.executed_at,
                stage="5-execution",
                record_id=execution.id,
                summary=(
                    f"{execution.intervention} -> {execution.action_type} "
                    f"{execution.status}: {artifact}"
                    + (
                        f" ({execution.failure_reason})"
                        if execution.failure_reason
                        else ""
                    )
                ),
            )
        )
    for verification in verifications:
        entries.append(
            TimelineEntry(
                at=verification.verified_at,
                stage="6-verification",
                record_id=verification.id,
                summary=(
                    f"{verification.razorpay_event} -> {verification.outcome}: "
                    f"{_money_str(verification.amount_recovered)} of "
                    f"{_money_str(verification.amount_expected)} expected"
                    + (" — AMOUNT MISMATCH" if verification.amount_mismatch else "")
                    + f" (razorpay event {verification.razorpay_event_id})"
                ),
            )
        )
    for promise in promises:
        entries.append(
            TimelineEntry(
                at=promise.created_at,
                stage="6-promise",
                record_id=promise.id,
                summary=(
                    f"promised {_money_str(promise.promised_amount)} by "
                    f"{promise.promised_date.isoformat()}; now {promise.state}"
                    + (
                        f", resolved {promise.resolved_at.isoformat()}"
                        if promise.resolved_at
                        else ""
                    )
                    + (", follow-up sent" if promise.follow_up_sent else "")
                ),
            )
        )

    # Stage breaks the tie, so records written in the same millisecond still read in
    # pipeline order rather than in whatever order the lists happened to be built.
    entries.sort(key=lambda entry: (entry.at, entry.stage))

    # --- which rulebooks judged this event -----------------------------------

    grouped: dict[tuple[str, str], list[int]] = {}
    for verdict in verdicts:
        key = (verdict.rulebook_fingerprint, verdict.rulebook_fingerprint_source)
        grouped.setdefault(key, []).append(verdict.version)
    fingerprints = [
        FingerprintUse(
            rulebook_fingerprint=fingerprint,
            source=source,
            verdict_versions=sorted(versions),
            attests_to_rulebook_in_force=source not in UNATTESTED_FINGERPRINT_SOURCES,
        )
        for (fingerprint, source), versions in sorted(grouped.items())
    ]

    # --- shape ---------------------------------------------------------------

    survivors, _ = distinct_recoveries(snapshot.verifications)
    recovered_records = [
        record for record in verifications if record.outcome == RECOVERED_OUTCOME
    ]

    return AuditTrail(
        event_id=event_id,
        event=event,
        diagnoses=diagnoses,
        decisions=decisions,
        policy_verdicts=verdicts,
        executions=executions,
        verifications=verifications,
        promises=promises,
        timeline=entries,
        rulebook_fingerprints=fingerprints,
        record_counts={
            "diagnoses": len(diagnoses),
            "decisions": len(decisions),
            "policy_verdicts": len(verdicts),
            "policy_verdicts_authorized": sum(
                1 for v in verdicts if v.verdict == "authorized"
            ),
            "executions": len(executions),
            "executions_completed": sum(
                1 for e in executions if e.status == "completed"
            ),
            "verifications": len(verifications),
            "verifications_recovered": len(recovered_records),
            "promises": len(promises),
            "timeline_entries": len(entries),
        },
        distinct_recoveries=len(survivors),
        revenue_recovered=money(
            sum(document["amount_recovered"] for document in survivors.values())
        ),
        assembled_at=snapshot.read_at,
    )
