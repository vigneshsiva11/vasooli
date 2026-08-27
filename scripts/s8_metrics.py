"""Checkpoint 7 — the metrics surface at 305-event scale.

`scripts/s7_independent_check.py` already covers `/metrics/summary`,
`/metrics/promise-to-pay` and `/audit-trail/{event_id}`. This script covers the
three endpoints it does not touch — `/metrics/by-root-cause`,
`/metrics/by-intervention`, `/metrics/baseline-comparison` — and then the
reconciliations that live BETWEEN endpoints, which neither script can do alone:
the same recovered rupees are reported by four different endpoints computed by four
different code paths, and if they disagree, at most one of them is right.

INDEPENDENCE, AND EXACTLY WHERE IT STOPS
----------------------------------------
Nothing here imports `app.metrics`. Collection names are hardcoded literals, the
intervention/action-type map is a hardcoded literal, and every figure is recomputed
from raw pymongo documents with local arithmetic.

There is ONE deliberate exception, and it is disclosed in the output rather than
buried here: the two simulated baselines score events against probabilities from
`app/decision/matrix.py`, so this script imports `candidates_for` from that module.
That makes the baseline check a check of `app/metrics/baseline.py`'s ARITHMETIC AND
ELIGIBILITY RULE, not of the probability table — the table is a shared input to both
sides of the comparison. `vasooli_expected` and `vasooli_actual` need no such import
and are fully independent, because they read figures the pipeline already stored.

A mismatch is reported as a mismatch. There is no code here for deciding that a
difference is acceptable.

Usage:
    .venv/Scripts/python.exe scripts/s8_metrics.py [--base URL]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import httpx
from pymongo import MongoClient

# The one import that is not raw. See the module docstring.
from app.decision.matrix import candidates_for

# ---------------------------------------------------------------------------
# Hardcoded on purpose, exactly as in s7_independent_check.py: if a name is
# wrong this script reads an empty collection and fails loudly, instead of
# agreeing with the app because it asked the app where to look.
# ---------------------------------------------------------------------------
EVENTS = "events"
DIAGNOSES = "diagnoses"
DECISIONS = "decisions"
VERDICTS = "policy_verdicts"
EXECUTIONS = "executions"
VERIFICATIONS = "verifications"
PROMISES = "promises"

#: Restated as a literal rather than imported from `app/models/execution.py`.
ACTION_FOR_INTERVENTION: dict[str, str] = {
    "payment_method_update_link": "payment_link_generated",
    "recovery_payment_link": "payment_link_generated",
    "immediate_retry": "retry_simulated",
    "delayed_retry": "retry_simulated",
    "reminder": "contact_logged",
    "escalating_reminder_sequence": "contact_logged",
    "manual_escalation": "contact_logged",
}
LINK_ACTION_TYPES = frozenset({"payment_link_generated", "retry_simulated"})
NO_ACTION_INTERVENTIONS = frozenset(
    {"no_action", "no_action_low_confidence", "no_action_negative_erv"}
)
NON_RECOVERABLE_ROOT_CAUSES = frozenset({"suspected_fraud", "payment_dispute"})

RETRY_FAMILY = frozenset({"immediate_retry", "delayed_retry"})
REMINDER_FAMILY = frozenset({"reminder", "escalating_reminder_sequence"})

PASSED = 0
FAILED = 0


def check(label: str, mine, theirs, *, detail: str = "") -> bool:
    """Compare one independently-computed value against the endpoint's value."""
    global PASSED, FAILED
    ok = mine == theirs
    if ok:
        PASSED += 1
        print(f"  [PASS] {label}")
        print(f"         independent={mine!r}  endpoint={theirs!r}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}")
        print(f"         independent={mine!r}")
        print(f"         endpoint   ={theirs!r}")
    if detail:
        print(f"         {detail}")
    return ok


def assert_true(label: str, condition: bool, detail: str = "") -> bool:
    """A pass/fail that is not a two-sided comparison."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def note(text: str) -> None:
    print(f"  .. {text}")


def read_env(path: Path) -> dict[str, str]:
    """Parse `.env` without importing the app's settings object. Never printed."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def money(value: float) -> float:
    """Round a money total to paise, once, at the end of a summation."""
    return round(value, 2)


def percentage(numerator: float, denominator: float) -> float | None:
    """Percentage, or None when the denominator is zero. None, never 0.0."""
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def latest_per_event(documents: list[dict], *, key: str = "version") -> dict[str, dict]:
    """Highest-`key` document per `event_id`. Versions are unique per event."""
    latest: dict[str, dict] = {}
    for document in documents:
        event_id = document.get("event_id")
        if event_id is None:
            continue
        current = latest.get(event_id)
        if current is None or document.get(key, 0) >= current.get(key, 0):
            latest[event_id] = document
    return latest


def survivors_of(verifications: list[dict]) -> tuple[dict[str, dict], int]:
    """Collapse recovered verifications to the latest one per execution."""
    recovered = [d for d in verifications if d.get("outcome") == "recovered"]
    survivors: dict[str, dict] = {}
    for document in recovered:
        execution_id = document.get("execution_id")
        if execution_id is None:
            continue
        current = survivors.get(execution_id)
        if current is None or document["verified_at"] >= current["verified_at"]:
            survivors[execution_id] = document
    return survivors, len(recovered) - len(survivors)


def best_probability(surface: str, root_cause: str, family: frozenset[str]):
    """Highest matrix probability for this pair within `family`, or None."""
    try:
        candidates = candidates_for(surface, root_cause)
    except KeyError:
        return None
    eligible = [
        candidate
        for candidate in candidates
        if candidate.intervention in family
        and candidate.intervention not in NO_ACTION_INTERVENTIONS
    ]
    if not eligible:
        return None
    return max(candidate.recovery_probability for candidate in eligible)


def is_eligible(diagnosis: dict) -> bool:
    """The Stage 3 hard gate, from the stored flag with the cause set as fallback."""
    recoverable = diagnosis.get("recoverable")
    if recoverable is not None:
        return bool(recoverable)
    return diagnosis.get("root_cause") not in NON_RECOVERABLE_ROOT_CAUSES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    env = read_env(root / ".env")
    uri = env.get("MONGODB_URI") or os.environ["MONGODB_URI"]
    db_name = env.get("MONGODB_DB_NAME", "vasooli")

    client = MongoClient(uri, serverSelectionTimeoutMS=20000)
    db = client[db_name]
    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=120.0)

    # =====================================================================
    heading("0. THE SCALE THIS IS BEING RUN AT")
    # =====================================================================
    events = list(db[EVENTS].find({}))
    diagnoses = list(db[DIAGNOSES].find({}))
    decisions = list(db[DECISIONS].find({}))
    verdicts = list(db[VERDICTS].find({}))
    executions = list(db[EXECUTIONS].find({}))
    verifications = list(db[VERIFICATIONS].find({}))
    promises = list(db[PROMISES].find({}))

    print(f"  database: {db_name}   server: {args.base}")
    print()
    print(f"  {'collection':<16} {'documents':>10}")
    for name, docs in (
        (EVENTS, events),
        (DIAGNOSES, diagnoses),
        (DECISIONS, decisions),
        (VERDICTS, verdicts),
        (EXECUTIONS, executions),
        (VERIFICATIONS, verifications),
        (PROMISES, promises),
    ):
        print(f"  {name:<16} {len(docs):>10}")

    demo = [e for e in events if e["event_id"].startswith("demo_")]
    fixture = [e for e in events if not e["event_id"].startswith("demo_")]
    print()
    print(f"  events: {len(events)} = {len(demo)} Stage-8 demo + {len(fixture)} earlier fixture")
    assert_true(
        "the dataset is at the full 305-event scale this checkpoint is about",
        len(events) == 305 and len(demo) == 200 and len(fixture) == 105,
        f"{len(events)} events, {len(demo)} demo, {len(fixture)} fixture",
    )

    events_by_id = {e["event_id"]: e for e in events}
    decisions_by_oid = {str(d["_id"]): d for d in decisions}
    executions_by_oid = {str(e["_id"]): e for e in executions}
    latest_diagnoses = latest_per_event(diagnoses)
    latest_decisions = latest_per_event(decisions)
    survivors, duplicates = survivors_of(verifications)

    recovered_per_event: dict[str, float] = {}
    for document in survivors.values():
        eid = document["event_id"]
        recovered_per_event[eid] = (
            recovered_per_event.get(eid, 0.0) + document["amount_recovered"]
        )

    # =====================================================================
    heading("1. GET /metrics/by-root-cause — recomputed from raw documents")
    # =====================================================================
    rows_endpoint = http.get("/metrics/by-root-cause").json()

    ever_seen = {d["root_cause"] for d in diagnoses}
    currently_named: dict[str, list[str]] = {}
    for event_id, diagnosis in latest_diagnoses.items():
        if event_id not in events_by_id:
            continue
        currently_named.setdefault(diagnosis["root_cause"], []).append(event_id)

    mine_rows = []
    for root_cause in ever_seen:
        ids = currently_named.get(root_cause, [])
        at_risk = money(sum(events_by_id[i]["amount"] for i in ids))
        recovered = money(sum(recovered_per_event.get(i, 0.0) for i in ids))
        surfaces = sorted({events_by_id[i]["surface"] for i in ids}) or sorted(
            {d["surface"] for d in diagnoses if d["root_cause"] == root_cause}
        )
        mine_rows.append(
            {
                "root_cause": root_cause,
                "surfaces": surfaces,
                "events": len(ids),
                "revenue_at_risk": at_risk,
                "revenue_recovered": recovered,
                "recovery_rate": percentage(recovered, at_risk),
                "superseded_only": not ids,
            }
        )
    mine_rows.sort(key=lambda r: (-r["revenue_at_risk"], r["root_cause"]))

    print(f"  {'root cause':<28} {'ev':>4} {'at risk':>13} {'recovered':>11} {'rate':>7}")
    for row in mine_rows:
        rate = "—" if row["recovery_rate"] is None else f"{row['recovery_rate']:.2f}%"
        print(
            f"  {row['root_cause']:<28} {row['events']:>4} "
            f"{row['revenue_at_risk']:>13,.2f} {row['revenue_recovered']:>11,.2f} "
            f"{rate:>7}"
        )
    print()

    check("number of root-cause rows", len(mine_rows), len(rows_endpoint))
    check(
        "the row ORDER (revenue_at_risk desc, root_cause as tiebreak)",
        [r["root_cause"] for r in mine_rows],
        [r["root_cause"] for r in rows_endpoint],
    )
    for field in (
        "surfaces",
        "events",
        "revenue_at_risk",
        "revenue_recovered",
        "recovery_rate",
        "superseded_only",
    ):
        check(
            f"every row's {field}",
            {r["root_cause"]: r[field] for r in mine_rows},
            {r["root_cause"]: r[field] for r in rows_endpoint},
        )

    # =====================================================================
    heading("2. GET /metrics/by-intervention — recomputed from raw documents")
    # =====================================================================
    interventions_endpoint = http.get("/metrics/by-intervention").json()

    recommended = Counter(d["recommended_intervention"] for d in decisions)

    authorized: Counter = Counter()
    orphan_verdicts = 0
    for verdict in verdicts:
        if verdict.get("verdict") != "authorized":
            continue
        decision = decisions_by_oid.get(verdict.get("decision_id", ""))
        if decision is None:
            orphan_verdicts += 1
            continue
        authorized[decision["recommended_intervention"]] += 1

    executed: Counter = Counter()
    failed: Counter = Counter()
    for execution in executions:
        bucket = executed if execution.get("status") == "completed" else failed
        bucket[execution["intervention"]] += 1

    recoveries: Counter = Counter()
    recovered_money: dict[str, float] = {}
    orphan_verifications = 0
    for verification in survivors.values():
        execution = executions_by_oid.get(verification.get("execution_id", ""))
        if execution is None:
            orphan_verifications += 1
            continue
        name = execution["intervention"]
        recoveries[name] += 1
        recovered_money[name] = (
            recovered_money.get(name, 0.0) + verification["amount_recovered"]
        )

    mine_interventions = []
    for name in sorted(set(recommended) | set(authorized) | set(executed) | set(failed)):
        action_type = ACTION_FOR_INTERVENTION.get(name)
        completed = executed.get(name, 0)
        mine_interventions.append(
            {
                "intervention": name,
                "times_recommended": recommended.get(name, 0),
                "times_authorized": authorized.get(name, 0),
                "times_executed": completed,
                "recovery_rate": percentage(recoveries.get(name, 0), completed),
                "times_execution_failed": failed.get(name, 0),
                "recoveries": recoveries.get(name, 0),
                "revenue_recovered": money(recovered_money.get(name, 0.0)),
                "action_type": action_type,
                "verifiable": action_type in LINK_ACTION_TYPES,
            }
        )
    mine_interventions.sort(key=lambda r: (-r["times_recommended"], r["intervention"]))

    print(
        f"  {'intervention':<30} {'rec':>4} {'auth':>5} {'exec':>5} {'fail':>5} "
        f"{'recov':>6} {'money':>11} {'vfy':>4}"
    )
    for row in mine_interventions:
        print(
            f"  {row['intervention']:<30} {row['times_recommended']:>4} "
            f"{row['times_authorized']:>5} {row['times_executed']:>5} "
            f"{row['times_execution_failed']:>5} {row['recoveries']:>6} "
            f"{row['revenue_recovered']:>11,.2f} "
            f"{'yes' if row['verifiable'] else 'no':>4}"
        )
    print()

    check("number of intervention rows", len(mine_interventions), len(interventions_endpoint))
    check(
        "the row ORDER (times_recommended desc, intervention as tiebreak)",
        [r["intervention"] for r in mine_interventions],
        [r["intervention"] for r in interventions_endpoint],
    )
    for field in (
        "times_recommended",
        "times_authorized",
        "times_executed",
        "times_execution_failed",
        "recoveries",
        "revenue_recovered",
        "recovery_rate",
        "action_type",
        "verifiable",
    ):
        check(
            f"every row's {field}",
            {r["intervention"]: r[field] for r in mine_interventions},
            {r["intervention"]: r[field] for r in interventions_endpoint},
        )
    assert_true(
        "no authorized verdict names a decision that is not in the database",
        orphan_verdicts == 0,
        f"{orphan_verdicts} orphaned" if orphan_verdicts else "all verdicts resolve",
    )
    assert_true(
        "no recovered verification names an execution that is not in the database",
        orphan_verifications == 0,
        f"{orphan_verifications} orphaned"
        if orphan_verifications
        else "all recoveries resolve",
    )

    # -- the one row where authorized exceeds recommended -----------------
    print()
    print("  WHY times_authorized CAN EXCEED times_recommended")
    inverted = [
        r for r in mine_interventions
        if r["times_authorized"] > r["times_recommended"]
    ]
    for row in inverted:
        name = row["intervention"]
        target_decisions = {oid for oid, d in decisions_by_oid.items()
                            if d["recommended_intervention"] == name}
        authorizing = [
            v for v in verdicts
            if v.get("verdict") == "authorized" and v.get("decision_id") in target_decisions
        ]
        per_decision = Counter(v["decision_id"] for v in authorizing)
        repeats = {oid: n for oid, n in per_decision.items() if n > 1}
        print(
            f"    {name}: {row['times_recommended']} decisions recommend it, "
            f"{row['times_authorized']} authorized verdicts point at them"
        )
        print(
            f"    {len(per_decision)} distinct decisions were authorized; "
            f"{len(repeats)} of them more than once "
            f"(extra verdicts: {sum(n - 1 for n in repeats.values())})"
        )
        assert_true(
            f"{name}: the excess is re-authorization of the SAME decisions, "
            "not verdicts for decisions that do not exist",
            len(per_decision) <= row["times_recommended"]
            and sum(per_decision.values()) == row["times_authorized"],
            f"distinct decisions authorized {len(per_decision)} <= "
            f"{row['times_recommended']} recommended, and the verdicts sum to "
            f"{sum(per_decision.values())}",
        )
    if not inverted:
        note("no row has authorized > recommended in this dataset")

    assert_true(
        "times_executed never exceeds times_authorized, on every row",
        all(
            r["times_executed"] <= r["times_authorized"] for r in mine_interventions
        ),
        "the documented invariant of this endpoint",
    )
    assert_true(
        "every contact-type intervention is flagged verifiable=false",
        all(
            r["verifiable"] is False
            for r in mine_interventions
            if r["action_type"] == "contact_logged"
        )
        and all(
            r["recoveries"] == 0
            for r in mine_interventions
            if r["action_type"] == "contact_logged"
        ),
        "their 0% recovery_rate is structural, not measured ineffectiveness",
    )

    # =====================================================================
    heading("3. GET /metrics/baseline-comparison")
    # =====================================================================
    baseline = http.get("/metrics/baseline-comparison").json()

    print("  INDEPENDENCE NOTE, stated before the numbers:")
    print("    event_basis, vasooli_expected and vasooli_actual below are recomputed")
    print("    entirely from stored documents — fully independent.")
    print("    baseline_retry_everything and baseline_generic_reminder score events")
    print("    against app/decision/matrix.py, which this script IMPORTS. So those two")
    print("    checks verify the arithmetic and the eligibility rule, NOT the")
    print("    probability table: the table is a shared input to both sides.")
    print()

    diagnosed = {
        eid: d for eid, d in latest_diagnoses.items() if eid in events_by_id
    }
    eligible = [
        (events_by_id[eid], d) for eid, d in sorted(diagnosed.items()) if is_eligible(d)
    ]
    eligible_ids = {event["event_id"] for event, _ in eligible}
    missing_flag = sum(1 for d in diagnosed.values() if d.get("recoverable") is None)
    note(
        f"{missing_flag} of {len(diagnosed)} latest diagnoses lack a `recoverable` "
        "flag and fell back to the root-cause set"
    )

    my_basis = {
        "total_events": len(events),
        "events_with_diagnosis": len(diagnosed),
        "eligible_events": len(eligible),
        "excluded_non_recoverable": len(diagnosed) - len(eligible),
        "excluded_undiagnosed": len(events) - len(diagnosed),
        "eligible_revenue_at_risk": money(sum(e["amount"] for e, _ in eligible)),
    }
    print("  event_basis")
    for field, value in my_basis.items():
        check(f"event_basis.{field}", value, baseline["event_basis"][field])

    undiagnosed = sorted(set(events_by_id) - set(diagnosed))
    note(f"the undiagnosed event(s): {undiagnosed}")

    print()
    print("  the two simulated baselines")
    for key, family in (
        ("baseline_retry_everything", RETRY_FAMILY),
        ("baseline_generic_reminder", REMINDER_FAMILY),
    ):
        gross = 0.0
        with_probability = 0
        for event, diagnosis in eligible:
            best = best_probability(event["surface"], diagnosis["root_cause"], family)
            if best is None:
                continue
            with_probability += 1
            gross += event["amount"] * best
        block = baseline[key]
        check(f"{key}.gross_expected_recovery", money(gross), block["gross_expected_recovery"])
        check(f"{key}.events_scored", len(eligible), block["events_scored"])
        check(
            f"{key}.events_with_defined_probability",
            with_probability,
            block["events_with_defined_probability"],
        )
        check(
            f"{key}.events_scored_zero_no_defined_pairing",
            len(eligible) - with_probability,
            block["events_scored_zero_no_defined_pairing"],
        )
        check(f"{key}.kind", "simulated", block["kind"])

    print()
    print("  vasooli_expected — from the decisions the pipeline actually stored")
    counted = [
        d for eid, d in sorted(latest_decisions.items()) if eid in eligible_ids
    ]
    expected_block = baseline["vasooli_expected"]
    check(
        "vasooli_expected.expected_recovery_value_net",
        money(sum(d["expected_recovery_value"] for d in counted)),
        expected_block["expected_recovery_value_net"],
    )
    check(
        "vasooli_expected.gross_expected_recovery",
        money(sum(d["revenue_at_risk"] * d["recovery_probability"] for d in counted)),
        expected_block["gross_expected_recovery"],
    )
    check(
        "vasooli_expected.total_intervention_cost",
        money(sum(d["estimated_cost"] for d in counted)),
        expected_block["total_intervention_cost"],
    )
    check(
        "vasooli_expected.decisions_counted",
        len(counted),
        expected_block["decisions_counted"],
    )
    check(
        "vasooli_expected.no_action_decisions",
        sum(
            1
            for d in counted
            if d["recommended_intervention"] in NO_ACTION_INTERVENTIONS
        ),
        expected_block["no_action_decisions"],
    )
    check("vasooli_expected.kind", "simulated", expected_block["kind"])

    print()
    print("  vasooli_actual — the only figure on this endpoint that is real money")
    actual_block = baseline["vasooli_actual"]
    check(
        "vasooli_actual.revenue_recovered",
        money(sum(d["amount_recovered"] for d in survivors.values())),
        actual_block["revenue_recovered"],
    )
    check(
        "vasooli_actual.events_recovered",
        len({d["event_id"] for d in survivors.values()}),
        actual_block["events_recovered"],
    )
    check(
        "vasooli_actual.executions_verified_recovered",
        len(survivors),
        actual_block["executions_verified_recovered"],
    )
    check("vasooli_actual.kind", "real", actual_block["kind"])
    assert_true(
        "exactly one of the four strategy blocks is labelled kind='real'",
        [
            baseline["baseline_retry_everything"]["kind"],
            baseline["baseline_generic_reminder"]["kind"],
            baseline["vasooli_expected"]["kind"],
            baseline["vasooli_actual"]["kind"],
        ].count("real") == 1,
        "the other three say 'simulated' at the type level",
    )

    # =====================================================================
    heading("4. CROSS-ENDPOINT RECONCILIATION — four code paths, one number")
    # =====================================================================
    summary = http.get("/metrics/summary").json()

    money_summary = summary["total_revenue_recovered"]
    money_by_cause = money(sum(r["revenue_recovered"] for r in rows_endpoint))
    money_by_intervention = money(
        sum(r["revenue_recovered"] for r in interventions_endpoint)
    )
    money_baseline = baseline["vasooli_actual"]["revenue_recovered"]
    money_raw = money(sum(d["amount_recovered"] for d in survivors.values()))

    print(f"  independent (raw pymongo)        {money_raw:>14,.2f}")
    print(f"  /metrics/summary                 {money_summary:>14,.2f}")
    print(f"  /metrics/by-root-cause (summed)  {money_by_cause:>14,.2f}")
    print(f"  /metrics/by-intervention (summed){money_by_intervention:>14,.2f}")
    print(f"  /metrics/baseline-comparison     {money_baseline:>14,.2f}")
    print()
    check("summary vs raw pymongo", money_raw, money_summary)
    check("by-root-cause summed vs summary", money_summary, money_by_cause)
    check("by-intervention summed vs summary", money_summary, money_by_intervention)
    check("baseline vasooli_actual vs summary", money_summary, money_baseline)

    check(
        "recoveries summed across interventions == distinct_recoveries_counted",
        sum(r["recoveries"] for r in interventions_endpoint),
        summary["distinct_recoveries_counted"],
    )
    check(
        "events summed across root causes == total_events_processed",
        sum(r["events"] for r in rows_endpoint),
        summary["total_events_processed"],
    )
    check(
        "total_events - events_without_decision == total_events_processed",
        summary["total_events"] - summary["events_without_decision"],
        summary["total_events_processed"],
    )
    check(
        "baseline event_basis.total_events == summary total_events",
        summary["total_events"],
        baseline["event_basis"]["total_events"],
    )
    check(
        "events_by_status sums to total_events",
        sum(summary["events_by_status"].values()),
        summary["total_events"],
    )
    check(
        "distinct + duplicates == recovered_verification_records",
        summary["distinct_recoveries_counted"]
        + summary["duplicate_verification_records_ignored"],
        summary["recovered_verification_records"],
    )
    check(
        "recovery_rate == recovered / at_risk, recomputed",
        percentage(
            summary["total_revenue_recovered"], summary["total_revenue_at_risk"]
        ),
        summary["recovery_rate"],
    )
    check(
        "the non-recoverable money excluded by the baseline equals the "
        "non_recoverable_at_risk the summary reports",
        money(
            summary["total_revenue_at_risk"]
            - baseline["event_basis"]["eligible_revenue_at_risk"]
            - money(
                sum(events_by_id[eid]["amount"] for eid in undiagnosed)
            )
        ),
        summary["non_recoverable_at_risk"],
    )

    # =====================================================================
    heading("5. WHAT THE DASHBOARD ACTUALLY SAYS, IN PLAIN WORDS")
    # =====================================================================
    executed_events = {e["event_id"] for e in executions if e.get("status") == "completed"}
    recovered_events = {d["event_id"] for d in survivors.values()}
    cohort_rate = percentage(len(recovered_events), len(executed_events))

    print(f"  305 events carrying {summary['total_revenue_at_risk']:,.2f} of at-risk revenue.")
    print(f"  {summary['total_events_processed']} were diagnosed and decided; "
          f"{summary['events_without_decision']} was not.")
    print(f"  {baseline['event_basis']['excluded_non_recoverable']} were judged "
          "non-recoverable and deliberately not chased, worth "
          f"{summary['non_recoverable_at_risk']:,.2f}.")
    print()
    print("  TWO RECOVERY RATES, REPORTED SEPARATELY AND NOT RECONCILED:")
    print(f"    headline, over all at-risk revenue    {summary['recovery_rate']:.2f}%  "
          f"({summary['total_revenue_recovered']:,.2f} of "
          f"{summary['total_revenue_at_risk']:,.2f})")
    print(f"    executed cohort, events recovered     "
          f"{cohort_rate if cohort_rate is not None else float('nan'):.2f}%  "
          f"({len(recovered_events)} of {len(executed_events)} events executed)")
    print()
    print("  The gap between them is the point, not an error: only a small number of")
    print("  events were driven all the way through execution, because the Razorpay")
    print("  test account has a 30-payment-link lifetime ceiling. The headline rate")
    print("  divides real recoveries by the WHOLE portfolio; the cohort rate divides")
    print("  them by the events that were actually actioned.")
    print()
    print("  Simulated, on the same 289 eligible events, all gross:")
    print(f"    retry everything     {baseline['baseline_retry_everything']['gross_expected_recovery']:>13,.2f}")
    print(f"    generic reminder     {baseline['baseline_generic_reminder']['gross_expected_recovery']:>13,.2f}")
    print(f"    Vasooli's choices    {baseline['vasooli_expected']['gross_expected_recovery']:>13,.2f}")
    print(f"  Vasooli beats the better baseline by "
          f"{baseline['vasooli_expected']['gross_expected_recovery'] / baseline['baseline_generic_reminder']['gross_expected_recovery']:.2f}x")
    print("  on identical events with identical probabilities, differing only in which")
    print("  intervention each event gets. That is the valid comparison.")

    heading(f"RESULT — {PASSED} passed, {FAILED} failed")
    client.close()
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
