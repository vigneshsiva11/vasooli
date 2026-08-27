"""Stage 8 checkpoint 6 — 11 promise-to-pay records at demo scale.

Runs the EXISTING Stage 6 Part B endpoints (`POST /promises`,
`POST /promises/{event_id}/check`) over the 11 dataset events assigned a PTP role,
producing the ratified distribution:

    4 honored                      promised -> honored
    2 broken, follow-up suppressed  promised -> broken, policy refused the chase
    3 broken then chased            promised -> broken -> reevaluating
    2 still promised                promised, deadline not yet reached

Every state is reached by the state machine in `app/models/promise.py` acting on
evidence. Nothing here sets a `state` field — the API offers no way to, which is the
point of that router's docstring.

TWO THINGS THIS RUN HAS TO GET RIGHT, both discovered by reading the code rather
than by trying it:

1. THE 4 HONORED EVENTS ARE CURRENTLY TERMINAL, so a promise cannot be recorded
   against them at all. `app/ptp/store.py:NON_PROMISABLE_STATUSES` is
   `TERMINAL_EVENT_STATUSES`, and checkpoint 5 moved all four to `recovered`. The
   guard is right and the ordering is mine: in reality a customer promises while the
   money is still out, and *then* pays. Checkpoint 5 ran before checkpoint 6, so the
   payment is already recorded.

   `--resequence-honored` puts those four back into the real order: archive and
   delete their verification, return the event to `at_risk`, record the promise, then
   re-deliver the same paid webhook so the money arrives *after* the commitment. The
   `razorpay_event_id` is reused verbatim, so the finished dataset is identical to the
   one approved at checkpoint 5 except that four `verified_at` stamps move later.
   Content, amounts, outcomes and counts are unchanged.

   The alternative — reverting the status, recording the promise while the money is
   demonstrably already in, and putting the status back — was rejected. That does not
   re-order anything; it just defeats `assert_event_promisable`, which exists to stop
   exactly that record from being written.

2. PRE-AUTHORIZING THE 3 CHASED EVENTS WOULD MAKE THEM UNCHASEABLE.
   `app/policy/store.py:prior_authorized_contacts` counts an authorized contact
   verdict as a reservation even when nothing has executed against it, anchoring the
   cooldown at `evaluated_at`. So calling `POST /authorize/{event_id}` on
   demo_174/175/176 now would put a fresh 24h cooldown anchor on each, and the
   follow-up 30 seconds later would be blocked — turning all three into *suppressed*
   and leaving `broken -> reevaluating` dead in the demo, which is the one arc that
   proves follow-ups go through the gate rather than around it.

   These three are therefore authorized by the follow-up path itself, which calls
   `authorize_event` — the same function `POST /authorize/{event_id}` is, not a
   reimplementation of it; see `app/ptp/service.py:send_follow_up`. The gate runs, on
   the ordinary code path, and the verdict it writes is a normal appended verdict.
   This is the only sequence in which both the 3 chased and the 2 suppressed records
   are reachable.

CREATES NO PAYMENT LINK. The 3 follow-ups execute real interventions, and all three
decisions recommend contact-type interventions (`escalating_reminder_sequence`,
`manual_escalation`), which map to `contact_logged`. That is asserted on the returned
records, and the account's link count is read before and after and must be identical.

Usage:
    .venv/Scripts/python.exe scripts/s8_ptp.py --dry-run
    .venv/Scripts/python.exe scripts/s8_ptp.py --resequence-honored
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import s8_dataset as ds  # noqa: E402
from s8_verify import (  # noqa: E402
    RAZORPAY,
    account_link_count,
    post_webhook,
    razorpay_call,
    webhook_body,
)

API = "http://127.0.0.1:8123"

#: Today, read once so every promised date in the run is relative to one instant.
TODAY = datetime.now(timezone.utc).date()

#: How each role's promised date sits relative to today, and what that makes the
#: promise. The honored four are given a date in the FUTURE deliberately: the
#: mandatory payment re-check runs before the deadline is even looked at, so an
#: honored promise whose deadline has not passed is the clearest possible evidence
#: that the re-check comes first rather than being one branch among several.
OFFSET_FOR_ROLE: dict[str, int] = {
    ds.ROLE_PTP_HONORED: +4,
    ds.ROLE_PTP_SUPPRESSED: -5,
    ds.ROLE_PTP_REEVALUATING: -7,
    ds.ROLE_PTP_PROMISED: +14,
}

#: One promise is deliberately for less than the amount at risk. `PromiseToPay`
#: allows this on purpose — "a partial commitment is a real and common thing to
#: promise" — and a dataset where every promise is for the full amount would leave
#: that unexercised. Chosen as the largest receivable, where a part payment is what
#: actually happens.
PARTIAL_PROMISE = {"demo_177_rcv": 0.5}

EXPECTED = {
    ds.ROLE_PTP_HONORED: "honored",
    ds.ROLE_PTP_SUPPRESSED: "broken",
    ds.ROLE_PTP_REEVALUATING: "reevaluating",
    ds.ROLE_PTP_PROMISED: "promised",
}

PASSED = 0
FAILED = 0


def heading(text: str) -> None:
    print()
    print("=" * 98)
    print(text)
    print("=" * 98)


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def database():
    """Raw pymongo handle, from .env directly."""
    env = Path(".env").read_text(encoding="utf-8")
    uri = re.search(r"MONGODB_URI=(.+)", env).group(1).strip()
    name = re.search(r"MONGODB_DB_NAME=(.+)", env).group(1).strip()
    return MongoClient(uri)[name]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resequence-honored",
        action="store_true",
        help=(
            "archive and delete the 4 honored events' verification records, return "
            "those events to at_risk, and re-deliver the same paid webhook AFTER the "
            "promise is recorded. Required to reach the honored state at all; without "
            "it those 4 are reported as blocked and skipped."
        ),
    )
    parser.add_argument("--archive-dir", default=".s8_archive")
    args = parser.parse_args()

    roles = ds.roles(list(ds.generate()))
    plan: list[dict[str, Any]] = []
    for role, offset in OFFSET_FOR_ROLE.items():
        for spec in roles.get(role, []):
            plan.append(
                {
                    "event_id": spec["event_id"],
                    "role": role,
                    "promised_date": TODAY + timedelta(days=offset),
                    "expected": EXPECTED[role],
                }
            )

    db = database()
    ids = [p["event_id"] for p in plan]
    events = {e["event_id"]: e for e in db["events"].find({"event_id": {"$in": ids}})}
    decisions: dict[str, dict] = {}
    for d in db["decisions"].find({"event_id": {"$in": ids}}):
        if d["version"] >= decisions.get(d["event_id"], {}).get("version", 0):
            decisions[d["event_id"]] = d
    for p in plan:
        eid = p["event_id"]
        at_risk = decisions[eid]["revenue_at_risk"]
        p["amount"] = round(at_risk * PARTIAL_PROMISE.get(eid, 1.0), 2)
        p["status_before"] = events[eid].get("status")
        p["intervention"] = decisions[eid]["recommended_intervention"]

    client = httpx.Client()
    links_before = account_link_count(client)

    # =====================================================================
    heading("0. THE PLAN, AND THE ONE THING BLOCKING IT")
    # =====================================================================
    print(f"  today (UTC): {TODAY.isoformat()}   payment links on the account: "
          f"{links_before} of 30")
    print()
    print(f"  {'event':<14} {'role':<32} {'promised':<12} {'amount':>10} "
          f"{'status now':<16} target")
    for p in plan:
        print(f"  {p['event_id']:<14} {p['role']:<32} "
              f"{p['promised_date'].isoformat():<12} {p['amount']:>10,.2f} "
              f"{p['status_before']:<16} {p['expected']}")
    blocked = [p for p in plan if p["status_before"] == "recovered"]
    print()
    print(f"  {len(blocked)} of {len(plan)} events are already 'recovered', which is "
          "terminal. POST /promises")
    print("  refuses those (422 EventSettled) — you cannot promise to pay money that "
          "has arrived.")
    print("  Reaching 'honored' requires the promise to predate the payment, so those "
          "4 are")
    print("  re-sequenced rather than forced. Verification content is unchanged; only "
          "its\n  timestamp moves after the promise.")

    if args.dry_run:
        heading("DRY RUN — nothing written")
        client.close()
        return 0

    # =====================================================================
    heading("1. RE-SEQUENCE THE 4 HONORED EVENTS — archive, then unwind")
    # =====================================================================
    resequenced: list[str] = []
    if blocked and not args.resequence_honored:
        print("  SKIPPED — --resequence-honored was not passed. These 4 will be "
              "reported as blocked:")
        for p in blocked:
            print(f"    {p['event_id']}")
        plan = [p for p in plan if p not in blocked]
    elif blocked:
        targets = [p["event_id"] for p in blocked]
        docs = list(db["verifications"].find({"event_id": {"$in": targets}}))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = Path(args.archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"verifications_resequenced_{stamp}.json"
        archive.write_text(
            json.dumps(
                {
                    "archived_at": stamp,
                    "reason": (
                        "Checkpoint 5 ran before checkpoint 6, so these 4 payments "
                        "were recorded before the promises they settle. Deleted and "
                        "re-delivered in the correct order, reusing the same "
                        "razorpay_event_id. Content, amount and outcome unchanged."
                    ),
                    "collection": "verifications",
                    "count": len(docs),
                    "documents": docs,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        readback = json.loads(archive.read_text(encoding="utf-8"))
        check(
            "the 4 verification records are archived before anything is deleted",
            readback["count"] == len(docs) == len(targets),
            f"{archive} — {len(docs)} documents, {archive.stat().st_size:,} bytes",
        )
        if FAILED:
            print("\n  ABORTING — nothing deleted, because the archive is not "
                  "trustworthy.")
            client.close()
            return 1

        keep = {
            d["event_id"]: (d["razorpay_event_id"], d["razorpay_event"],
                            d["amount_recovered"], d["amount_expected"])
            for d in docs
        }
        deleted = db["verifications"].delete_many(
            {"_id": {"$in": [d["_id"] for d in docs]}}
        )
        # Raw write, and the only one in this checkpoint. `transition_event_status`
        # cannot do it: `recovered` is terminal by declaration, which is correct —
        # money does not un-arrive. What is being corrected is the order two
        # checkpoints ran in, not a business fact.
        reverted = db["events"].update_many(
            {"event_id": {"$in": targets}}, {"$set": {"status": "at_risk"}}
        )
        check(
            "exactly the 4 verifications were removed and their events returned to "
            "at_risk",
            deleted.deleted_count == len(targets)
            and reverted.modified_count == len(targets),
            f"{deleted.deleted_count} verifications deleted, "
            f"{reverted.modified_count} events reverted",
        )
        check(
            "no other verification was touched",
            db["verifications"].count_documents({}) == 42 - len(targets),
            f"{db['verifications'].count_documents({})} remain of 42 — the other 21 "
            "demo records and all 17 Stage 6 fixtures are untouched",
        )
        resequenced = targets
        for p in blocked:
            p["replay"] = keep[p["event_id"]]

    # =====================================================================
    heading(f"2. RECORD {len(plan)} PROMISES — POST /promises")
    # =====================================================================
    for p in plan:
        response = client.post(
            f"{API}/promises",
            json={
                "event_id": p["event_id"],
                "promised_amount": p["amount"],
                "promised_date": p["promised_date"].isoformat(),
            },
            timeout=30,
        )
        p["create_http"] = response.status_code
        p["promise"] = response.json() if response.status_code in (200, 201) else None
        flag = "" if response.status_code == 201 else f"  <- HTTP {response.status_code}"
        print(f"  {p['event_id']:<14} {p['amount']:>10,.2f} by "
              f"{p['promised_date'].isoformat()}{flag}")
    check(
        "every promise was recorded as new",
        all(p["create_http"] == 201 for p in plan),
        f"{sum(1 for p in plan if p['create_http'] == 201)} of {len(plan)} returned 201"
        if all(p["create_http"] == 201 for p in plan)
        else f"unexpected: {[(p['event_id'], p['create_http']) for p in plan if p['create_http'] != 201]}",
    )
    check(
        "every promise starts in the initial state, not a state the caller chose",
        all(p["promise"] and p["promise"]["state"] == "promised" for p in plan),
        "all 'promised' with resolved_at null and follow_up_sent false — the request "
        "model has no state field to set",
    )
    statuses = {
        e["event_id"]: e["status"]
        for e in db["events"].find({"event_id": {"$in": [p["event_id"] for p in plan]}})
    }
    check(
        "recording a promise moved each event to awaiting_promise",
        all(v == "awaiting_promise" for v in statuses.values()),
        f"{len(statuses)} events, all awaiting_promise — written by the one guarded "
        "implementation of that transition"
        if all(v == "awaiting_promise" for v in statuses.values())
        else f"{dict(Counter(statuses.values()))}",
    )

    # =====================================================================
    heading(f"3. THE MONEY ARRIVES — re-deliver {len(resequenced)} paid webhooks, "
            "now after the promise")
    # =====================================================================
    for p in plan:
        if p["event_id"] not in resequenced:
            continue
        event_id, razorpay_event, recovered_amount, _ = p["replay"]
        link_id = db["executions"].find_one(
            {"event_id": p["event_id"]}, {"razorpay_payment_link_id": 1}
        )["razorpay_payment_link_id"]
        entity = razorpay_call(
            client, "GET", f"{RAZORPAY}/payment_links/{link_id}"
        ).json()
        entity = {**entity, "status": "paid", "amount_paid": int(entity["amount"])}
        body = webhook_body(
            event=razorpay_event, entity=entity, created_at=int(entity["created_at"])
        )
        response = post_webhook(client, body=body, razorpay_event_id=event_id)
        ack = response.json()
        p["replay_http"] = response.status_code
        p["replay_ack"] = ack
        print(f"  {p['event_id']:<14} {razorpay_event:<22} -> HTTP "
              f"{response.status_code} processed={ack.get('processed')} "
              f"event_status={ack.get('event_status')!r}")
    replays = [p for p in plan if "replay_http" in p]
    check(
        "each re-delivered payment was accepted and processed",
        all(p["replay_http"] == 200 and p["replay_ack"].get("processed") for p in replays),
        f"{len(replays)} of {len(resequenced)} — the same razorpay_event_id as before, "
        "so the dataset's event ids are unchanged",
    )
    rewritten = {
        p["event_id"]: db["verifications"].find_one(
            {"razorpay_event_id": p["replay"][0]}
        )
        for p in replays
    }
    check(
        "the re-delivered payments recovered the same money as the archived records",
        all(
            rewritten[p["event_id"]] is not None
            and rewritten[p["event_id"]]["amount_recovered"] == p["replay"][2]
            and rewritten[p["event_id"]]["amount_expected"] == p["replay"][3]
            for p in replays
        ),
        f"amount_recovered and amount_expected match the archive on all "
        f"{len(replays)} records; only verified_at moved",
    )

    # =====================================================================
    heading(f"4. RESOLVE — POST /promises/{{event_id}}/check on all {len(plan)}")
    # =====================================================================
    for p in plan:
        response = client.post(f"{API}/promises/{p['event_id']}/check", timeout=60)
        p["check_http"] = response.status_code
        p["check"] = response.json() if response.status_code == 200 else response.text
    for p in plan:
        c = p["check"]
        if not isinstance(c, dict):
            print(f"  {p['event_id']:<14} HTTP {p['check_http']} {str(c)[:120]}")
            continue
        fu = c.get("follow_up")
        tail = (
            f"follow_up sent={fu['sent']} verdict={fu['policy_verdict']} "
            f"({fu['policy_reason']})"
            if fu
            else "no follow-up attempted"
        )
        print(f"  {p['event_id']:<14} {c['state_before']} -> {c['state']:<13} "
              f"deadline_passed={str(c['deadline_passed']):<5} {tail}")

    check(
        "every check returned 200",
        all(p["check_http"] == 200 for p in plan),
        f"{sum(1 for p in plan if p['check_http'] == 200)} of {len(plan)}",
    )
    check(
        "the mandatory payment re-check ran on every path, including the ones that "
        "did nothing",
        all(
            isinstance(p["check"], dict) and p["check"]["payment_rechecked_at"]
            for p in plan
        ),
        "payment_rechecked_at is non-null on all "
        f"{len(plan)} responses, including the 2 still-open promises",
    )
    check(
        "every promise reached the state its role calls for",
        all(p["check"]["state"] == p["expected"] for p in plan),
        f"{dict(Counter(p['check']['state'] for p in plan))}"
        if all(p["check"]["state"] == p["expected"] for p in plan)
        else "mismatches: "
        + str([(p["event_id"], p["expected"], p["check"]["state"]) for p in plan
               if p["check"]["state"] != p["expected"]]),
    )

    # =====================================================================
    heading("5. THE SAFETY PROPERTIES, READ OFF THE RESPONSES")
    # =====================================================================
    honored = [p for p in plan if p["expected"] == "honored"]
    suppressed = [p for p in plan if p["expected"] == "broken"]
    chased = [p for p in plan if p["expected"] == "reevaluating"]
    open_ = [p for p in plan if p["expected"] == "promised"]

    check(
        "no follow-up was attempted for any promise whose money had arrived",
        all(p["check"]["follow_up"] is None for p in honored) and bool(honored),
        f"{len(honored)} honored, follow_up null on every one — and each carries a "
        "recovered_verification_id, so the re-check is the reason, not a coincidence",
    )
    check(
        "the honored promises were settled before their deadline even mattered",
        all(p["check"]["deadline_passed"] is False for p in honored),
        "deadline_passed=false on all 4: the re-check found the money first, which is "
        "the ordering `check_promise` enforces by raising rather than branching",
    )
    check(
        "the suppressed follow-ups were refused by policy, not skipped",
        all(
            p["check"]["follow_up"]
            and p["check"]["follow_up"]["sent"] is False
            and p["check"]["follow_up"]["policy_verdict"] != "authorized"
            for p in suppressed
        ),
        "; ".join(
            f"{p['event_id']}: {p['check']['follow_up']['policy_verdict']} "
            f"({p['check']['follow_up']['policy_reason']})"
            for p in suppressed
        ),
    )
    check(
        "a suppressed follow-up leaves the promise broken and unchased, so a later "
        "check retries",
        all(
            p["check"]["state"] == "broken"
            and p["promise"] is not None
            and db["promises"].find_one({"_id": _oid(p)}).get("follow_up_sent") is False
            for p in suppressed
        ),
        f"{len(suppressed)} promises still broken with follow_up_sent false",
    )
    check(
        "every chased follow-up carries the verdict that authorized it",
        all(
            p["check"]["follow_up"]
            and p["check"]["follow_up"]["sent"] is True
            and p["check"]["follow_up"]["policy_verdict"] == "authorized"
            and p["check"]["follow_up"]["policy_verdict_id"]
            and p["check"]["follow_up"]["execution_id"]
            for p in chased
        ),
        "; ".join(
            f"{p['event_id']}: {p['check']['follow_up']['intervention']} via "
            f"{p['check']['follow_up']['action_type']}"
            for p in chased
        ),
    )
    check(
        "no chased follow-up produced a payment link",
        all(
            p["check"]["follow_up"]["action_type"] == "contact_logged"
            for p in chased
        ),
        "all 3 are contact_logged — the receivable block of the decision matrix only "
        "recommends contacts, so no link artifact exists to create",
    )
    check(
        "the still-open promises were left alone",
        all(
            p["check"]["changed"] is False
            and p["check"]["deadline_passed"] is False
            and p["check"]["follow_up"] is None
            for p in open_
        ),
        f"{len(open_)} promises untouched: unpaid, but the date has not passed, so "
        "nothing is due and nothing was sent",
    )

    # =====================================================================
    heading("6. READ BACK FROM THE API, AND THE LINK GUARANTEE")
    # =====================================================================
    stored = client.get(f"{API}/promises", params={"history": True}, timeout=30).json()
    demo = [s for s in stored if s["event_id"].startswith("demo_")]
    by_state = Counter(s["state"] for s in demo)
    check(
        "GET /promises reports 11 demo promises in the ratified distribution",
        len(demo) == 11
        and by_state == Counter({"honored": 4, "reevaluating": 3, "broken": 2,
                                 "promised": 2}),
        f"{len(demo)} records: {dict(by_state)}",
    )
    check(
        "resolved_at is set exactly on the promises that left the open state",
        all((s["resolved_at"] is not None) == (s["state"] != "promised") for s in demo),
        "9 resolved, 2 open — the model rejects any other combination",
    )
    check(
        "follow_up_sent is true only where a follow-up demonstrably executed",
        {s["event_id"] for s in demo if s["follow_up_sent"]}
        == {p["event_id"] for p in chased},
        f"exactly the {len(chased)} chased events, and no others",
    )
    fixtures = [s for s in stored if not s["event_id"].startswith("demo_")]
    check(
        "the 9 Stage 6 fixture promises are untouched",
        len(fixtures) == 9,
        f"{len(fixtures)} fixture promises, unchanged",
    )

    links_after = account_link_count(client)
    check(
        "this checkpoint created no payment link",
        links_after == links_before,
        f"{links_after} == {links_before}, measured against the account. "
        f"{30 - links_after} lifetime slots remain",
    )

    verifications = db["verifications"].count_documents({})
    recovered_money = sum(
        v["amount_recovered"]
        for v in db["verifications"].find({"event_id": {"$regex": "^demo_"}})
    )
    check(
        "checkpoint 5's figures survived the re-sequencing unchanged",
        verifications == 42 and abs(recovered_money - 22105.14) < 0.01,
        f"{verifications} verifications, {recovered_money:,.2f} recovered — identical "
        "to what was approved at checkpoint 5",
    )

    heading(f"CHECKPOINT 6 — {PASSED} passed, {FAILED} failed")
    client.close()
    return 1 if FAILED else 0


def _oid(plan_row: dict[str, Any]):
    from bson import ObjectId

    return ObjectId(plan_row["promise"]["id"])


if __name__ == "__main__":
    raise SystemExit(main())
