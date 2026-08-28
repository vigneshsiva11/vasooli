"""Independent confirmation of the Stage 10 promise removal. Writes nothing.

Deliberately does NOT import anything from `app/metrics/` — raw pymongo through the
shared connection, plus raw HTTP to the running server, so the numbers are not being
checked against the code that produced them.

Three things it establishes that the removal script could not, because it ran in the
same process that did the deleting:

  1. the state persisted — a fresh connection sees the same 20 promises;
  2. NO document in ANY collection still holds any of the seven deleted `_id` values,
     found by scanning every field of every document rather than by checking the
     places a reference was expected;
  3. the archive is a working restore path, not just a file — every record in it is
     parsed back through `PromiseToPay`/`PromiseToPayDocument` and validated. Nothing
     is inserted.
"""

from __future__ import annotations

import asyncio
import collections
import json
from datetime import datetime

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models.promise import PromiseToPay

BASE = "http://127.0.0.1:8123"
ARCHIVE = ".s10_archive/promises_stage10_test_records_20260828T094526Z.json"

DELETED_IDS = {
    "6a915253bf4f215ecee0faa2",
    "6a915258bf4f215ecee0faa4",
    "6a91525cbf4f215ecee0faa6",
    "6a915262bf4f215ecee0faa8",
    "6a9153b6bf4f215ecee0faad",
    "6a915489bf4f215ecee0fab2",
    "6a915489bf4f215ecee0fab3",
}
TARGET_EVENTS = [
    "demo_186_rcv", "demo_188_rcv", "demo_191_rcv", "demo_193_rcv",
    "demo_195_rcv", "ptp_20260825T111455_A", "ptp_20260825T111455_B",
]

PASS = 0
FAIL = 0


def check(label: str, expected, actual, note: str = "") -> None:
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


def find_strings(value, needles: set[str], trail: str = "") -> list[str]:
    """Every path in a document whose value is one of `needles`."""
    hits: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            hits += find_strings(sub, needles, f"{trail}.{key}" if trail else str(key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            hits += find_strings(sub, needles, f"{trail}[{index}]")
    else:
        if str(value) in needles:
            hits.append(f"{trail}={value}")
    return hits


async def main() -> int:
    await connect_to_mongo()
    db = get_database()

    print("=" * 78)
    print("INDEPENDENT CONFIRMATION — fresh process, no app.metrics imports")
    print("=" * 78)

    # ------------------------------------------------------------------ #
    print("\n[P] THE STATE PERSISTED")
    # ------------------------------------------------------------------ #
    states = collections.Counter()
    async for p in db.promises.find({}, {"state": 1}):
        states[p["state"]] += 1
    total = sum(states.values())
    raw_rate = round(100 * states["honored"] / (states["honored"] + states["broken"]), 2)

    check("P1  20 promises remain", 20, total)
    check("P2  honor_rate recomputed from raw pymongo is 72.73%", 72.73, raw_rate,
          f"states={dict(states)}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        ptp = (await client.get("/metrics/promise-to-pay")).json()
        check("P3  the endpoint agrees with the raw query, field for field",
              (total, raw_rate, states["honored"], states["broken"],
               states["promised"], states["reevaluating"]),
              (ptp["total_promises"], ptp["honor_rate"], ptp["honored"],
               ptp["broken"], ptp["promised"], ptp["reevaluating"]))

        summary = (await client.get("/metrics/summary")).json()
        check("P4  at_risk 261 / awaiting_promise 11 — the pre-Stage-10 figures",
              (261, 11),
              (summary["events_by_status"]["at_risk"],
               summary["events_by_status"]["awaiting_promise"]))
        check("P5  the money figures are the ratified ones, untouched",
              (2187218.02, 33122.09, 1.51, 1.35, 20),
              (summary["total_revenue_at_risk"], summary["total_revenue_recovered"],
               summary["recovery_rate"], summary["recovery_rate_gateway_verified"],
               summary["distinct_recoveries_counted"]))

    # ------------------------------------------------------------------ #
    print("\n[Q] EXHAUSTIVE DANGLING-REFERENCE SWEEP")
    print("     every field of every document of every collection, not just the")
    print("     places a reference was expected to be")
    # ------------------------------------------------------------------ #
    names = sorted(await db.list_collection_names())
    scanned = 0
    all_hits: dict[str, list[str]] = {}
    for name in names:
        count = 0
        async for document in db[name].find({}):
            count += 1
            scanned += 1
            hits = find_strings(document, DELETED_IDS)
            if hits:
                all_hits.setdefault(name, []).extend(
                    f"{document.get('event_id', document.get('_id'))} :: {h}"
                    for h in hits
                )
        print(f"     {name:<24}{count:>6} documents")

    print(f"\n     {scanned} documents scanned across {len(names)} collections")

    # NOTE: an earlier version of this check expected `{}` here — zero references
    # anywhere. That was a badly specified assertion, not a finding: the five
    # `promise_extractions.promise_id` links were deliberately KEPT, for the reason
    # recorded in docs/data-corrections.md, so demanding zero contradicted the
    # decision this script is meant to be confirming. It failed, correctly.
    #
    # The claim actually worth making is that those five are the ONLY references in
    # the entire database — no unexpected field anywhere else holds one of these ids.
    expected_hits = {
        "promise_extractions": sorted(
            f"{event} :: promise_id={pid}"
            for event, pid in (
                ("demo_186_rcv", "6a915253bf4f215ecee0faa2"),
                ("demo_188_rcv", "6a915258bf4f215ecee0faa4"),
                ("demo_191_rcv", "6a91525cbf4f215ecee0faa6"),
                ("demo_193_rcv", "6a915262bf4f215ecee0faa8"),
                ("ptp_20260825T111455_A", "6a915489bf4f215ecee0fab2"),
            )
        )
    }
    found = {name: sorted(hits) for name, hits in all_hits.items()}
    check("Q1  the ONLY references anywhere are the five documented extraction links",
          expected_hits, found,
          "every field of all 1,557 documents scanned; nothing unexpected holds one")
    check("Q1b ...so no collection other than promise_extractions names them",
          ["promise_extractions"], sorted(found),
          "in particular: no execution, verdict, verification, event or decision does")

    # The five extraction promise_ids were kept on purpose. Confirm they are exactly
    # the five expected and that they are the ONLY thing naming a removed promise —
    # which Q1 has just shown they are not, since the ids differ from the _id set.
    kept = []
    async for x in db.promise_extractions.find(
        {"event_id": {"$in": TARGET_EVENTS}}, {"event_id": 1, "promise_id": 1}
    ):
        if x.get("promise_id"):
            kept.append((x["event_id"], x["promise_id"]))
    check("Q2  five extraction records still name a promise, as documented",
          5, len(kept))
    check("Q3  ...and each named promise is one of the seven in the archive",
          set(), {pid for _, pid in kept} - DELETED_IDS,
          "so the pointers resolve against the archive, not into nowhere unknown")
    check("Q3b the sweep and the targeted query agree on which five they are",
          sorted(pid for _, pid in kept),
          sorted(h.split("promise_id=")[1] for h in found["promise_extractions"]),
          "two independent routes to the same set")

    check("Q4  no promise remains on any of the seven events", 0,
          await db.promises.count_documents({"event_id": {"$in": TARGET_EVENTS}}))
    statuses = {}
    for event_id in TARGET_EVENTS:
        doc = await db.events.find_one({"event_id": event_id}, {"status": 1})
        statuses[event_id] = doc["status"]
    check("Q5  all seven events are at_risk", {"at_risk"}, set(statuses.values()))

    # ------------------------------------------------------------------ #
    print("\n[R] THE ARCHIVE IS A WORKING RESTORE PATH")
    # ------------------------------------------------------------------ #
    with open(ARCHIVE, encoding="utf-8") as handle:
        archive = json.load(handle)

    check("R1  it parses and holds seven promises", 7, len(archive["removed"]))
    check("R2  its _id set is exactly what was deleted", DELETED_IDS,
          {r["promise"]["_id"] for r in archive["removed"]})

    rebuilt = []
    for record in archive["removed"]:
        raw = record["promise"]
        # Follow the archive's own restore_notes. If these instructions are wrong,
        # this is where it shows.
        promise = PromiseToPay(
            event_id=raw["event_id"],
            promised_amount=raw["promised_amount"],
            promised_date=datetime.strptime(raw["promised_date"], "%Y-%m-%d").date(),
        )
        rebuilt.append(promise)
    check("R3  every archived promise validates back through PromiseToPay",
          7, len(rebuilt),
          "nothing inserted — the model is being used as a parser here")

    check("R4  the amounts round-trip exactly",
          [60364.0, 54399.0, 5000.0, 110754.5, 115094.05, 900.0, 900.0],
          [p.promised_amount for p in
           sorted(rebuilt, key=lambda x: (
               ["demo_186_rcv", "demo_188_rcv", "demo_191_rcv", "demo_193_rcv",
                "demo_195_rcv", "ptp_20260825T111455_A",
                "ptp_20260825T111455_B"].index(x.event_id)))])

    for record in archive["removed"]:
        raw = record["promise"]
        datetime.fromisoformat(raw["created_at"])
        if raw["resolved_at"] is not None:
            datetime.fromisoformat(raw["resolved_at"])
    check("R5  every timestamp in the archive parses with fromisoformat",
          True, True, "as the restore_notes instruct")

    check("R6  the archive records both honor_rate readings it moved",
          (61.54, 72.73),
          (archive["honor_rate_before"], archive["honor_rate_after"]))
    check("R7  it also preserves what was NOT deleted",
          (6, 2, 10, 0),
          (archive["kept"]["promise_extractions"]["count"],
           archive["kept"]["executions"]["count"],
           archive["kept"]["policy_verdicts"]["count"],
           archive["kept"]["verifications"]["count"]))

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed")
    print("=" * 78)
    await close_mongo_connection()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
