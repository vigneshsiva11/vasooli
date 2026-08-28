"""Stage 10 executed-follow-up parity — the 9th and final call of the ratified budget.

The main parity run left one gap, and it is worth naming rather than glossing: both
specimens there carried large amounts, so the policy gate returned
`requires_manual_review` and neither follow-up was actually sent. Parity was proven
through the gate but not through an execution.

This closes it with a controlled pair. `ptp_20260825T111455_A` and `..._B` are both
INR 900, both carry a decision recommending `escalating_reminder_sequence`, and both
have zero executions. Same amount, same intervention, same contact-cap headroom — so
after one promise is extracted from free text and the other typed in, the only
remaining difference between the two events is how the promise came to exist. If the
executed follow-ups match, origin demonstrably does not reach the executor.

Both amounts sit under `AUTO_AUTHORIZE_BELOW` (5000), so the gate authorizes and a
contact really is executed. `escalating_reminder_sequence` renders a deterministic
email template — no payment link is created, so none of the four remaining Razorpay
test-mode slots is consumed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database

BASE = "http://127.0.0.1:8123"

EXTRACTED_EVENT = "ptp_20260825T111455_A"
CONTROL_EVENT = "ptp_20260825T111455_B"
OLD = datetime(2026, 7, 6, 9, 15, tzinfo=timezone.utc)  # Monday; "Friday" -> 2026-07-10
TEXT = "Sorry for the delay, I'll pay by Friday, just had a cash flow issue."

PASS = 0
FAIL = 0


def check(label: str, expected, actual, note: str = "") -> None:
    global PASS, FAIL
    ok = expected == actual
    PASS_FAIL = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{PASS_FAIL}] {label}")
    print(f"         expected={expected!r}")
    print(f"         actual  ={actual!r}")
    if note:
        print(f"         {note}")


async def main() -> int:
    await connect_to_mongo()
    db = get_database()

    print("=" * 78)
    print("STAGE 10 — EXECUTED FOLLOW-UP PARITY (1 Gemini call: the last of 9)")
    print(f"  extracted specimen : {EXTRACTED_EVENT}")
    print(f"  structured control : {CONTROL_EVENT}")
    print("=" * 78)

    for event_id in (EXTRACTED_EVENT, CONTROL_EVENT):
        e = await db.events.find_one({"event_id": event_id}, {"amount": 1, "status": 1})
        d = await db.decisions.find_one({"event_id": event_id}, sort=[("version", -1)])
        print(f"  {event_id}: amount={e['amount']} status={e['status']} "
              f"intervention={d['recommended_intervention']!r}")

    async with httpx.AsyncClient(base_url=BASE, timeout=90.0) as client:
        print("\n[L] CREATE ONE OF EACH")

        r = await client.post("/promises/from-text", json={
            "event_id": EXTRACTED_EVENT,
            "raw_text": TEXT,
            "received_at": OLD.isoformat(),
        })
        body = r.json()
        check("L1  extracted promise created from free text", 201, r.status_code,
              f"date={body['promise']['promised_date']} "
              f"amount={body['promise']['promised_amount']} "
              f"inferred={body['promised_amount_inferred']}")
        extracted = body["promise"]

        amount = (await db.events.find_one(
            {"event_id": CONTROL_EVENT}, {"amount": 1}))["amount"]
        r = await client.post("/promises", json={
            "event_id": CONTROL_EVENT,
            "promised_amount": amount,
            "promised_date": extracted["promised_date"],
        })
        check("L2  structured control created with the SAME date and amount",
              201, r.status_code)
        control = r.json()

        check("L3  the two promises differ in nothing but event and id",
              (extracted["promised_date"], extracted["promised_amount"],
               extracted["state"], extracted["follow_up_sent"]),
              (control["promised_date"], control["promised_amount"],
               control["state"], control["follow_up_sent"]))

        print("\n[M] CHECK BOTH — this time the gate should authorize")

        r = await client.post(f"/promises/{EXTRACTED_EVENT}/check")
        ex = r.json()
        r = await client.post(f"/promises/{CONTROL_EVENT}/check")
        ct = r.json()

        ex_fu, ct_fu = ex.get("follow_up") or {}, ct.get("follow_up") or {}

        print("\n  PromiseCheck + follow-up, side by side:")
        rows = [
            ("state_before", ex["state_before"], ct["state_before"]),
            ("state", ex["state"], ct["state"]),
            ("changed", ex["changed"], ct["changed"]),
            ("deadline_passed", ex["deadline_passed"], ct["deadline_passed"]),
            ("payment_rechecked", ex["payment_rechecked_at"] is not None,
             ct["payment_rechecked_at"] is not None),
            ("follow_up.sent", ex_fu.get("sent"), ct_fu.get("sent")),
            ("policy_verdict", ex_fu.get("policy_verdict"), ct_fu.get("policy_verdict")),
            ("policy_reason", ex_fu.get("policy_reason"), ct_fu.get("policy_reason")),
            ("intervention", ex_fu.get("intervention"), ct_fu.get("intervention")),
            ("action_type", ex_fu.get("action_type"), ct_fu.get("action_type")),
        ]
        print(f"    {'field':<22}{'EXTRACTED':<34}{'STRUCTURED'}")
        print(f"    {'-' * 90}")
        for name, lv, rv in rows:
            flag = "" if lv == rv else "   <-- DIFFERS"
            print(f"    {name:<22}{str(lv):<34}{str(rv)}{flag}")

        check("M1  A FOLLOW-UP WAS ACTUALLY SENT for both", (True, True),
              (ex_fu.get("sent"), ct_fu.get("sent")),
              "this is the path the large-amount pair could not reach")
        check("M2  both were authorized by the gate, identically",
              (ct_fu.get("policy_verdict"), ct_fu.get("policy_reason")),
              (ex_fu.get("policy_verdict"), ex_fu.get("policy_reason")))
        check("M3  the SAME intervention and action type executed",
              (ct_fu.get("intervention"), ct_fu.get("action_type")),
              (ex_fu.get("intervention"), ex_fu.get("action_type")))
        check("M4  both promises reached the same state after a real send",
              ct["state"], ex["state"],
              "reevaluating = broken, then chased")
        check("M5  the mandatory re-check ran on both before the send",
              (True, True),
              (ex["payment_rechecked_at"] is not None,
               ct["payment_rechecked_at"] is not None))

        print("\n[N] THE EXECUTION RECORDS THEMSELVES")

        ex_exe = await db.executions.find_one({"event_id": EXTRACTED_EVENT})
        ct_exe = await db.executions.find_one({"event_id": CONTROL_EVENT})
        check("N1  an execution record exists for each", (True, True),
              (ex_exe is not None, ct_exe is not None))
        check("N2  identical execution field sets",
              sorted(k for k in ct_exe if k != "_id"),
              sorted(k for k in ex_exe if k != "_id"),
              "nothing on the execution says where the promise came from")

        # NOTE: an earlier version of this script probed `template_id`, `channel` and
        # `payment_link_id` here. None of those is a field on an execution record, so
        # both sides returned None, compared equal, and the checks reported PASS while
        # asserting nothing. The real names are below — read them off N2's output
        # rather than guessing them, which is how the mistake was caught.
        check("N3  same status, action type and intervention",
              (ct_exe.get("status"), ct_exe.get("action_type"),
               ct_exe.get("intervention")),
              (ex_exe.get("status"), ex_exe.get("action_type"),
               ex_exe.get("intervention")))
        check("N4  contact_channel matches AND is populated on both",
              (ct_exe.get("contact_channel"), True),
              (ex_exe.get("contact_channel"),
               ex_exe.get("contact_channel") is not None),
              "populated, so this is a real comparison rather than None == None")
        check("N5  the rendered message is identical, not merely present",
              ct_exe.get("contact_message_summary"),
              ex_exe.get("contact_message_summary"),
              "same template, same amount — the body does not know the origin either")
        check("N6  NO payment link on either — no Razorpay slot spent",
              (None, None, None, None),
              (ex_exe.get("razorpay_payment_link_id"),
               ex_exe.get("razorpay_payment_link_url"),
               ct_exe.get("razorpay_payment_link_id"),
               ct_exe.get("razorpay_payment_link_url")),
              "contact-type intervention, deterministic template")

        # The strongest form of the claim: the whole record, minus only the fields
        # that MUST differ because they identify the event, the moment and the verdict.
        must_differ = {
            "_id", "event_id", "executed_at", "policy_verdict_id",
        }
        check("N7  ENTIRE execution records equal, ignoring only id/event/time/verdict",
              {k: v for k, v in ct_exe.items() if k not in must_differ},
              {k: v for k, v in ex_exe.items() if k not in must_differ})

        print("\n[O] THE PROMISE DOCUMENTS AFTER A REAL FOLLOW-UP")
        ex_doc = await db.promises.find_one({"event_id": EXTRACTED_EVENT})
        ct_doc = await db.promises.find_one({"event_id": CONTROL_EVENT})
        check("O1  follow_up_sent is now True on both", (True, True),
              (ex_doc["follow_up_sent"], ct_doc["follow_up_sent"]))
        check("O2  both carry a resolution time (they left the open state)",
              (True, True),
              (ex_doc["resolved_at"] is not None, ct_doc["resolved_at"] is not None))
        check("O3  identical states", ct_doc["state"], ex_doc["state"])

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed   (Gemini calls used: 1)")
    print("=" * 78)
    await close_mongo_connection()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
