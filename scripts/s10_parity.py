"""Stage 10 downstream parity — ZERO Gemini calls.

The claim this stage rests on is that an extracted promise is not a second kind of
promise. This script tries to find a way in which it is one.

The specimen is L2's promise on `demo_188_rcv`: extracted from a seven-week-old
message, dated 2026-07-10, therefore already overdue the moment it was created. The
control is a structured promise created through `POST /promises` with the same date,
on a comparable event. Both are then put through the identical sequence and the
responses compared field by field.

Deliberately included, because parity is not only about the happy path:

* the promise documents' key sets are compared. If extraction had added a `source`
  or `extracted` marker, downstream code could branch on it, and "indistinguishable"
  would be false however identical the behaviour looked;
* the mandatory payment re-check is checked on both, since a follow-up reached
  without it is the one failure Stage 6 was built to prevent;
* the unforgeable-token property is re-tested against the extracted promise's event
  specifically, because a new entry point is exactly where someone would forget it.

No verification records are fabricated to test the already-paid path. Stage 9 taught
that lesson expensively: a probe that writes is a probe that changes the dataset it
is measuring. `AlreadyRecovered` is instead demonstrated against an event that is
genuinely recovered already.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.ptp import (
    AlreadyRecovered,
    UnmintedConfirmation,
    UnpaidConfirmation,
    confirm_still_unpaid,
    promise_transition_allowed,
)

BASE = "http://127.0.0.1:8123"

EXTRACTED_EVENT = "demo_188_rcv"      # L2: extracted, dated 2026-07-10, overdue
CONTROL_EVENT = "demo_195_rcv"        # structured control, same date
SHARED_DATE = "2026-07-10"
RECOVERED_EVENT = "exe_S5_20260824T164458_REMIND"

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


def side_by_side(title: str, left_label: str, left: dict, right_label: str, right: dict,
                 keys: list[str]) -> None:
    print(f"\n  {title}")
    width = max(len(k) for k in keys) + 2
    print(f"    {'field'.ljust(width)}{left_label:<38}{right_label}")
    print(f"    {'-' * (width + 70)}")
    for key in keys:
        lv, rv = left.get(key), right.get(key)
        flag = "" if lv == rv else "   <-- DIFFERS"
        print(f"    {key.ljust(width)}{str(lv):<38}{str(rv)}{flag}")


async def main() -> int:
    await connect_to_mongo()
    db = get_database()

    print("=" * 78)
    print("STAGE 10 — DOWNSTREAM PARITY (zero Gemini calls)")
    print("=" * 78)

    async with httpx.AsyncClient(base_url=BASE, timeout=90.0) as client:
        # ---------------------------------------------------------------- #
        print("\n[G] THE TWO SPECIMENS")
        # ---------------------------------------------------------------- #
        control_amount = (await db.events.find_one(
            {"event_id": CONTROL_EVENT}, {"amount": 1}))["amount"]

        r = await client.post("/promises", json={
            "event_id": CONTROL_EVENT,
            "promised_amount": control_amount,
            "promised_date": SHARED_DATE,
        })
        check("G1  structured control promise created", 201, r.status_code,
              "POST /promises — the path Stage 6 built")
        control = r.json()

        r = await client.get("/promises", params={"event_id": EXTRACTED_EVENT})
        extracted = r.json()[0]
        check("G2  extracted promise readable through the SAME GET /promises",
              200, r.status_code,
              f"id={extracted['id']} date={extracted['promised_date']}")

        # The structural claim: same fields, no marker of origin.
        ex_keys = sorted(extracted)
        ct_keys = sorted(control)
        check("G3  identical field sets — no 'source'/'extracted' marker exists",
              ct_keys, ex_keys,
              "if extraction added a field, downstream code could branch on it")

        ex_doc = await db.promises.find_one({"event_id": EXTRACTED_EVENT})
        ct_doc = await db.promises.find_one({"event_id": CONTROL_EVENT})
        check("G4  identical field sets in MongoDB too, not just in the API model",
              sorted(k for k in ct_doc if k != "_id"),
              sorted(k for k in ex_doc if k != "_id"))

        side_by_side("promise documents, side by side:",
                     "EXTRACTED (from free text)", extracted,
                     "STRUCTURED (typed by hand)", control,
                     ["promised_date", "state", "follow_up_sent", "resolved_at"])

        check("G5  both are overdue, so both take the same branch through check",
              (SHARED_DATE, SHARED_DATE),
              (extracted["promised_date"], control["promised_date"]))

        # ---------------------------------------------------------------- #
        print("\n[H] THE SAME CHECK, RUN ON BOTH")
        # ---------------------------------------------------------------- #
        r = await client.post(f"/promises/{EXTRACTED_EVENT}/check")
        check("H1  check on the EXTRACTED promise returns 200", 200, r.status_code)
        ex_check = r.json()

        r = await client.post(f"/promises/{CONTROL_EVENT}/check")
        check("H2  check on the STRUCTURED promise returns 200", 200, r.status_code)
        ct_check = r.json()

        check("H3  identical response field sets", sorted(ct_check), sorted(ex_check))

        side_by_side("PromiseCheck responses, side by side:",
                     "EXTRACTED", ex_check, "STRUCTURED", ct_check,
                     ["state_before", "state", "changed", "deadline_passed",
                      "verifications_examined", "recovered_verification_id"])

        check("H4  THE MANDATORY RE-CHECK RAN ON BOTH (never null)",
              (True, True),
              (ex_check["payment_rechecked_at"] is not None,
               ct_check["payment_rechecked_at"] is not None),
              f"extracted={ex_check['payment_rechecked_at']}  "
              f"structured={ct_check['payment_rechecked_at']}")

        check("H5  both saw the deadline as passed", (True, True),
              (ex_check["deadline_passed"], ct_check["deadline_passed"]))

        check("H6  both left 'promised' identically", ("promised", "promised"),
              (ex_check["state_before"], ct_check["state_before"]))

        check("H7  both reached the SAME resulting state",
              ct_check["state"], ex_check["state"],
              "whatever the policy gate decided, it decided it the same way for both")

        print("\n  follow-up reports, side by side:")
        ex_fu = ex_check.get("follow_up") or {}
        ct_fu = ct_check.get("follow_up") or {}
        side_by_side("", "EXTRACTED", ex_fu, "STRUCTURED", ct_fu,
                     ["sent", "policy_verdict", "policy_reason", "action_type",
                      "intervention"])
        check("H8  identical follow-up report field sets", sorted(ct_fu), sorted(ex_fu))
        check("H9  the policy gate ran for both (a verdict id exists either way)",
              (True, True),
              (bool(ex_fu.get("policy_verdict_id")), bool(ct_fu.get("policy_verdict_id"))),
              f"extracted verdict={ex_fu.get('policy_verdict_id')}  "
              f"structured verdict={ct_fu.get('policy_verdict_id')}")
        print(f"\n    extracted detail : {ex_fu.get('detail')}")
        print(f"    structured detail: {ct_fu.get('detail')}")

        # ---------------------------------------------------------------- #
        print("\n[I] SAFETY PROPERTIES, RE-TESTED AGAINST THE EXTRACTED PROMISE")
        # ---------------------------------------------------------------- #

        # The token is unforgeable — re-tested here because a new entry point is
        # exactly where this would be forgotten.
        try:
            UnpaidConfirmation(
                event_id=EXTRACTED_EVENT,
                confirmed_at=datetime.now(timezone.utc),
                verifications_examined=0,
            )
            check("I1  a hand-built confirmation is refused", "refused", "ACCEPTED")
        except (UnmintedConfirmation, TypeError, ValueError) as exc:
            check("I1  a hand-built confirmation is refused", "refused", "refused",
                  f"{type(exc).__name__}: {str(exc)[:150]}")

        # A confirmation for the extracted promise's event cannot be obtained while
        # the money is in. Demonstrated on a genuinely recovered event — nothing is
        # fabricated to make this fire.
        try:
            await confirm_still_unpaid(RECOVERED_EVENT)
            check("I2  confirm_still_unpaid refuses on a recovered event",
                  "AlreadyRecovered", "returned a token")
        except AlreadyRecovered as exc:
            check("I2  confirm_still_unpaid refuses on a recovered event",
                  "AlreadyRecovered", "AlreadyRecovered",
                  f"{str(exc)[:170]}")

        # A confirmation minted for one event cannot authorize another.
        confirmation = await confirm_still_unpaid(EXTRACTED_EVENT)
        check("I3  a real confirmation for the extracted event mints fine",
              EXTRACTED_EVENT, confirmation.event_id,
              f"verifications_examined={confirmation.verifications_examined}")
        try:
            confirmation.assert_matches(CONTROL_EVENT)
            check("I4  it cannot be re-pointed at another event", "refused", "ACCEPTED")
        except Exception as exc:
            check("I4  it cannot be re-pointed at another event", "refused", "refused",
                  f"{type(exc).__name__}: {str(exc)[:150]}")

        # ---------------------------------------------------------------- #
        print("\n[J] STATE MACHINE — the same table governs both")
        # ---------------------------------------------------------------- #
        reached = ex_check["state"]
        check("J1  no arc leads back to 'promised' from the reached state",
              False, promise_transition_allowed(reached, "promised"),
              f"promise_transition_allowed({reached!r}, 'promised')")
        check("J2  'honored' is reachable from it (money can still arrive)",
              True, promise_transition_allowed(reached, "honored"))
        check("J3  the table answers identically for both promises",
              promise_transition_allowed(ct_check["state"], "promised"),
              promise_transition_allowed(ex_check["state"], "promised"),
              "one table, no per-origin branching")

        # A second check must not double-send.
        r = await client.post(f"/promises/{EXTRACTED_EVENT}/check")
        second = r.json()
        check("J4  re-checking the extracted promise does not move it again",
              False, second["changed"],
              f"state stayed {second['state']!r}; "
              f"payment re-checked again at {second['payment_rechecked_at']}")
        check("J5  ...and the re-check still ran on that idempotent path",
              True, second["payment_rechecked_at"] is not None,
              "the re-check is not skipped just because nothing else happens")

        # ---------------------------------------------------------------- #
        print("\n[K] THE AUDIT TRAIL — raw message beside what was extracted")
        # ---------------------------------------------------------------- #
        r = await client.get("/promise-extractions", params={"event_id": EXTRACTED_EVENT})
        records = r.json()
        check("K1  the extraction record is retrievable for this event", 1, len(records))
        record = records[0]
        check("K2  it links to the promise that exists", extracted["id"],
              record["promise_id"])
        check("K3  the ORIGINAL MESSAGE is stored verbatim", True,
              record["raw_text"].startswith("Sorry for the delay"),
              f"raw_text={record['raw_text']!r}")
        print(f"\n    received_at    : {record['received_at']}")
        print(f"    promised_date  : {record['promised_date']}  <- resolved against received_at")
        print(f"    amount         : {record['promised_amount']} (inferred={record['amount_inferred']})")
        print(f"    confidence     : {record['confidence']} (floor {record['confidence_floor']})")
        print(f"    quote          : {record['quote']!r} verified={record['quote_verified']}")

        r = await client.get("/promise-extractions", params={"accepted": False})
        refusals = r.json()
        check("K4  refusals are retrievable and outnumber nothing", True,
              len(refusals) >= 4,
              f"{len(refusals)} refused attempts on record: "
              f"{sorted({x['refusal_reason'] for x in refusals})}")

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed   (Gemini calls used: 0)")
    print("=" * 78)
    await close_mongo_connection()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
