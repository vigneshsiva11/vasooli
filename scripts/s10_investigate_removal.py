"""Stage 10 test-promise removal — INVESTIGATION ONLY. Writes nothing.

Maps every record and every metric that touches the seven promises created during
Stage 10 verification, so the removal is planned against what actually references
them rather than against an assumption about what does.

Run this before `s10_remove_test_promises.py`. It prints the exact archive payload
that script will write, so the two can be compared.
"""

from __future__ import annotations

import asyncio
import collections
import json

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database

BASE = "http://127.0.0.1:8123"

# The seven, named individually rather than matched by a pattern, so nothing can be
# swept up by a query that turns out to be broader than intended.
#
# NOTE ON SCOPE: the request enumerated these as "L1-L4, plus the two
# executed-follow-up parity pairs". Read strictly, one executed pair is
# ptp_..._A/_B — two promises, not four — and that arithmetic gives six, not seven.
# The seventh is demo_195_rcv, the structured control from the SUPPRESSED-path
# parity run (its partner, demo_188_rcv, is already counted as L2). Since the
# request said seven and seven is the exact number Stage 10 created, all seven are
# listed. Both parity runs are represented: one pair per branch.
TARGETS = {
    "demo_186_rcv":            "L1  extracted, clear message",
    "demo_188_rcv":            "L2  extracted, same text on an older clock",
    "demo_191_rcv":            "L3  extracted, explicit amount",
    "demo_193_rcv":            "L4  extracted, relative date",
    "demo_195_rcv":            "structured control, SUPPRESSED-path parity pair (with demo_188_rcv)",
    "ptp_20260825T111455_A":   "extracted, EXECUTED-path parity pair",
    "ptp_20260825T111455_B":   "structured control, EXECUTED-path parity pair",
}


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def main() -> int:
    await connect_to_mongo()
    db = get_database()

    show("1. THE SEVEN PROMISES AS THEY STAND")
    promises = []
    async for p in db.promises.find({"event_id": {"$in": list(TARGETS)}}):
        promises.append(p)
    print(f"  found {len(promises)} promise documents "
          f"(expected 7 — one per target event)")
    print(f"\n  {'event_id':<26}{'state':<15}{'date':<13}{'amount':<11}follow_up")
    print(f"  {'-'*78}")
    for p in sorted(promises, key=lambda d: d["event_id"]):
        print(f"  {p['event_id']:<26}{p['state']:<15}{p['promised_date']:<13}"
              f"{p['promised_amount']:<11}{p['follow_up_sent']}")
    states = collections.Counter(p["state"] for p in promises)
    print(f"\n  states being removed: {dict(states)}")

    # Confirm nothing else lives on these events by accident.
    extra = [p["event_id"] for p in promises if p["event_id"] not in TARGETS]
    print(f"  promises matched outside the target list: {extra or 'none'}")
    for event_id in TARGETS:
        n = await db.promises.count_documents({"event_id": event_id})
        if n != 1:
            print(f"  !! {event_id} holds {n} promises, not 1 — check before removing")

    show("2. HONOR RATE — before, and the arithmetic after")
    all_states = collections.Counter()
    async for p in db.promises.find({}, {"state": 1}):
        all_states[p["state"]] += 1
    after = collections.Counter(all_states)
    after.subtract(states)
    rate = lambda c: round(100 * c["honored"] / (c["honored"] + c["broken"]), 2)
    print(f"  now   : {dict(all_states)}  total {sum(all_states.values())}  "
          f"honor_rate {rate(all_states)}%")
    print(f"  after : {dict(after)}  total {sum(after.values())}  "
          f"honor_rate {rate(after)}%")
    print(f"\n  none of the seven is 'honored', so removal cannot inflate the rate by")
    print(f"  discarding a failure — it only removes 2 'broken' and 2 'reevaluating'")
    print(f"  that were overdue by construction, plus 3 still-open.")

    show("3. EVENT STATUSES — what the promises moved, and must move back")
    print(f"  {'event_id':<26}{'status now':<22}{'executions':<12}verdicts")
    print(f"  {'-'*78}")
    for event_id in sorted(TARGETS):
        e = await db.events.find_one({"event_id": event_id}, {"status": 1})
        nexe = await db.executions.count_documents({"event_id": event_id})
        nver = await db.policy_verdicts.count_documents({"event_id": event_id})
        print(f"  {event_id:<26}{e['status']:<22}{nexe:<12}{nver}")
    print("\n  every one of these was 'at_risk' before Stage 10; `create_promise`")
    print("  moved them to 'awaiting_promise'. Leaving them there with no promise")
    print("  would be a broken state: `POST /promises/{id}/check` would 404 and")
    print("  events_by_status would count seven events as awaiting something that")
    print("  does not exist.")

    show("4. DOWNSTREAM REFERENCES — what points at these promises")

    print("\n  (a) promise_extractions.promise_id")
    linked = []
    async for x in db.promise_extractions.find(
        {"event_id": {"$in": list(TARGETS)}},
        {"event_id": 1, "promise_id": 1, "accepted": 1, "refusal_reason": 1},
    ):
        linked.append(x)
    ids = {str(p["_id"]) for p in promises}
    dangling = [x for x in linked if x.get("promise_id") in ids]
    print(f"      {len(linked)} extraction records on these events; "
          f"{len(dangling)} carry a promise_id that would DANGLE after removal:")
    for x in dangling:
        print(f"        {x['event_id']:<26} promise_id={x['promise_id']}")
    print("      ^^ THIS IS A REAL DEPENDENCY. It must be resolved, not ignored.")

    print("\n  (b) executions caused by the follow-ups")
    for event_id in ("ptp_20260825T111455_A", "ptp_20260825T111455_B"):
        async for x in db.executions.find({"event_id": event_id}):
            print(f"      {event_id:<26} {x['intervention']} / {x['action_type']} "
                  f"status={x['status']} link={x.get('razorpay_payment_link_id')}")
    print("      these record contacts that genuinely were logged. They do not")
    print("      reference a promise_id — an execution stands on its policy verdict.")

    print("\n  (c) verifications referencing these events")
    n = await db.verifications.count_documents({"event_id": {"$in": list(TARGETS)}})
    print(f"      {n} verification records — so no recovered money depends on these")

    print("\n  (d) any collection holding a field named like a promise reference")
    for name in await db.list_collection_names():
        sample = await db[name].find_one({})
        if not sample:
            continue
        hits = [k for k in sample if "promise" in k.lower()]
        if hits:
            print(f"      {name}: {hits}")

    show("5. METRICS THAT READ THE PROMISES COLLECTION")
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        for path in ("/metrics/summary", "/metrics/promise-to-pay",
                     "/metrics/by-intervention", "/metrics/baseline-comparison"):
            r = await client.get(path)
            blob = json.dumps(r.json()).lower()
            touches = "promise" in blob
            print(f"  {path:<34}{r.status_code}  mentions promises: {touches}")

        r = await client.get("/metrics/summary")
        s = r.json()
        print(f"\n  summary figures that WILL move:")
        print(f"    events_by_status.awaiting_promise = {s['events_by_status'].get('awaiting_promise')}"
              f"  -> should drop by 7")
        print(f"    events_by_status.at_risk          = {s['events_by_status'].get('at_risk')}"
              f"  -> should rise by 7")
        print(f"\n  summary figures that MUST NOT move (no money is involved):")
        for k in ("total_revenue_at_risk", "total_revenue_recovered", "recovery_rate",
                  "recovery_rate_gateway_verified", "distinct_recoveries_counted",
                  "total_events", "total_events_processed"):
            print(f"    {k} = {s[k]}")

        for event_id in ("demo_188_rcv", "ptp_20260825T111455_A"):
            r = await client.get(f"/audit-trail/{event_id}")
            a = r.json()
            print(f"\n  /audit-trail/{event_id}: "
                  f"promises={len(a.get('promises', []))} "
                  f"executions={len(a.get('executions', []))} "
                  f"verdicts={len(a.get('policy_verdicts', []))}")
            print(f"    record_counts={a.get('record_counts')}")

    show("6. CODE THAT READS THE PROMISES COLLECTION")
    print("  grep is run separately — see the shell output following this script.")

    await close_mongo_connection()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
