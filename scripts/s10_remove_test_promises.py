"""Remove the seven Stage 10 test promises. Archive first, verify, then delete.

Order is deliberate and enforced by the control flow rather than by discipline: the
archive is written AND READ BACK AND COMPARED against what is still in the database
before a single delete runs. If the round-trip does not match, the script exits
non-zero having deleted nothing.

Three writes happen, in this order:

  1. the seven promise documents are deleted, one at a time, keyed on `_id`;
  2. their seven events are moved from `awaiting_promise` back to `at_risk`;
  3. nothing else.

Step 2 is a raw `$set`, which bypasses `transition_event_status`, and that is
disclosed rather than hidden. `ALLOWED_STATUS_TRANSITIONS` gives `awaiting_promise`
no arc back to `at_risk` — deliberately, because in normal operation a promise is
never unmade. There is therefore no guarded path for this and there should not be
one. The `$set` is filtered on the status still being exactly `awaiting_promise`, so
it cannot move an event that has meanwhile gone somewhere else.

What is NOT touched, and why, is recorded in the archive alongside what is.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
from datetime import datetime, timezone

import httpx
from bson import ObjectId

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models.events import ALLOWED_STATUS_TRANSITIONS

BASE = "http://127.0.0.1:8123"
ARCHIVE_DIR = ".s10_archive"

# See scripts/s10_investigate_removal.py for how this list was arrived at, including
# the note on the seventh record.
TARGETS = {
    "demo_186_rcv": "L1 — extracted from 'I'll pay by Friday' on a 2026-08-25 clock",
    "demo_188_rcv": "L2 — the same text on a 2026-07-06 clock, overdue on creation",
    "demo_191_rcv": "L3 — extracted with an explicit amount",
    "demo_193_rcv": "L4 — extracted from a relative date ('next Monday')",
    "demo_195_rcv": "structured control for the suppressed-path parity pair (demo_188_rcv)",
    "ptp_20260825T111455_A": "extracted half of the executed-path parity pair",
    "ptp_20260825T111455_B": "structured control half of the executed-path parity pair",
}

RESTORE_NOTES = (
    "Serialized to match .s8_archive/.s9_archive: `_id` values and timestamps are "
    "plain strings. To restore, re-cast every `_id` with bson.ObjectId and every "
    "`created_at`/`resolved_at` with datetime.fromisoformat before inserting. "
    "`promised_date` is stored as a plain 'YYYY-MM-DD' string in MongoDB and needs "
    "no conversion."
)

WHY = (
    "Created by Stage 10's verification run, not by any business activity. Four were "
    "extracted from free-text messages I wrote to exercise the extractor; three are "
    "controls or their structured counterparts. Every one was built to hit an edge "
    "case rather than to represent a customer: demo_188_rcv, demo_195_rcv, "
    "ptp_..._A and ptp_..._B were dated 2026-07-10 from a deliberately stale "
    "reference clock, so all four were already overdue the moment they existed. That "
    "is the point of the test and the reason they cannot stay: they moved honor_rate "
    "from 72.73% to 61.54% by adding breakage that no customer caused. None of the "
    "seven is 'honored', so removing them cannot flatter the metric by discarding a "
    "failure — it removes 2 'broken' and 2 'reevaluating' that were overdue by "
    "construction, plus 3 still open."
)

PASS = 0
FAIL = 0


def check(label: str, expected, actual, note: str = "") -> bool:
    global PASS, FAIL
    ok = expected == actual
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"         expected={expected!r}")
    print(f"         actual  ={actual!r}")
    if note:
        print(f"         {note}")
    return ok


def plain(value):
    """ObjectId/datetime -> string, matching the earlier archives' convention."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def honor_rate(counter) -> float:
    return round(100 * counter["honored"] / (counter["honored"] + counter["broken"]), 2)


FROZEN = (
    "total_revenue_at_risk", "total_revenue_recovered", "recovery_rate",
    "recovery_rate_gateway_verified", "distinct_recoveries_counted",
    "total_events", "total_events_processed",
)


async def main() -> int:
    await connect_to_mongo()
    db = get_database()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(ARCHIVE_DIR, f"promises_stage10_test_records_{stamp}.json")

    print("=" * 78)
    print("STAGE 10 TEST PROMISES — ARCHIVE, THEN REMOVE")
    print(f"  archive: {path}")
    print("=" * 78)

    # ------------------------------------------------------------------ #
    print("\n[A] GATHER — everything that will be removed, and everything that will not")
    # ------------------------------------------------------------------ #
    promises = [p async for p in db.promises.find({"event_id": {"$in": list(TARGETS)}})]
    check("A1  exactly seven promise documents matched", 7, len(promises))
    check("A2  one per target event, none matched outside the list",
          sorted(TARGETS), sorted(p["event_id"] for p in promises))

    target_ids = {str(p["_id"]) for p in promises}
    check("A3  seven distinct _id values", 7, len(target_ids))

    # Every surviving _id, recorded BEFORE the deletes. Verifying by _id afterwards
    # is what distinguishes "the right seven went" from "seven went".
    survivor_ids = {
        str(p["_id"]) async for p in db.promises.find({}, {"_id": 1})
    } - target_ids
    states_before = collections.Counter()
    async for p in db.promises.find({}, {"state": 1}):
        states_before[p["state"]] += 1
    states_removed = collections.Counter(p["state"] for p in promises)
    expected_after = collections.Counter(states_before)
    expected_after.subtract(states_removed)

    print(f"\n  promises now: {sum(states_before.values())}  "
          f"honor_rate {honor_rate(states_before)}%")
    print(f"  after       : {sum(expected_after.values())}  "
          f"honor_rate {honor_rate(expected_after)}%")
    check("A4  the arithmetic lands on the figure the request predicted",
          72.73, honor_rate(expected_after))

    statuses_before = {}
    for event_id in TARGETS:
        doc = await db.events.find_one({"event_id": event_id}, {"status": 1})
        statuses_before[event_id] = doc["status"]
    check("A5  all seven events are 'awaiting_promise' right now",
          {"awaiting_promise"}, set(statuses_before.values()))
    check("A6  ...and the status table has NO arc back to 'at_risk'",
          False, "at_risk" in ALLOWED_STATUS_TRANSITIONS["awaiting_promise"],
          "which is why step 2 is a disclosed raw $set, not a guarded transition")

    # Records that are being kept. Captured into the archive so the entry says what
    # survived, not only what went.
    extractions = [
        plain(x) async for x in db.promise_extractions.find(
            {"event_id": {"$in": list(TARGETS)}}
        )
    ]
    executions = [
        plain(x) async for x in db.executions.find({"event_id": {"$in": list(TARGETS)}})
    ]
    verdicts = [
        plain(v) async for v in db.policy_verdicts.find(
            {"event_id": {"$in": list(TARGETS)}}, {"event_id": 1, "verdict": 1, "reason": 1}
        )
    ]
    linked = [x for x in extractions if x.get("promise_id") in target_ids]
    check("A7  five extraction records point at promises about to be removed",
          5, len(linked),
          "kept, not nulled — see the archive's `kept` section for why")

    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        summary_before = (await client.get("/metrics/summary")).json()
        ptp_before = (await client.get("/metrics/promise-to-pay")).json()
    check("A8  the live endpoint agrees with the raw count of promises",
          sum(states_before.values()), ptp_before["total_promises"])
    check("A9  the live endpoint agrees with the raw honor_rate",
          honor_rate(states_before), ptp_before["honor_rate"])

    # ------------------------------------------------------------------ #
    print("\n[B] ARCHIVE — written, then read back, then compared. No deletes yet.")
    # ------------------------------------------------------------------ #
    payload = {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "what": "the seven promises created by Stage 10's verification run",
        "why": WHY,
        "restore_notes": RESTORE_NOTES,
        "honor_rate_before": honor_rate(states_before),
        "honor_rate_after": honor_rate(expected_after),
        "promise_states_removed": dict(states_removed),
        "removed": [
            {
                "reason_created": TARGETS[p["event_id"]],
                "event_status_before_removal": statuses_before[p["event_id"]],
                "event_status_restored_to": "at_risk",
                "promise": plain(p),
            }
            for p in sorted(promises, key=lambda d: d["event_id"])
        ],
        "kept": {
            "promise_extractions": {
                "count": len(extractions),
                "linked_to_removed_promises": len(linked),
                "why_kept": (
                    "These are the audit record of nine real Gemini calls, including "
                    "the raw model response for each. Deleting them would destroy the "
                    "evidence Stage 10 rests on, and they are not promises: they do "
                    "not enter honor_rate or any other metric. The five promise_id "
                    "values are left pointing at the promises in this file rather "
                    "than nulled, because `PromiseExtraction.promise_id` documents a "
                    "null on an accepted extraction as meaning either the linking "
                    "write did not complete or creation was refused downstream. "
                    "Neither is true here, so nulling would store a false statement "
                    "in an audit log. The pointers resolve against this archive."
                ),
                "records": extractions,
            },
            "executions": {
                "count": len(executions),
                "why_kept": (
                    "Two contacts genuinely were rendered and logged for the "
                    "executed-parity pair, each through the policy gate, each with "
                    "razorpay_payment_link_id null. An execution stands on its policy "
                    "verdict and holds no reference to a promise, so neither becomes "
                    "dangling. Deleting a record of a contact that was actually made "
                    "would misstate the contact history the caps are counted from."
                ),
                "records": executions,
            },
            "policy_verdicts": {
                "count": len(verdicts),
                "why_kept": "append-only; a verdict records a decision that was made",
                "records": verdicts,
            },
            "verifications": {
                "count": 0,
                "why_kept": "there are none — no money is involved in any of this",
            },
        },
    }

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=False)

    size = os.path.getsize(path)
    with open(path, encoding="utf-8") as handle:
        readback = json.load(handle)
    print(f"  wrote and re-read {size} bytes")

    check("B1  the archive round-trips through JSON unchanged",
          json.dumps(payload, sort_keys=True), json.dumps(readback, sort_keys=True))
    check("B2  it holds seven promises", 7, len(readback["removed"]))
    check("B3  every _id in the archive matches one still in the database",
          target_ids, {r["promise"]["_id"] for r in readback["removed"]})
    check("B4  every archived promise still reads back identically from MongoDB",
          [plain(p) for p in sorted(promises, key=lambda d: d["event_id"])],
          [r["promise"] for r in readback["removed"]],
          "compared field by field, not by count")

    # Field-set completeness: an archive missing a field is not a restore path.
    live_fields = {tuple(sorted(p.keys())) for p in promises}
    archived_fields = {tuple(sorted(r["promise"].keys())) for r in readback["removed"]}
    check("B5  no field was dropped in serialization", live_fields, archived_fields)

    if FAIL:
        print("\n" + "!" * 78)
        print("ARCHIVE VERIFICATION FAILED — nothing has been deleted. Stopping.")
        print("!" * 78)
        await close_mongo_connection()
        return 1

    # ------------------------------------------------------------------ #
    print("\n[C] DELETE — one document at a time, keyed on _id")
    # ------------------------------------------------------------------ #
    for p in sorted(promises, key=lambda d: d["event_id"]):
        result = await db.promises.delete_one({"_id": p["_id"]})
        check(f"C   deleted {p['event_id']} ({p['state']})", 1, result.deleted_count,
              f"_id={p['_id']}")

    # ------------------------------------------------------------------ #
    print("\n[D] RESTORE EVENT STATUS — disclosed raw $set, filtered on the old value")
    # ------------------------------------------------------------------ #
    for event_id in sorted(TARGETS):
        result = await db.events.update_one(
            {"event_id": event_id, "status": "awaiting_promise"},
            {"$set": {"status": "at_risk"}},
        )
        check(f"D   {event_id} awaiting_promise -> at_risk", 1, result.modified_count)

    # ------------------------------------------------------------------ #
    print("\n[E] VERIFY BY _id — not by count, because a count cannot rule out a swap")
    # ------------------------------------------------------------------ #
    remaining = {str(p["_id"]) async for p in db.promises.find({}, {"_id": 1})}
    check("E1  none of the seven _id values survives", set(), remaining & target_ids)
    check("E2  every _id that was meant to survive did, exactly", survivor_ids, remaining)
    check("E3  the collection holds the predicted number", 20, len(remaining))

    states_after = collections.Counter()
    async for p in db.promises.find({}, {"state": 1}):
        states_after[p["state"]] += 1
    check("E4  state distribution matches the prediction made before deleting",
          dict(expected_after), dict(states_after))

    for event_id in sorted(TARGETS):
        doc = await db.events.find_one({"event_id": event_id}, {"status": 1})
        n = await db.promises.count_documents({"event_id": event_id})
        ok = doc["status"] == "at_risk" and n == 0
        check(f"E5  {event_id}: at_risk with no promise", (True, "at_risk", 0),
              (ok, doc["status"], n))

    check("E6  the extraction records were not touched", len(extractions),
          await db.promise_extractions.count_documents(
              {"event_id": {"$in": list(TARGETS)}}))
    check("E7  the two executions were not touched", len(executions),
          await db.executions.count_documents({"event_id": {"$in": list(TARGETS)}}))
    check("E8  the policy verdicts were not touched", len(verdicts),
          await db.policy_verdicts.count_documents({"event_id": {"$in": list(TARGETS)}}))

    # ------------------------------------------------------------------ #
    print("\n[F] THE LIVE ENDPOINTS")
    # ------------------------------------------------------------------ #
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        ptp = (await client.get("/metrics/promise-to-pay")).json()
        check("F1  honor_rate is back to 72.73%", 72.73, ptp["honor_rate"])
        check("F2  total_promises is 20", 20, ptp["total_promises"])
        # Per-state, against the raw counts — the rate alone could be right for the
        # wrong reasons if two states had moved in compensating directions.
        check("F2b every per-state count on the endpoint matches the raw query",
              {k: expected_after[k] for k in ("honored", "broken", "promised",
                                              "reevaluating")},
              {k: ptp[k] for k in ("honored", "broken", "promised", "reevaluating")})
        check("F2c still_open is promised + reevaluating",
              ptp["promised"] + ptp["reevaluating"], ptp["still_open"])

        summary = (await client.get("/metrics/summary")).json()
        check("F3  awaiting_promise fell by exactly 7",
              summary_before["events_by_status"]["awaiting_promise"] - 7,
              summary["events_by_status"]["awaiting_promise"])
        check("F4  at_risk rose by exactly 7 — so all seven came from at_risk",
              summary_before["events_by_status"]["at_risk"] + 7,
              summary["events_by_status"]["at_risk"])
        check("F5  NO money figure moved",
              {k: summary_before[k] for k in FROZEN},
              {k: summary[k] for k in FROZEN})

        # A removed promise must now 404 rather than half-exist.
        r = await client.post("/promises/demo_188_rcv/check")
        check("F6  checking a removed promise 404s", 404, r.status_code,
              str(r.json().get("detail"))[:120])
        r = await client.get("/promises", params={"event_id": "demo_188_rcv"})
        check("F7  ...and it is gone from GET /promises", (200, 0),
              (r.status_code, len(r.json())))

        # The event is promisable again, which is what at_risk means.
        r = await client.get("/audit-trail/demo_188_rcv")
        audit = r.json()
        check("F8  the audit trail shows no promise and does not error",
              (200, 0), (r.status_code, audit["record_counts"]["promises"]))
        print(f"       record_counts={audit['record_counts']}")

        for path_ in ("/metrics/summary", "/metrics/by-intervention",
                      "/metrics/by-root-cause", "/metrics/promise-to-pay",
                      "/metrics/baseline-comparison"):
            r = await client.get(path_)
            check(f"F9  {path_} still 200", 200, r.status_code)

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed")
    print(f"archive: {path} ({size} bytes)")
    print("=" * 78)
    await close_mongo_connection()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
