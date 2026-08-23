"""Stage 3 full audit — re-derive every stored decision from scratch and compare.

This does not trust the stored numbers. For each decision in the database it
re-reads the referenced diagnosis and the event, re-runs the pure engine on them,
and compares the result field by field. A decision that was correct when written
but whose basis no longer supports it will show up here.

Checks per decision:
  * intervention is inside the fixed catalogue
  * ERV equals revenue_at_risk x probability - cost
  * revenue_at_risk equals the linked event's amount
  * cost and probability match the matrix entry for the chosen intervention
  * the whole decision matches what `decide()` produces from the same inputs
  * the referenced diagnosis exists, belongs to this event, and is that version
  * no_action variants carry no cost and no probability
  * (event_id, version) is unique; decided_at is timezone-aware

Then a sweep of all 24 (surface, root_cause) cells at four amounts, and a
re-diagnose/re-decide round trip proving version pinning holds.

Run:  python scripts/s3_audit.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bson import ObjectId

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision import INTERVENTION_MATRIX, cost_of, decide, evaluate
from app.diagnosis import diagnose
from app.diagnosis import append as append_diagnosis
from app.decision import append as append_decision
from app.models import (
    ALLOWED_INTERVENTIONS,
    CONFIDENCE_FLOOR,
    NO_ACTION_INTERVENTIONS,
    Decision,
    DecisionRecord,
    DiagnosisRecord,
    RevenueEvent,
    expected_recovery_value,
)

problems: list[str] = []
notes: list[str] = []


def fail(message: str) -> None:
    problems.append(message)


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


async def audit_stored_decisions() -> None:
    database = get_database()

    decisions = await database["decisions"].find().to_list(length=None)
    events = {
        document["event_id"]: document
        for document in await database["events"].find().to_list(length=None)
    }
    diagnoses = {
        document["_id"]: document
        for document in await database["diagnoses"].find().to_list(length=None)
    }

    latest_diagnosis_version: dict[str, int] = defaultdict(int)
    for document in diagnoses.values():
        event_id = document["event_id"]
        latest_diagnosis_version[event_id] = max(
            latest_diagnosis_version[event_id], int(document["version"])
        )

    latest_decision_version: dict[str, int] = defaultdict(int)
    for document in decisions:
        event_id = document["event_id"]
        latest_decision_version[event_id] = max(
            latest_decision_version[event_id], int(document["version"])
        )

    section(f"1. Re-deriving all {len(decisions)} stored decisions")

    seen_versions: set[tuple[str, int]] = set()
    interventions: Counter[str] = Counter()
    causes: Counter[tuple[str, str]] = Counter()
    stale_but_historical = 0
    checked = 0

    for document in sorted(decisions, key=lambda d: (d["event_id"], d["version"])):
        event_id = document["event_id"]
        label = f"{event_id} v{document['version']}"

        # The stored document must still satisfy the model it was written through.
        try:
            record = DecisionRecord.from_document(document)
        except Exception as exc:  # noqa: BLE001 - any failure is an audit finding
            fail(f"{label}: stored document no longer validates: {exc}")
            continue

        interventions[record.recommended_intervention] += 1

        key = (event_id, record.version)
        if key in seen_versions:
            fail(f"{label}: duplicate (event_id, version) — unique index breached")
        seen_versions.add(key)

        if record.recommended_intervention not in ALLOWED_INTERVENTIONS:
            fail(f"{label}: intervention {record.recommended_intervention!r} outside catalogue")

        recomputed = expected_recovery_value(
            record.revenue_at_risk, record.recovery_probability, record.estimated_cost
        )
        if abs(recomputed - record.expected_recovery_value) > 0.01:
            fail(
                f"{label}: ERV {record.expected_recovery_value} != recomputed {recomputed}"
            )

        if record.recommended_intervention in NO_ACTION_INTERVENTIONS and (
            record.estimated_cost or record.recovery_probability
        ):
            fail(
                f"{label}: {record.recommended_intervention} carries "
                f"cost={record.estimated_cost} p={record.recovery_probability}"
            )

        if record.decided_at.tzinfo is None:
            fail(f"{label}: decided_at is naive, not timezone-aware")

        # Referential integrity, re-checked against the collection rather than
        # trusting the write-time guard.
        diagnosis_document = diagnoses.get(ObjectId(record.diagnosis_id))
        if diagnosis_document is None:
            fail(f"{label}: diagnosis {record.diagnosis_id} does not exist")
            continue
        if diagnosis_document["event_id"] != event_id:
            fail(
                f"{label}: diagnosis belongs to {diagnosis_document['event_id']!r}"
            )
            continue
        if int(diagnosis_document["version"]) != record.diagnosis_version:
            fail(
                f"{label}: claims diagnosis v{record.diagnosis_version} but that "
                f"document is v{diagnosis_document['version']}"
            )
            continue

        # A superseded decision pointing at a superseded diagnosis is correct by
        # design — that is what append-only history is for. Only the *current*
        # recommendation is required to rest on the current explanation.
        is_current = record.version == latest_decision_version[event_id]
        if record.diagnosis_version != latest_diagnosis_version[event_id]:
            if is_current:
                fail(
                    f"{label}: current decision rests on diagnosis "
                    f"v{record.diagnosis_version} but v"
                    f"{latest_diagnosis_version[event_id]} exists"
                )
            else:
                stale_but_historical += 1

        event_document = events.get(event_id)
        if event_document is None:
            fail(f"{label}: no event {event_id!r} exists")
            continue
        if abs(float(event_document["amount"]) - record.revenue_at_risk) > 0.01:
            fail(
                f"{label}: revenue_at_risk {record.revenue_at_risk} != event amount "
                f"{event_document['amount']}"
            )

        causes[(diagnosis_document["surface"], diagnosis_document["root_cause"])] += 1

        # The cost and probability must match the matrix, not merely be internally
        # consistent: a tampered pair that happens to satisfy the ERV formula would
        # pass every check above and only fail here.
        if record.recommended_intervention not in NO_ACTION_INTERVENTIONS:
            matrix_cost = cost_of(record.recommended_intervention)
            if abs(matrix_cost - record.estimated_cost) > 0.01:
                fail(
                    f"{label}: cost {record.estimated_cost} != matrix cost {matrix_cost}"
                )
            matched = [
                candidate
                for candidate, _, _ in evaluate(
                    diagnosis_document["surface"],
                    diagnosis_document["root_cause"],
                    record.revenue_at_risk,
                )
                if candidate.intervention == record.recommended_intervention
            ]
            if not matched:
                fail(
                    f"{label}: {record.recommended_intervention} is not a permitted "
                    f"candidate for {diagnosis_document['surface']}/"
                    f"{diagnosis_document['root_cause']}"
                )
            elif abs(matched[0].recovery_probability - record.recovery_probability) > 1e-9:
                fail(
                    f"{label}: probability {record.recovery_probability} != matrix "
                    f"{matched[0].recovery_probability}"
                )

        # Full re-derivation: run the engine on the same diagnosis and event.
        event = RevenueEvent.model_validate(
            {k: v for k, v in event_document.items() if k != "_id"}
        )
        diagnosis = DiagnosisRecord.from_document(diagnosis_document)
        rederived = decide(diagnosis=diagnosis, event=event)

        for field in (
            "recommended_intervention",
            "estimated_cost",
            "recovery_probability",
            "revenue_at_risk",
            "expected_recovery_value",
            "reasoning",
        ):
            stored_value = getattr(record, field)
            fresh_value = getattr(rederived, field)
            if isinstance(stored_value, float):
                if abs(stored_value - fresh_value) > 0.01:
                    fail(f"{label}: {field} stored {stored_value} != re-derived {fresh_value}")
            elif stored_value != fresh_value:
                fail(
                    f"{label}: {field} differs from re-derivation\n"
                    f"      stored:     {stored_value}\n"
                    f"      re-derived: {fresh_value}"
                )

        checked += 1

    print(f"  fully re-derived and matched: {checked}/{len(decisions)}")
    print(f"  superseded decisions resting on superseded diagnoses (by design): "
          f"{stale_but_historical}")
    print(f"  unique (event_id, version) pairs: {len(seen_versions)}")

    print(f"\n  interventions used ({len(interventions)}/{len(ALLOWED_INTERVENTIONS)} "
          f"of the catalogue):")
    for name, count in interventions.most_common():
        print(f"    {name:<30} {count:>2}")
    unused = ALLOWED_INTERVENTIONS - set(interventions)
    print(f"  never recommended: {sorted(unused) or 'none — full catalogue exercised'}")

    print(f"\n  root causes decided on ({len(causes)}):")
    for (surface, root_cause), count in sorted(causes.items()):
        print(f"    {surface + '/' + root_cause:<45} {count:>2}")


def sweep_matrix() -> None:
    section(f"2. Sweeping all {len(INTERVENTION_MATRIX)} matrix cells at four amounts")

    amounts = (1.0, 150.0, 5_000.0, 250_000.0)
    print(f"  amounts: {', '.join(f'{amount:,.2f}' for amount in amounts)}")
    print(f"  {'surface/root_cause':<45} " + "  ".join(f"{a:>12,.0f}" for a in amounts))

    for surface, root_cause in sorted(INTERVENTION_MATRIX):
        winners: list[str] = []
        for amount in amounts:
            scored = evaluate(surface, root_cause, amount)
            candidate, cost, erv = scored[0]
            name = candidate.intervention
            if name in NO_ACTION_INTERVENTIONS:
                winners.append("no_action")
                continue
            if erv < 0:
                winners.append("neg->no_act")
                continue
            winners.append(name[:12])

            # Every scored option must be representable as a Decision.
            for cand, cand_cost, cand_erv in scored:
                if cand.intervention in NO_ACTION_INTERVENTIONS or cand_erv < 0:
                    continue
                try:
                    Decision(
                        event_id="sweep",
                        diagnosis_id="0" * 24,
                        diagnosis_version=1,
                        recommended_intervention=cand.intervention,
                        estimated_cost=cand_cost,
                        recovery_probability=cand.recovery_probability,
                        revenue_at_risk=amount,
                        expected_recovery_value=cand_erv,
                        reasoning="sweep",
                    )
                except Exception as exc:  # noqa: BLE001
                    fail(
                        f"sweep {surface}/{root_cause} @ {amount}: "
                        f"{cand.intervention} not representable: {exc}"
                    )

        print(f"  {surface + '/' + root_cause:<45} " + "  ".join(f"{w:>12}" for w in winners))


async def version_pinning_round_trip() -> None:
    section("3. Re-diagnose then re-decide: version pinning and append-only history")

    event_id = "dec_S3_TEMP"
    database = get_database()

    before = await database["decisions"].find({"event_id": event_id}).to_list(length=None)
    print(f"  {event_id} had {len(before)} decision(s):")
    for document in sorted(before, key=lambda d: d["version"]):
        print(
            f"    decision v{document['version']} -> diagnosis "
            f"{document['diagnosis_id']} v{document['diagnosis_version']} "
            f"= {document['recommended_intervention']}"
        )

    event_document = await database["events"].find_one({"event_id": event_id})
    event = RevenueEvent.model_validate(
        {k: v for k, v in event_document.items() if k != "_id"}
    )

    # Re-diagnose through the real service (rules path — deterministic, no LLM).
    fresh_diagnosis, method = await diagnose(event)
    new_diagnosis_id, new_version = await append_diagnosis(fresh_diagnosis, method)
    print(
        f"  re-diagnosed: {new_diagnosis_id} v{new_version} method={method} "
        f"{fresh_diagnosis.surface}/{fresh_diagnosis.root_cause} "
        f"conf={fresh_diagnosis.confidence:.2f}"
    )

    diagnosis = DiagnosisRecord(
        id=new_diagnosis_id,
        version=new_version,
        method=method,
        **fresh_diagnosis.model_dump(),
    )
    new_decision = decide(diagnosis=diagnosis, event=event)
    decision_id, decision_version = await append_decision(new_decision)
    print(
        f"  re-decided: {decision_id} v{decision_version} -> diagnosis "
        f"{new_decision.diagnosis_id} v{new_decision.diagnosis_version} "
        f"= {new_decision.recommended_intervention}"
    )

    after = await database["decisions"].find({"event_id": event_id}).to_list(length=None)
    if len(after) != len(before) + 1:
        fail(f"re-decide did not append: {len(before)} -> {len(after)}")
    else:
        print(f"  OK: decision count {len(before)} -> {len(after)} (appended, not overwritten)")

    old = [d for d in after if d["version"] < decision_version]
    if any(d["diagnosis_id"] == new_decision.diagnosis_id for d in old):
        fail("an older decision's diagnosis_id changed — history was mutated")
    else:
        print("  OK: older decisions still pin their original diagnosis ids")

    if new_decision.diagnosis_id == str(new_diagnosis_id) and new_decision.diagnosis_version == new_version:
        print(f"  OK: the new decision pins diagnosis v{new_version}, not the old one")
    else:
        fail("new decision did not pin the new diagnosis version")

    notes.append(
        f"{event_id} now has {len(after)} decisions across {new_version} diagnosis "
        "versions; the superseded pair is retained deliberately"
    )


async def main() -> None:
    await connect_to_mongo()
    print("Stage 3 full audit")
    print(f"confidence floor {CONFIDENCE_FLOOR}   catalogue {len(ALLOWED_INTERVENTIONS)}   "
          f"matrix cells {len(INTERVENTION_MATRIX)}")

    await audit_stored_decisions()
    sweep_matrix()
    await version_pinning_round_trip()

    # Re-audit after the round trip so the newly written pair is checked too.
    section("4. Re-audit after the round trip")
    await audit_stored_decisions()

    await close_mongo_connection()

    print("\n" + "=" * 78)
    for note in notes:
        print(f"note: {note}")
    if problems:
        print(f"\nAUDIT FINDINGS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("\nno ERV math errors, no out-of-catalogue interventions, no stale or "
          "mismatched diagnosis references")


if __name__ == "__main__":
    asyncio.run(main())
