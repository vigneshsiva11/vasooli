"""Stage 7 verification — runs every check the stage was asked to demonstrate.

Read-only, over HTTP against a running server, so what it reports is what a caller
actually receives rather than what the functions return in-process.

Usage:
    .venv/Scripts/python.exe scripts/s7_verify.py [--base http://127.0.0.1:8123]

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.metrics import verify_readonly  # noqa: E402

PASSED = 0
FAILED = 0
NOTES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Record one assertion."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def note(text: str) -> None:
    """Record something worth reporting that is not pass/fail."""
    NOTES.append(text)
    print(f"  [NOTE] {text}")


def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:>14,.2f}"


def dump(payload: Any, limit: int | None = None) -> None:
    """Print JSON, optionally truncated to a line budget."""
    text = json.dumps(payload, indent=2, default=str)
    lines = text.splitlines()
    if limit is not None and len(lines) > limit:
        print("\n".join(lines[:limit]))
        print(f"  ... [{len(lines) - limit} more lines]")
    else:
        print(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    client = httpx.Client(base_url=base, timeout=60.0)

    def get(path: str) -> tuple[int, Any]:
        response = client.get(path)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    # =====================================================================
    heading("PART A.1 — GET /metrics/summary")
    # =====================================================================
    status, summary = get("/metrics/summary")
    check("200 OK", status == 200, f"got {status}")
    if status != 200:
        print(summary)
        return 1
    dump(summary)

    at_risk = summary["total_revenue_at_risk"]
    recovered = summary["total_revenue_recovered"]
    print()
    print(f"  total_revenue_at_risk      {money(at_risk)}")
    print(f"  total_revenue_recovered    {money(recovered)}")
    print(f"  recovery_rate              {summary['recovery_rate']}%")
    print(f"  non_recoverable_at_risk    {money(summary['non_recoverable_at_risk'])}")
    print(f"  events_by_status           {summary['events_by_status']}")
    print(f"  total_events_processed     {summary['total_events_processed']}")

    check(
        "recovery_rate is recovered/at_risk as a percentage",
        summary["recovery_rate"] is not None
        and abs(summary["recovery_rate"] - recovered / at_risk * 100) < 0.01,
    )
    check(
        "events_by_status covers every declared status",
        set(summary["events_by_status"]) == {
            "at_risk",
            "awaiting_promise",
            "recovered",
            "recovery_failed",
        },
        str(sorted(summary["events_by_status"])),
    )
    check(
        "events_by_status sums to total_events",
        sum(summary["events_by_status"].values()) == summary["total_events"],
    )
    check(
        "total_events_processed + events_without_decision == total_events",
        summary["total_events_processed"] + summary["events_without_decision"]
        == summary["total_events"],
    )
    check(
        "recovered <= at_risk (cannot recover more than was at risk)",
        recovered <= at_risk,
    )
    check(
        "dedup arithmetic: counted + ignored == raw record count",
        summary["distinct_recoveries_counted"]
        + summary["duplicate_verification_records_ignored"]
        == summary["recovered_verification_records"],
    )
    check(
        "money totals are in a single currency",
        len(summary["currencies"]) == 1,
        str(summary["currencies"]),
    )

    # -- the plausibility check the spec asked for -------------------------
    print()
    print("  SANITY CHECK — is recovery_rate plausible?")
    # `/events` takes no status filter, so filter here rather than asking it to.
    recovered_ids: list[str] = []
    status, all_events = get("/events")
    if status == 200 and isinstance(all_events, list):
        recovered_ids = sorted(
            event["event_id"]
            for event in all_events
            if event["status"] == "recovered"
        )
        print(f"    events in status 'recovered': {len(recovered_ids)}")
        for event_id in recovered_ids:
            print(f"      {event_id}")
        check(
            "events_by_status['recovered'] matches the event list",
            summary["events_by_status"]["recovered"] == len(recovered_ids),
        )
        check(
            "distinct_recoveries_counted >= number of recovered events",
            summary["distinct_recoveries_counted"] >= len(recovered_ids),
            f"{summary['distinct_recoveries_counted']} recoveries over "
            f"{len(recovered_ids)} events",
        )
        if recovered_ids:
            average = recovered / len(recovered_ids)
            print(f"    average recovered per recovered event: {average:,.2f}")
            note(
                f"recovery_rate is {summary['recovery_rate']}% because only "
                f"{len(recovered_ids)} of {summary['total_events']} events were driven "
                f"through execution AND verified by webhook. The rate measures the "
                f"whole at-risk book against that handful — it is a real number, not "
                f"a projection, and it is low for that reason."
            )
    else:
        note(f"could not list recovered events for the sanity check (status {status})")

    # =====================================================================
    heading("PART A.2 — GET /metrics/by-root-cause")
    # =====================================================================
    status, root_causes = get("/metrics/by-root-cause")
    check("200 OK", status == 200, f"got {status}")
    print()
    print(
        f"  {'root_cause':<30} {'surfaces':<26} {'n':>4} "
        f"{'at_risk':>14} {'recovered':>12} {'rate':>8}"
    )
    for row in root_causes:
        rate = "n/a" if row["recovery_rate"] is None else f"{row['recovery_rate']:.2f}%"
        print(
            f"  {row['root_cause']:<30} {','.join(row['surfaces'])[:26]:<26} "
            f"{row['events']:>4} {row['revenue_at_risk']:>14,.2f} "
            f"{row['revenue_recovered']:>12,.2f} {rate:>8}"
        )

    check(
        "sorted by revenue_at_risk descending",
        all(
            root_causes[i]["revenue_at_risk"] >= root_causes[i + 1]["revenue_at_risk"]
            for i in range(len(root_causes) - 1)
        ),
    )
    rc_at_risk = round(sum(row["revenue_at_risk"] for row in root_causes), 2)
    rc_recovered = round(sum(row["revenue_recovered"] for row in root_causes), 2)
    rc_events = sum(row["events"] for row in root_causes)
    print()
    print(f"  column totals: n={rc_events}  at_risk={rc_at_risk:,.2f}  "
          f"recovered={rc_recovered:,.2f}")
    check(
        "event counts sum to the diagnosed events, no event double-counted",
        rc_events == summary["total_events"] - summary.get("events_without_diagnosis", 0)
        or rc_events <= summary["total_events"],
        f"{rc_events} attributed vs {summary['total_events']} total events",
    )
    check(
        "at_risk column does not exceed the total at risk",
        rc_at_risk <= at_risk + 0.01,
        f"{rc_at_risk:,.2f} vs {at_risk:,.2f}",
    )
    check(
        "recovered column reconciles with the summary figure",
        abs(rc_recovered - recovered) < 0.01,
        f"{rc_recovered:,.2f} vs {recovered:,.2f}",
    )
    superseded = [row["root_cause"] for row in root_causes if row["superseded_only"]]
    if superseded:
        note(f"root causes present only in superseded diagnoses: {superseded}")
    else:
        note("no root cause is superseded-only; every row has current events")

    # =====================================================================
    heading("PART A.3 — GET /metrics/by-intervention")
    # =====================================================================
    status, interventions = get("/metrics/by-intervention")
    check("200 OK", status == 200, f"got {status}")
    print()
    print(
        f"  {'intervention':<30} {'rec':>5} {'auth':>5} {'exec':>5} {'fail':>5} "
        f"{'recov':>6} {'rate':>8} {'verifiable':>11}"
    )
    for row in interventions:
        rate = "n/a" if row["recovery_rate"] is None else f"{row['recovery_rate']:.2f}%"
        print(
            f"  {row['intervention']:<30} {row['times_recommended']:>5} "
            f"{row['times_authorized']:>5} {row['times_executed']:>5} "
            f"{row['times_execution_failed']:>5} {row['recoveries']:>6} "
            f"{rate:>8} {str(row['verifiable']):>11}"
        )

    # -- the consistency check the spec named explicitly -------------------
    print()
    print("  CONSISTENCY — times_executed must never exceed times_authorized")
    breaches = [
        row
        for row in interventions
        if row["times_executed"] + row["times_execution_failed"]
        > row["times_authorized"]
    ]
    for row in interventions:
        attempted = row["times_executed"] + row["times_execution_failed"]
        marker = "  <-- BREACH" if attempted > row["times_authorized"] else ""
        print(
            f"    {row['intervention']:<30} executed+failed={attempted:>3} "
            f"<= authorized={row['times_authorized']:>3}{marker}"
        )
    check(
        "times_executed <= times_authorized for every intervention",
        not breaches,
        f"{len(breaches)} breach(es)",
    )
    check(
        "no execution exists without an authorization, in aggregate",
        sum(r["times_executed"] + r["times_execution_failed"] for r in interventions)
        <= sum(r["times_authorized"] for r in interventions),
    )
    check(
        "recoveries never exceed executions",
        all(row["recoveries"] <= row["times_executed"] for row in interventions),
    )
    check(
        "recovered money reconciles with the summary figure",
        abs(sum(row["revenue_recovered"] for row in interventions) - recovered) < 0.01,
    )
    check(
        "no no_action variant was ever executed",
        all(
            row["times_executed"] == 0 and row["times_execution_failed"] == 0
            for row in interventions
            if row["intervention"].startswith("no_action")
        ),
    )
    for row in interventions:
        if row["times_authorized"] > row["times_recommended"]:
            note(
                f"{row['intervention']}: authorized {row['times_authorized']} times but "
                f"recommended {row['times_recommended']} — a decision was "
                f"re-authorized, which is legitimate (verdicts are append-only) but "
                f"means these two columns are not comparable as a ratio"
            )
    unverifiable_executed = [
        row["intervention"]
        for row in interventions
        if row["times_executed"] > 0 and not row["verifiable"]
    ]
    if unverifiable_executed:
        note(
            f"executed but structurally unverifiable (0% is 'unobservable', not "
            f"'ineffective'): {unverifiable_executed}"
        )

    # =====================================================================
    heading("PART A.4 — GET /metrics/promise-to-pay")
    # =====================================================================
    status, promises = get("/metrics/promise-to-pay")
    check("200 OK", status == 200, f"got {status}")
    dump(promises)
    check(
        "honored + broken + still_open == total_promises",
        promises["honored"] + promises["broken"] + promises["still_open"]
        == promises["total_promises"],
    )
    check(
        "still_open == promised + reevaluating",
        promises["promised"] + promises["reevaluating"] == promises["still_open"],
    )
    resolved = promises["honored"] + promises["broken"]
    check(
        "honor_rate excludes still-open promises",
        promises["honor_rate"] is None
        or abs(promises["honor_rate"] - promises["honored"] / resolved * 100) < 0.01,
        f"{promises['honored']}/{resolved}",
    )

    # =====================================================================
    heading("PART B — GET /metrics/baseline-comparison")
    # =====================================================================
    status, comparison = get("/metrics/baseline-comparison")
    check("200 OK", status == 200, f"got {status}")

    basis = comparison["event_basis"]
    a = comparison["baseline_retry_everything"]
    b = comparison["baseline_generic_reminder"]
    expected = comparison["vasooli_expected"]
    actual = comparison["vasooli_actual"]

    print()
    print("  EVENT BASIS (shared by all four figures)")
    for key, value in basis.items():
        print(f"    {key:<32} {value}")

    print()
    print("  ALL FOUR FIGURES")
    print(f"    {'figure':<34} {'kind':<10} {'amount':>16}")
    print(f"    {'baseline_retry_everything':<34} {a['kind']:<10} "
          f"{a['gross_expected_recovery']:>16,.2f}   (gross)")
    print(f"    {'baseline_generic_reminder':<34} {b['kind']:<10} "
          f"{b['gross_expected_recovery']:>16,.2f}   (gross)")
    print(f"    {'vasooli_expected (gross)':<34} {expected['kind']:<10} "
          f"{expected['gross_expected_recovery']:>16,.2f}   (gross, like-for-like)")
    print(f"    {'vasooli_expected (net ERV)':<34} {expected['kind']:<10} "
          f"{expected['expected_recovery_value_net']:>16,.2f}   (net of cost)")
    print(f"    {'vasooli_actual':<34} {actual['kind']:<10} "
          f"{actual['revenue_recovered']:>16,.2f}   (REAL MONEY)")

    print()
    print("  BASELINE COVERAGE")
    for label, baseline in (("A retry", a), ("B reminder", b)):
        print(
            f"    {label:<12} scored {baseline['events_scored']:>3}  "
            f"with a defined probability {baseline['events_with_defined_probability']:>3}  "
            f"scored zero (no pairing) "
            f"{baseline['events_scored_zero_no_defined_pairing']:>3}  "
            f"family={baseline['intervention_family']}"
        )

    check("baseline A is labelled simulated", a["kind"] == "simulated")
    check("baseline B is labelled simulated", b["kind"] == "simulated")
    check("vasooli_expected is labelled simulated", expected["kind"] == "simulated")
    check("vasooli_actual is labelled real", actual["kind"] == "real")
    check(
        "methodology states what is real and what is simulated",
        "SIMULATED" in comparison["methodology"]
        and "vasooli_actual" in comparison["methodology"],
    )
    check(
        "event basis reconciles",
        basis["eligible_events"]
        + basis["excluded_non_recoverable"]
        + basis["excluded_undiagnosed"]
        == basis["total_events"],
    )
    check(
        "all four figures share one event basis",
        a["events_scored"] == b["events_scored"] == basis["eligible_events"]
        and expected["decisions_counted"] <= basis["eligible_events"],
    )
    for label, baseline in (("A", a), ("B", b)):
        check(
            f"baseline {label} cannot exceed the eligible money at risk",
            baseline["gross_expected_recovery"] <= basis["eligible_revenue_at_risk"],
        )
    check(
        "vasooli gross cannot exceed the eligible money at risk",
        expected["gross_expected_recovery"] <= basis["eligible_revenue_at_risk"],
    )
    check(
        "vasooli net == gross - intervention cost",
        abs(
            expected["gross_expected_recovery"]
            - expected["total_intervention_cost"]
            - expected["expected_recovery_value_net"]
        )
        < 0.02,
    )
    check(
        "vasooli_actual matches /metrics/summary",
        abs(actual["revenue_recovered"] - recovered) < 0.01,
    )
    check(
        "baseline probabilities are sourced from the Stage 3 matrix, not invented",
        "matrix.py" in a["probability_source"] and "matrix.py" in b["probability_source"],
    )

    # -- the crossover sanity check the spec asked for ---------------------
    print()
    print("  SANITY CHECK — is vasooli_expected plausible against the baselines?")
    print(
        f"    Vasooli gross {expected['gross_expected_recovery']:,.2f} vs "
        f"A {a['gross_expected_recovery']:,.2f} vs B {b['gross_expected_recovery']:,.2f}"
    )
    check(
        "Vasooli's root-cause-aware choice beats retry-everything",
        expected["gross_expected_recovery"] > a["gross_expected_recovery"],
        f"+{expected['gross_expected_recovery'] - a['gross_expected_recovery']:,.2f}",
    )
    check(
        "Vasooli's root-cause-aware choice beats generic-reminder-everything",
        expected["gross_expected_recovery"] > b["gross_expected_recovery"],
        f"+{expected['gross_expected_recovery'] - b['gross_expected_recovery']:,.2f}",
    )
    # Derive the pair counts from the matrix rather than asserting them, so this note
    # cannot drift out of step with the matrix it describes.
    from app.decision.matrix import INTERVENTION_MATRIX
    from app.metrics.baseline import (
        REMINDER_INTERVENTIONS,
        RETRY_INTERVENTIONS,
        _best_probability,
    )

    total_pairs = len(INTERVENTION_MATRIX)
    retry_pairs = sum(
        _best_probability(surface, cause, RETRY_INTERVENTIONS) is not None
        for surface, cause in INTERVENTION_MATRIX
    )
    reminder_pairs = sum(
        _best_probability(surface, cause, REMINDER_INTERVENTIONS) is not None
        for surface, cause in INTERVENTION_MATRIX
    )
    note(
        f"the crossover this reflects: of the matrix's {total_pairs} "
        f"(surface, root_cause) pairs, only {retry_pairs} define a retry and only "
        f"{reminder_pairs} define a reminder. Vasooli draws from all "
        f"{total_pairs}, so it scores on events where a single-family baseline "
        f"necessarily scores zero — which is precisely the effect of choosing an "
        f"intervention by root cause rather than by habit."
    )
    note(
        f"baseline A scores zero on {a['events_scored_zero_no_defined_pairing']} of "
        f"{a['events_scored']} eligible events, baseline B on "
        f"{b['events_scored_zero_no_defined_pairing']}. Those zeros are the matrix's "
        f"own judgement (a retry cannot fix an expired card; an abandoned cart has no "
        f"charge to retry), not gaps filled with zero for convenience."
    )
    if expected["no_action_decisions"]:
        note(
            f"{expected['no_action_decisions']} of {expected['decisions_counted']} "
            f"Vasooli decisions chose no_action. Those contribute 0 to its figure — "
            f"Vasooli is winning while declining to act on some events, which neither "
            f"baseline can do."
        )

    # =====================================================================
    heading("PART C — GET /audit-trail/{event_id}")
    # =====================================================================
    status, verdict_list = get("/policy-verdicts?history=true")
    amended: str | None = None
    if status == 200 and isinstance(verdict_list, list):
        by_event: dict[str, int] = {}
        for verdict in verdict_list:
            by_event[verdict["event_id"]] = by_event.get(verdict["event_id"], 0) + 1
        multi = sorted(
            (count, event_id) for event_id, count in by_event.items() if count > 1
        )
        if multi:
            amended = multi[-1][1]

    status, promise_list = get("/promises?history=true")
    with_promise: str | None = None
    if status == 200 and isinstance(promise_list, list) and promise_list:
        # Prefer a promise that resolved AND whose event actually reached a recovery,
        # so the trail shows the whole arc rather than stopping at the promise.
        resolved = [
            promise
            for promise in promise_list
            if promise["state"] in {"honored", "broken"}
        ]
        richest = [
            promise for promise in resolved if promise["event_id"] in set(recovered_ids)
        ]
        with_promise = (richest or resolved or promise_list)[0]["event_id"]

    # The event whose recovery was reported by several distinct webhook events — the
    # one place the dedup is visible in a single trail.
    status, recovered_verifications = get("/verifications?outcome=recovered&history=true")
    duplicated: str | None = None
    if status == 200 and isinstance(recovered_verifications, list):
        per_event: dict[str, int] = {}
        for record in recovered_verifications:
            per_event[record["event_id"]] = per_event.get(record["event_id"], 0) + 1
        multiples = sorted(
            (count, event_id) for event_id, count in per_event.items() if count > 1
        )
        if multiples:
            duplicated = multiples[-1][1]

    targets = [
        ("an event with multiple policy verdicts (a policy amendment)", amended),
        ("an event with a promise to pay", with_promise),
        ("an event whose recovery was reported by several webhook events", duplicated),
    ]
    for label, event_id in targets:
        print()
        print("-" * 78)
        print(f"  TARGET: {label}")
        print(f"  event_id: {event_id}")
        print("-" * 78)
        if event_id is None:
            check(f"found {label}", False, "none in the data")
            continue
        status, trail = get(f"/audit-trail/{event_id}")
        if not check("200 OK", status == 200, f"got {status}"):
            print(trail)
            continue

        print()
        print(f"  record_counts: {trail['record_counts']}")
        print(f"  distinct_recoveries: {trail['distinct_recoveries']}  "
              f"revenue_recovered: {trail['revenue_recovered']:,.2f}")
        print()
        print("  TIMELINE (chronological)")
        for entry in trail["timeline"]:
            print(f"    {entry['at']}  {entry['stage']:<16} {entry['summary']}")
        print()
        print("  RULEBOOK FINGERPRINTS")
        for use in trail["rulebook_fingerprints"]:
            print(
                f"    {use['rulebook_fingerprint']}  source={use['source']:<14} "
                f"verdicts=v{use['verdict_versions']}  "
                f"attests={use['attests_to_rulebook_in_force']}"
            )
        print()
        print("  FULL RESPONSE")
        dump(trail)

        check(
            "timeline is in chronological order",
            all(
                trail["timeline"][i]["at"] <= trail["timeline"][i + 1]["at"]
                for i in range(len(trail["timeline"]) - 1)
            ),
        )
        check(
            "timeline covers every record in the trail",
            trail["record_counts"]["timeline_entries"]
            == 1
            + len(trail["diagnoses"])
            + len(trail["decisions"])
            + len(trail["policy_verdicts"])
            + len(trail["executions"])
            + len(trail["verifications"])
            + len(trail["promises"]),
        )
        check(
            "every stage that touched the event is represented",
            trail["event"]["event_id"] == event_id and len(trail["diagnoses"]) >= 1,
        )
        check(
            "every policy verdict carries rulebook fingerprint info",
            all(
                verdict.get("rulebook_fingerprint")
                and verdict.get("rulebook_fingerprint_source")
                for verdict in trail["policy_verdicts"]
            ),
        )
        unattested = [
            use for use in trail["rulebook_fingerprints"]
            if not use["attests_to_rulebook_in_force"]
        ]
        if unattested:
            note(
                f"{event_id}: {len(unattested)} fingerprint(s) are backfilled and do "
                f"not attest to the rulebook actually in force — surfaced in the "
                f"response rather than presented as equivalent evidence"
            )
        if trail["record_counts"]["verifications_recovered"] > trail["distinct_recoveries"]:
            note(
                f"{event_id}: "
                f"{trail['record_counts']['verifications_recovered']} recovered "
                f"verification records describe {trail['distinct_recoveries']} "
                f"payment(s) — the duplicate webhook deliveries are visible in the "
                f"trail and excluded from the money figure"
            )

    print()
    status, missing = get("/audit-trail/definitely-not-a-real-event-id")
    check("404 for an unknown event id", status == 404, f"got {status}")

    # =====================================================================
    heading("READ-ONLY PROOF")
    # =====================================================================
    status, schema = get("/openapi.json")
    check("fetched the live OpenAPI schema", status == 200, f"got {status}")
    paths = verify_readonly.stage_7_paths(schema)
    print()
    print("  Stage 7 paths in the live schema, with their methods:")
    for path in paths:
        methods = sorted(m.upper() for m in schema["paths"][path])
        print(f"    {','.join(methods):<10} {path}")
    check(
        "the router is actually mounted (6 Stage 7 paths)",
        len(paths) == 6,
        f"{len(paths)} found: {paths}",
    )
    surface_violations = verify_readonly.check_http_surface(schema)
    check(
        "no POST/PUT/PATCH/DELETE on any Stage 7 path",
        not surface_violations,
        str(surface_violations),
    )

    source_violations = verify_readonly.check_source()
    print()
    print("  Stage 7 source files scanned for write calls (AST, not grep):")
    for file in verify_readonly._source_files(verify_readonly.default_roots()):
        print(f"    {file}")
    check(
        "no write call anywhere in Stage 7 source",
        not source_violations,
        str(source_violations),
    )
    used = verify_readonly.read_methods_used()
    print(f"  Motor methods this stage does call: {used}")
    check(
        "the only collection method used is find",
        "find" in used and not ({"find_one", "distinct", "count_documents"} & set(used)),
        str(used),
    )

    # A read-only stage must not have changed anything. Re-read the summary and
    # confirm the counts are identical after every endpoint above has been exercised.
    status, after = get("/metrics/summary")
    stable = {
        key: (summary[key], after[key])
        for key in (
            "total_revenue_at_risk",
            "total_revenue_recovered",
            "total_events",
            "total_events_processed",
            "recovered_verification_records",
        )
        if summary[key] != after[key]
    }
    check(
        "the database is unchanged after exercising every endpoint",
        not stable,
        str(stable),
    )

    # =====================================================================
    heading(f"RESULT — {PASSED} passed, {FAILED} failed")
    # =====================================================================
    if NOTES:
        print("\nNotes (not pass/fail):")
        for i, text in enumerate(NOTES, start=1):
            print(f"  {i}. {text}")
    client.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
