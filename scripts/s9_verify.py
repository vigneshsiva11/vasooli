"""Stage 9 verification — the receivable path, checked independently.

Independent in the sense the project has used since Stage 7: this file imports
nothing from `app/metrics/`, `app/webhooks/`, or any other application module. Every
expected number is recomputed here from raw motor queries, and every actual number
comes from an HTTP call to the running server. If the two agree, the agreement is
evidence; if they disagree, this script reports the disagreement rather than
explaining it.

What it checks, in five parts:

1. the collection state — three manual records, forty-two webhook records, and the
   read-time source default that makes every pre-Stage-9 row answer to
   `source=webhook` without any of them having been rewritten;
2. the source split in `/metrics/summary`, recomputed from raw documents, including
   the property the whole design rests on: the gateway figure must be exactly what it
   was before any manual confirmation existed, and the total must be the exact sum of
   the two parts;
3. the same split in `/metrics/by-intervention`, per intervention, plus the
   `verifiable` / `manually_confirmable` pair — the first unchanged from Stage 7, the
   second added beside it;
4. `/metrics/baseline-comparison`, where `kind: "real"` would otherwise blur asserted
   money into attested money;
5. the audit trail in `/audit-trail/{event_id}`, which renders a manual
   verification with a different stage label and an explicit "no gateway verified
   this" provenance string.

The dedup property is checked too, because it is the one that could have silently
broken: a manual confirmation carries its own contact `execution_id`, so it must not
collide with any webhook recovery in the latest-per-execution collapse.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("D:/vasooli/.env")

BASE = "http://127.0.0.1:8123"
WEBHOOK_SOURCE = "webhook"
MANUAL_SOURCE = "manual_confirmation"
CONTACT_ACTION = "contact_logged"
LINK_ACTIONS = ("payment_link_generated", "retry_simulated")

PASS = 0
FAIL = 0


def check(label: str, expected, actual, note: str = "") -> None:
    """Compare one independently derived number against one the endpoint returned."""
    global PASS, FAIL
    ok = expected == actual
    if isinstance(expected, float) and isinstance(actual, float):
        ok = abs(expected - actual) < 0.005
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
        print(f"         independent={expected}  endpoint={actual}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")
        print(f"         independent={expected}  endpoint={actual}")
    if note:
        print(f"         {note}")


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def money(value: float) -> float:
    return round(value + 0.0, 2)


async def main() -> int:
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[os.environ.get("MONGODB_DB_NAME", "vasooli")]

    print("=" * 78)
    print("STAGE 9 VERIFICATION — independent (raw motor + raw HTTP, no app imports)")
    print("=" * 78)

    # ---- raw reads, once ---------------------------------------------------
    verifications = [document async for document in db.verifications.find({})]
    events = {
        document["event_id"]: document async for document in db.events.find({})
    }
    executions = {
        str(document["_id"]): document async for document in db.executions.find({})
    }

    manual_docs = [d for d in verifications if d.get("source") == MANUAL_SOURCE]
    legacy_docs = [d for d in verifications if "source" not in d]
    tagged_webhook = [d for d in verifications if d.get("source") == WEBHOOK_SOURCE]

    print("\n[1] COLLECTION STATE")
    check("total verification documents", len(verifications), len(get("/verifications?history=true")))
    check("manual documents", len(manual_docs),
          len(get(f"/verifications?history=true&source={MANUAL_SOURCE}")))
    check(
        "documents answering to source=webhook",
        len(legacy_docs) + len(tagged_webhook),
        len(get(f"/verifications?history=true&source={WEBHOOK_SOURCE}")),
        f"{len(legacy_docs)} of them carry no stored `source` at all and are matched by "
        "the read-time default, not by a backfill",
    )
    check(
        "manual and webhook partition the collection with nothing left over",
        len(verifications),
        len(manual_docs) + len(legacy_docs) + len(tagged_webhook),
    )
    check(
        "every manual document carries a confirmation_id derived from its execution",
        len(manual_docs),
        sum(
            1
            for d in manual_docs
            if d.get("confirmation_id") == f"manual_conf_{d['execution_id']}"
        ),
    )
    check(
        "every manual document names a contact_logged execution",
        len(manual_docs),
        sum(
            1
            for d in manual_docs
            if executions.get(d["execution_id"], {}).get("action_type") == CONTACT_ACTION
        ),
        "the allowlist, observed in the data rather than in the code",
    )
    check(
        "no manual document carries any Razorpay field",
        0,
        sum(
            1
            for d in manual_docs
            if {"razorpay_event_id", "razorpay_event", "razorpay_payment_link_id"} & d.keys()
        ),
    )

    # ---- the dedup collapse, recomputed -----------------------------------
    latest: dict[str, dict] = {}
    for document in verifications:
        if document.get("outcome") != "recovered":
            continue
        key = document["execution_id"]
        current = latest.get(key)
        if current is None or document["verified_at"] > current["verified_at"]:
            latest[key] = document
    recovered_records = [d for d in verifications if d.get("outcome") == "recovered"]

    gateway_survivors = [d for d in latest.values() if d.get("source") != MANUAL_SOURCE]
    manual_survivors = [d for d in latest.values() if d.get("source") == MANUAL_SOURCE]
    gateway_money = money(sum(d["amount_recovered"] for d in gateway_survivors))
    manual_money = money(sum(d["amount_recovered"] for d in manual_survivors))

    print("\n[2] /metrics/summary — THE SOURCE SPLIT")
    summary = get("/metrics/summary")
    check("recovered verification records (raw, pre-dedup)",
          len(recovered_records), summary["recovered_verification_records"])
    check("distinct recoveries after the latest-per-execution collapse",
          len(latest), summary["distinct_recoveries_counted"])
    check("duplicates ignored", len(recovered_records) - len(latest),
          summary["duplicate_verification_records_ignored"],
          "unchanged by Stage 9: a manual record has its own contact execution_id, so it "
          "cannot collide with a webhook recovery in the collapse")
    check("distinct recoveries — gateway", len(gateway_survivors),
          summary["distinct_recoveries_gateway_verified"])
    check("distinct recoveries — manually asserted", len(manual_survivors),
          summary["distinct_recoveries_manually_asserted"])
    check("gateway-verified revenue", gateway_money, summary["gateway_verified_recovered"])
    check("manually asserted revenue", manual_money, summary["manually_asserted_recovered"])
    check("total revenue recovered is the EXACT sum of the two parts",
          money(gateway_money + manual_money), summary["total_revenue_recovered"])

    at_risk = money(sum(e["amount"] for e in events.values()))
    check("total revenue at risk", at_risk, summary["total_revenue_at_risk"])
    check("headline recovery rate (blended, both sources)",
          round(100.0 * (gateway_money + manual_money) / at_risk, 2),
          summary["recovery_rate"])
    check("gateway-only recovery rate",
          round(100.0 * gateway_money / at_risk, 2),
          summary["recovery_rate_gateway_verified"],
          "this is the figure a third party attests to; asserted money must not move it")

    # ---- by intervention ---------------------------------------------------
    print("\n[3] /metrics/by-intervention — THE SPLIT, PER INTERVENTION")
    rows = {row["intervention"]: row for row in get("/metrics/by-intervention")}

    expected_gateway: dict[str, int] = {}
    expected_manual: dict[str, int] = {}
    expected_gateway_money: dict[str, float] = {}
    expected_manual_money: dict[str, float] = {}
    for document in latest.values():
        execution = executions.get(document["execution_id"])
        if execution is None:
            continue
        name = execution["intervention"]
        if document.get("source") == MANUAL_SOURCE:
            expected_manual[name] = expected_manual.get(name, 0) + 1
            expected_manual_money[name] = (
                expected_manual_money.get(name, 0.0) + document["amount_recovered"]
            )
        else:
            expected_gateway[name] = expected_gateway.get(name, 0) + 1
            expected_gateway_money[name] = (
                expected_gateway_money.get(name, 0.0) + document["amount_recovered"]
            )

    action_for: dict[str, str] = {}
    for execution in executions.values():
        action_for[execution["intervention"]] = execution["action_type"]

    for name in sorted(rows):
        row = rows[name]
        action = action_for.get(name)
        if action is None:
            continue
        check(
            f"{name}: verifiable (gateway) / manually_confirmable",
            (action in LINK_ACTIONS, action == CONTACT_ACTION),
            (row["verifiable"], row["manually_confirmable"]),
            f"action_type={action}",
        )
        check(
            f"{name}: recoveries gateway / manual",
            (expected_gateway.get(name, 0), expected_manual.get(name, 0)),
            (row["recoveries_gateway_verified"], row["recoveries_manually_asserted"]),
        )
        check(
            f"{name}: revenue gateway / manual",
            (money(expected_gateway_money.get(name, 0.0)),
             money(expected_manual_money.get(name, 0.0))),
            (row["revenue_recovered_gateway_verified"],
             row["revenue_recovered_manually_asserted"]),
        )

    check(
        "no contact-type row claims a gateway recovery",
        0,
        sum(
            row["recoveries_gateway_verified"]
            for row in rows.values()
            if row["manually_confirmable"]
        ),
    )
    check(
        "no link-type row claims a manual recovery",
        0,
        sum(
            row["recoveries_manually_asserted"]
            for row in rows.values()
            if row["verifiable"]
        ),
        "the two paths do not leak into each other",
    )
    check("by-intervention gateway money sums to the summary figure",
          summary["gateway_verified_recovered"],
          money(sum(r["revenue_recovered_gateway_verified"] for r in rows.values())))
    check("by-intervention manual money sums to the summary figure",
          summary["manually_asserted_recovered"],
          money(sum(r["revenue_recovered_manually_asserted"] for r in rows.values())))

    # ---- baseline comparison ----------------------------------------------
    print("\n[4] /metrics/baseline-comparison — vasooli_actual")
    actual = get("/metrics/baseline-comparison")["vasooli_actual"]
    check("kind", "real", actual["kind"])
    check("revenue_recovered_gateway_verified", gateway_money,
          actual["revenue_recovered_gateway_verified"])
    check("revenue_recovered_manually_asserted", manual_money,
          actual["revenue_recovered_manually_asserted"])
    check("revenue_recovered is the sum", money(gateway_money + manual_money),
          actual["revenue_recovered"])
    check("events_recovered", len({d["event_id"] for d in latest.values()}),
          actual["events_recovered"])
    check("executions_verified_recovered", len(latest),
          actual["executions_verified_recovered"])

    # ---- the audit trail --------------------------------------------------
    print("\n[5] /audit-trail/{event_id} — HOW A MANUAL RECOVERY READS IN THE TRAIL")
    for document in manual_docs:
        event_id = document["event_id"]
        trail = get(f"/audit-trail/{event_id}")
        entries = [
            entry for entry in trail["timeline"] if entry["stage"] == "9-verification"
        ]
        check(
            f"{event_id}: exactly one 9-verification timeline entry",
            1,
            len(entries),
            "stage 9, not stage 6 — an auditor can see which channel confirmed it",
        )
        if entries:
            summary_text = entries[0]["summary"]
            check(
                f"{event_id}: the entry says no gateway verified it",
                True,
                "no gateway verified this" in summary_text
                and MANUAL_SOURCE in summary_text,
                summary_text,
            )
        check(
            f"{event_id}: no 6-verification entry (nothing pretends a webhook arrived)",
            0,
            len([e for e in trail["timeline"] if e["stage"] == "6-verification"]),
        )

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed")
    print("=" * 78)
    client.close()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
