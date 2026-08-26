"""Independent verification of Stage 7. Trusts nothing in app/metrics/.

Deliberately imports NOTHING from `app.metrics`, `app.models`, or any stage's store
module. It reads `.env` for the connection string, opens its own pymongo client, hits
the collections by hardcoded literal name, and does its own arithmetic. Then it calls
the live endpoints over HTTP and compares.

Hardcoding the collection names is part of the point: if a name is wrong, this script
finds an empty collection and fails loudly rather than agreeing with the app because it
asked the app where to look.

A mismatch is reported as a mismatch. This script has no logic for deciding that a
difference is acceptable.

Usage:
    .venv/Scripts/python.exe scripts/s7_independent_check.py [--base URL] [--event ID]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bson import ObjectId
from pymongo import MongoClient

# Hardcoded on purpose — see the module docstring.
EVENTS = "events"
DIAGNOSES = "diagnoses"
DECISIONS = "decisions"
VERDICTS = "policy_verdicts"
EXECUTIONS = "executions"
VERIFICATIONS = "verifications"
PROMISES = "promises"

ALL_COLLECTIONS = (
    EVENTS,
    DIAGNOSES,
    DECISIONS,
    VERDICTS,
    EXECUTIONS,
    VERIFICATIONS,
    PROMISES,
)

#: Maps a collection to the key its records appear under in the audit-trail response.
TRAIL_KEY = {
    DIAGNOSES: "diagnoses",
    DECISIONS: "decisions",
    VERDICTS: "policy_verdicts",
    EXECUTIONS: "executions",
    VERIFICATIONS: "verifications",
    PROMISES: "promises",
}

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


def read_env(path: Path) -> dict[str, str]:
    """Parse `.env` without importing the app's settings object.

    Values are returned for use, never printed — the connection string carries a
    password.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def norm(value):
    """Canonicalise a value so a BSON document and a JSON payload are comparable.

    ObjectIds become strings, datetimes become tz-naive UTC ISO strings, and numbers
    become floats — so 2000 and 2000.0 do not read as a discrepancy when they are not
    one.
    """
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, str):
        if ISO.match(value):
            try:
                return norm(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                return value
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [norm(item) for item in value]
    if isinstance(value, dict):
        return {key: norm(item) for key, item in value.items()}
    return value


def show(document: dict) -> str:
    return json.dumps(
        {key: norm(value) for key, value in sorted(document.items())},
        indent=4,
        default=str,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument("--event", default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    env = read_env(root / ".env")
    uri = env.get("MONGODB_URI") or os.environ["MONGODB_URI"]
    db_name = env.get("MONGODB_DB_NAME", "vasooli")

    client = MongoClient(uri, serverSelectionTimeoutMS=20000)
    db = client[db_name]
    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=60.0)

    print(f"database: {db_name}   server: {args.base}")
    present = set(db.list_collection_names())
    missing = [name for name in ALL_COLLECTIONS if name not in present]
    assert_true(
        "every hardcoded collection name exists in the database",
        not missing,
        f"missing: {missing}" if missing else "all 7 found",
    )

    # =====================================================================
    heading("1. INDEPENDENT COMPUTATION — raw pymongo, no app code")
    # =====================================================================

    # -- events -----------------------------------------------------------
    event_docs = list(db[EVENTS].find({}))
    my_event_count = len(event_docs)
    my_amount_sum = round(sum(doc["amount"] for doc in event_docs), 2)
    my_status_counts = dict(Counter(doc["status"] for doc in event_docs))
    print(f"  events: count={my_event_count}  sum(amount)={my_amount_sum:,.2f}")
    print(f"  events by status: {my_status_counts}")

    # -- recovered verifications, deduplicated by hand --------------------
    recovered = list(db[VERIFICATIONS].find({"outcome": "recovered"}))
    my_recovered_records = len(recovered)
    my_recovered_raw_sum = round(sum(doc["amount_recovered"] for doc in recovered), 2)

    by_execution: dict[str, list[dict]] = defaultdict(list)
    for doc in recovered:
        by_execution[str(doc["execution_id"])].append(doc)

    my_distinct_recoveries = len(by_execution)
    my_duplicates_ignored = my_recovered_records - my_distinct_recoveries

    # Sum one record per execution. Which record is picked only matters if the amounts
    # inside a group disagree, so check that separately rather than assuming.
    ambiguous = {
        exec_id: sorted({doc["amount_recovered"] for doc in docs})
        for exec_id, docs in by_execution.items()
        if len({doc["amount_recovered"] for doc in docs}) > 1
    }
    my_deduped_sum = round(
        sum(
            max(docs, key=lambda doc: doc["verified_at"])["amount_recovered"]
            for docs in by_execution.values()
        ),
        2,
    )
    print()
    print(f"  verifications outcome=recovered: {my_recovered_records} records")
    print(f"    raw sum (every record)        {my_recovered_raw_sum:>14,.2f}")
    print(f"    distinct execution_ids        {my_distinct_recoveries}")
    print(f"    duplicates                    {my_duplicates_ignored}")
    print(f"    deduped sum (one per exec)    {my_deduped_sum:>14,.2f}")
    print("    grouped:")
    for exec_id, docs in by_execution.items():
        amounts = sorted({doc["amount_recovered"] for doc in docs})
        print(
            f"      execution {exec_id}  n={len(docs)}  "
            f"event={docs[0]['event_id']}  amount(s)={amounts}"
        )
    assert_true(
        "amounts agree within every execution group, so the dedup pick is immaterial",
        not ambiguous,
        f"ambiguous groups: {ambiguous}" if ambiguous else "checked all groups",
    )

    # -- promises ---------------------------------------------------------
    promise_docs = list(db[PROMISES].find({}))
    my_promise_states = dict(Counter(doc["state"] for doc in promise_docs))
    print()
    print(f"  promises: count={len(promise_docs)}  by state={my_promise_states}")

    # =====================================================================
    heading("2. COMPARISON AGAINST THE LIVE ENDPOINTS")
    # =====================================================================
    summary = http.get("/metrics/summary").json()
    promises_endpoint = http.get("/metrics/promise-to-pay").json()

    print("\n  GET /metrics/summary")
    check("total event count", my_event_count, summary["total_events"])
    check("sum of RevenueEvent.amount", my_amount_sum, summary["total_revenue_at_risk"])
    check("events grouped by status", my_status_counts, summary["events_by_status"])
    check(
        "raw count of recovered VerificationRecords",
        my_recovered_records,
        summary["recovered_verification_records"],
    )
    check(
        "distinct recoveries after deduplicating by execution",
        my_distinct_recoveries,
        summary["distinct_recoveries_counted"],
    )
    check(
        "duplicate records excluded from the money total",
        my_duplicates_ignored,
        summary["duplicate_verification_records_ignored"],
    )
    check(
        "deduplicated recovered amount",
        my_deduped_sum,
        summary["total_revenue_recovered"],
    )
    assert_true(
        "the endpoint does NOT report the raw (double-counted) sum",
        summary["total_revenue_recovered"] != my_recovered_raw_sum,
        f"raw would be {my_recovered_raw_sum:,.2f}, endpoint reports "
        f"{summary['total_revenue_recovered']:,.2f}",
    )
    check(
        "recovery_rate recomputed independently",
        round(my_deduped_sum / my_amount_sum * 100, 2),
        summary["recovery_rate"],
    )

    print("\n  GET /metrics/promise-to-pay")
    check("total promises", len(promise_docs), promises_endpoint["total_promises"])
    for state, field in (
        ("honored", "honored"),
        ("broken", "broken"),
        ("promised", "promised"),
        ("reevaluating", "reevaluating"),
    ):
        check(
            f"promises in state {state!r}",
            my_promise_states.get(state, 0),
            promises_endpoint[field],
        )
    check(
        "still_open == promised + reevaluating, computed independently",
        my_promise_states.get("promised", 0) + my_promise_states.get("reevaluating", 0),
        promises_endpoint["still_open"],
    )
    my_resolved = my_promise_states.get("honored", 0) + my_promise_states.get("broken", 0)
    check(
        "honor_rate over resolved promises, computed independently",
        round(my_promise_states.get("honored", 0) / my_resolved * 100, 2)
        if my_resolved
        else None,
        promises_endpoint["honor_rate"],
    )
    unexpected = set(my_promise_states) - {
        "honored",
        "broken",
        "promised",
        "reevaluating",
    }
    assert_true(
        "no promise sits in a state the endpoint does not report",
        not unexpected,
        f"unreported states: {unexpected}" if unexpected else "all states accounted for",
    )

    # =====================================================================
    heading("3. SPOT CHECK — raw documents beside GET /audit-trail/{event_id}")
    # =====================================================================
    if args.event:
        event_id = args.event
        why = "chosen on the command line"
    else:
        # Pick the event touching the most collections, so the comparison exercises
        # every branch of the endpoint's assembly rather than a flattering subset.
        spread: dict[str, tuple[int, int]] = {}
        for doc in event_docs:
            eid = doc["event_id"]
            counts = [
                db[name].count_documents({"event_id": eid})
                for name in ALL_COLLECTIONS
                if name != EVENTS
            ]
            spread[eid] = (sum(1 for c in counts if c), sum(counts))
        event_id = max(spread, key=lambda eid: spread[eid])
        why = (
            f"present in {spread[event_id][0]} of 6 non-event collections, "
            f"{spread[event_id][1]} related documents — the widest spread in the data"
        )
    print(f"  event_id: {event_id}\n  ({why})")

    raw: dict[str, list[dict]] = {}
    raw_event = db[EVENTS].find_one({"event_id": event_id})
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        raw[name] = sorted(
            db[name].find({"event_id": event_id}),
            key=lambda doc: (doc.get("version", 0), doc.get("_id")),
        )

    trail = http.get(f"/audit-trail/{event_id}").json()

    print()
    print("-" * 78)
    print("  RAW DOCUMENTS, DIRECTLY FROM MONGODB")
    print("-" * 78)
    print(f"\n  == {EVENTS} ==")
    print(show(raw_event))
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        print(f"\n  == {name} ({len(raw[name])}) ==")
        for doc in raw[name]:
            print(show(doc))

    print()
    print("-" * 78)
    print("  GET /audit-trail/{event_id} RESPONSE")
    print("-" * 78)
    print(json.dumps(trail, indent=4, default=str))

    print()
    print("-" * 78)
    print("  SIDE BY SIDE")
    print("-" * 78)
    print(f"  {'collection':<18} {'in mongodb':>11} {'in response':>12}")
    print(f"  {EVENTS:<18} {1 if raw_event else 0:>11} "
          f"{1 if trail.get('event') else 0:>12}")
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        print(f"  {name:<18} {len(raw[name]):>11} {len(trail[TRAIL_KEY[name]]):>12}")

    assert_true(
        "the event document itself is present in the response",
        bool(raw_event) and trail.get("event", {}).get("event_id") == event_id,
    )
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        check(
            f"record count for {name}",
            len(raw[name]),
            len(trail[TRAIL_KEY[name]]),
        )

    # Identity, not just count: the same documents, matched by _id.
    print()
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        if not raw[name]:
            continue
        mine_ids = sorted(str(doc["_id"]) for doc in raw[name])
        theirs_ids = sorted(
            str(record.get("id")) for record in trail[TRAIL_KEY[name]]
        )
        check(f"the same {name} documents, by _id", mine_ids, theirs_ids)

    # Field-level: does the response misstate any value it does return?
    print()
    print("  FIELD-LEVEL COMPARISON (every field the response returns)")
    mismatches: list[str] = []
    omitted: list[str] = []
    pairs: list[tuple[str, dict, dict]] = [(EVENTS, raw_event, trail["event"])]
    for name in ALL_COLLECTIONS:
        if name == EVENTS:
            continue
        by_id = {str(record.get("id")): record for record in trail[TRAIL_KEY[name]]}
        for doc in raw[name]:
            record = by_id.get(str(doc["_id"]))
            if record is not None:
                pairs.append((name, doc, record))

    for name, doc, record in pairs:
        for key, value in doc.items():
            field = "id" if key == "_id" else key
            if field not in record:
                omitted.append(f"{name}.{field}")
                continue
            if norm(value) != norm(record[field]):
                mismatches.append(
                    f"{name}[{doc['_id']}].{field}: "
                    f"mongodb={norm(value)!r} response={norm(record[field])!r}"
                )
    fields_compared = sum(
        1 for name, doc, record in pairs
        for key in doc
        if ("id" if key == "_id" else key) in record
    )
    assert_true(
        f"no value is misstated ({fields_compared} fields compared)",
        not mismatches,
        "\n         ".join(mismatches) if mismatches else "",
    )
    assert_true(
        "no field stored in MongoDB is absent from the response",
        not omitted,
        f"absent: {sorted(set(omitted))}" if omitted else "",
    )

    # The response also carries derived fields. Recompute them independently.
    print()
    print("  DERIVED FIELDS ON THE TRAIL")
    my_trail_recovered = [
        doc for doc in raw[VERIFICATIONS] if doc["outcome"] == "recovered"
    ]
    my_trail_executions = {str(doc["execution_id"]) for doc in my_trail_recovered}
    check(
        "distinct_recoveries for this event",
        len(my_trail_executions),
        trail["distinct_recoveries"],
    )
    check(
        "revenue_recovered for this event",
        round(
            sum(
                max(
                    (d for d in my_trail_recovered if str(d["execution_id"]) == exec_id),
                    key=lambda d: d["verified_at"],
                )["amount_recovered"]
                for exec_id in my_trail_executions
            ),
            2,
        ),
        trail["revenue_recovered"],
    )
    check(
        "timeline entry count == 1 event + every related document",
        1 + sum(len(raw[name]) for name in ALL_COLLECTIONS if name != EVENTS),
        trail["record_counts"]["timeline_entries"],
    )
    check(
        "authorized verdict count",
        sum(1 for doc in raw[VERDICTS] if doc["verdict"] == "authorized"),
        trail["record_counts"]["policy_verdicts_authorized"],
    )
    check(
        "completed execution count",
        sum(1 for doc in raw[EXECUTIONS] if doc["status"] == "completed"),
        trail["record_counts"]["executions_completed"],
    )
    check(
        "recovered verification count",
        len(my_trail_recovered),
        trail["record_counts"]["verifications_recovered"],
    )

    heading(f"RESULT — {PASSED} passed, {FAILED} failed")
    client.close()
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
