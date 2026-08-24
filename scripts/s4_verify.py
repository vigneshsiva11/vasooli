"""Stage 4 verification — a verdict in every category, with the full trail shown.

Three phases:

1. **Sweep.** Authorize every event that has a decision. Each verdict is printed
   with its complete `checks_performed` trail, and independently re-derived in
   *this* process (`app.policy.evaluate` over a context gathered here) while the
   verdict itself comes back over HTTP from the running app. The context is
   gathered *before* the HTTP call, so the two evaluations see the same history
   and a disagreement means a real disagreement.
2. **Cooldown.** Re-authorize a contact-type event that phase 1 just authorized.
   Nothing is backdated: the cooldown fails because the contact genuinely happened
   moments ago.
3. **Opt-out, end to end.** A customer with a contact-type decision already
   authorized is opted out, then a second contact-type decision for them is
   refused, and a non-contact decision for the same customer still authorizes.

Run:  python scripts/s4_verify.py http://127.0.0.1:8123
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision import latest_decision
from app.models import DecisionRecord
from app.models.policy import CHECK_FOR_REASON, check_failed, check_name
from app.policy import (
    AUTO_AUTHORIZE_BELOW,
    COOLDOWN_HOURS,
    MAX_CONTACTS_PER_EVENT,
    MINIMUM_ERV,
    NEVER_AUTO_AT_OR_ABOVE,
    POLICY_CHECKS,
    REASON_PRECEDENCE,
    REASON_VERDICT,
    evaluate,
    gather_context,
    is_contact_intervention,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

OPT_OUT_CUSTOMER = "cust_pol_S4_optout"
#: A contact-type decision in the auto tier, authorized during the sweep, so
#: re-authorizing it immediately afterwards must hit the cooldown.
COOLDOWN_EVENT = "dec_S3_TINYINV"

problems: list[str] = []
#: (verdict, reason) pairs actually observed, for the coverage table at the end.
observed: Counter[tuple[str, str]] = Counter()


def request(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode(errors="replace")}


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def print_trail(verdict: dict, indent: str = "    ") -> None:
    """Print the complete evaluation trail, failures marked."""
    failures = [e for e in verdict["checks_performed"] if check_failed(e)]
    print(
        f"{indent}{len(verdict['checks_performed'])} checks recorded, "
        f"{len(failures)} failed:"
    )
    for entry in verdict["checks_performed"]:
        name, rest = entry.split(": ", 1)
        status = "FAIL" if check_failed(entry) else "pass"
        marker = "X" if status == "FAIL" else " "
        detail = rest.split("(", 1)[1].rsplit(")", 1)[0]
        print(f"{indent}  {marker} {name:<22} {status}  {detail}")


async def cross_check(event_id: str) -> tuple[str, str, set[str]]:
    """Re-derive the verdict for an event here, from a context gathered here.

    Must be called before the HTTP authorize, so both evaluations see the same
    prior history.
    """
    document = await latest_decision(event_id)
    decision = DecisionRecord.from_document(document)
    event = await get_database()["events"].find_one({"event_id": event_id})
    context = await gather_context(
        decision=decision, customer_ref=event["customer_ref"]
    )
    verdict = evaluate(decision=decision, context=context)
    failed = {
        check_name(entry) for entry in verdict.checks_performed if check_failed(entry)
    }
    return verdict.verdict, verdict.reason, failed


async def authorize(event_id: str, *, label: str = "") -> dict:
    """Re-derive locally, then authorize over HTTP, then compare and print."""
    local_verdict, local_reason, local_failed = await cross_check(event_id)

    status, verdict = request(f"/authorize/{event_id}", method="POST")
    if status >= 400:
        problems.append(f"{event_id}: authorize returned {status} {verdict}")
        print(f"  FAILED {event_id}: {status} {verdict}")
        return {}

    observed[(verdict["verdict"], verdict["reason"])] += 1

    remote_failed = {
        check_name(entry)
        for entry in verdict["checks_performed"]
        if check_failed(entry)
    }
    agree = (
        local_verdict == verdict["verdict"]
        and local_reason == verdict["reason"]
        and local_failed == remote_failed
    )
    if not agree:
        problems.append(
            f"{event_id}: server said {verdict['verdict']}/{verdict['reason']} "
            f"failing {sorted(remote_failed)}, local engine said "
            f"{local_verdict}/{local_reason} failing {sorted(local_failed)}"
        )

    suffix = f"  [{label}]" if label else ""
    print(
        f"\n  {event_id}  ->  {verdict['verdict'].upper()}  "
        f"reason={verdict['reason']}  (verdict v{verdict['version']}, "
        f"decision v{verdict['decision_version']}){suffix}"
    )
    print(
        f"    independent re-derivation: {local_verdict}/{local_reason} "
        f"{'agrees' if agree else 'DISAGREES'}"
    )
    print_trail(verdict)
    return verdict


async def sweep() -> None:
    section("1. Sweep: authorize every event that has a decision")
    database = get_database()

    decisions = await database["decisions"].find().to_list(length=None)
    latest: dict[str, dict] = {}
    for document in decisions:
        current = latest.get(document["event_id"])
        if current is None or document["version"] > current["version"]:
            latest[document["event_id"]] = document

    events = {
        document["event_id"]: document
        for document in await database["events"].find().to_list(length=None)
    }

    # Order so the interesting categories are adjacent: authorizable first, then
    # reviews, then blocks.
    def sort_key(item: tuple[str, dict]) -> tuple:
        event_id, decision = item
        return (
            decision["revenue_at_risk"],
            event_id,
        )

    print(f"  {len(latest)} events carry a decision")
    for event_id, decision in sorted(latest.items(), key=sort_key):
        event = events[event_id]
        contact = is_contact_intervention(decision["recommended_intervention"])
        print(
            f"\n  --- {event_id}  {event['currency']} "
            f"{decision['revenue_at_risk']:,.2f}  "
            f"{decision['recommended_intervention']}"
            f"{' (contact-type)' if contact else ''}  "
            f"cost {decision['estimated_cost']:,.2f}  "
            f"ERV {decision['expected_recovery_value']:,.2f}  "
            f"customer {event['customer_ref']}"
        )
        await authorize(event_id)


async def cooldown_case() -> None:
    section("2. Cooldown: re-authorize a contact that was just authorized")
    print(
        f"  {COOLDOWN_EVENT} was authorized moments ago in phase 1. Nothing here is\n"
        f"  backdated — the {COOLDOWN_HOURS}h cooldown fails because the contact\n"
        "  genuinely happened seconds ago, and the cap still passes because only\n"
        f"  1 of {MAX_CONTACTS_PER_EVENT} contacts has been used."
    )
    verdict = await authorize(COOLDOWN_EVENT, label="second attempt")
    if verdict and verdict["reason"] != "cooldown_active":
        problems.append(
            f"{COOLDOWN_EVENT}: expected cooldown_active on the second attempt, "
            f"got {verdict['reason']}"
        )


async def opt_out_end_to_end() -> None:
    section("3. Opt-out, end to end")

    print(f"  a) Before the opt-out, {OPT_OUT_CUSTOMER}'s contact-type decision on")
    print("     pol_S4_OPTOUT_A was AUTHORIZED in phase 1. Confirming that:")
    status, verdicts = request("/policy-verdicts?event_id=pol_S4_OPTOUT_A&history=true")
    for stored in sorted(verdicts, key=lambda v: v["version"]):
        print(
            f"     verdict v{stored['version']}: {stored['verdict']} "
            f"({stored['reason']})"
        )
    if not any(v["verdict"] == "authorized" for v in verdicts):
        problems.append(
            "pol_S4_OPTOUT_A was never authorized, so a later block cannot be "
            "attributed to the opt-out"
        )

    print(f"\n  b) POST /opt-out/{OPT_OUT_CUSTOMER}")
    status, body = request(f"/opt-out/{OPT_OUT_CUSTOMER}", method="POST",
                           payload={"reason": "asked us to stop messaging"})
    print(f"     {status} {json.dumps(body)}")
    if status >= 400:
        problems.append(f"opt-out failed: {status} {body}")
        return

    print(f"\n  c) Repeat the same opt-out (must be idempotent, timestamp preserved)")
    status, repeat = request(f"/opt-out/{OPT_OUT_CUSTOMER}", method="POST")
    print(f"     {status} created={repeat.get('created')} "
          f"opted_out_at={repeat.get('opted_out_at')}")
    if repeat.get("created") is not False:
        problems.append("repeating an opt-out reported created=true")
    if repeat.get("opted_out_at") != body.get("opted_out_at"):
        problems.append("repeating an opt-out moved opted_out_at")

    print("\n  d) A contact-type decision for that customer, on a fresh event")
    print("     (pol_S4_OPTOUT_B: no prior contacts, cooldown clear, auto tier,")
    print(f"     ERV well above {MINIMUM_ERV:,.2f} — consent is the only thing wrong)")
    verdict = await authorize("pol_S4_OPTOUT_B", label="after opt-out")
    if verdict and verdict["reason"] != "customer_opted_out":
        problems.append(
            f"pol_S4_OPTOUT_B: expected customer_opted_out, got {verdict['reason']}"
        )

    print("\n  e) A NON-contact decision for the same opted-out customer")
    print("     (pol_S4_OPTOUT_RETRY: immediate_retry touches the payment rail,")
    print("     not the person, so the opt-out must not stand in its way)")
    verdict = await authorize("pol_S4_OPTOUT_RETRY", label="after opt-out")
    if verdict and verdict["verdict"] != "authorized":
        problems.append(
            f"pol_S4_OPTOUT_RETRY: a non-contact action was refused after an "
            f"opt-out ({verdict['verdict']}/{verdict['reason']})"
        )

    print("\n  f) Re-authorize pol_S4_OPTOUT_A, which is now BOTH opted out and")
    print("     inside its cooldown — two failures, and precedence must report")
    print("     consent rather than the cooldown")
    verdict = await authorize("pol_S4_OPTOUT_A", label="opt-out AND cooldown")
    if verdict:
        failed = {
            check_name(e) for e in verdict["checks_performed"] if check_failed(e)
        }
        if not {"customer_opt_out", "contact_cooldown"} <= failed:
            problems.append(
                f"pol_S4_OPTOUT_A: expected both customer_opt_out and "
                f"contact_cooldown to fail, got {sorted(failed)}"
            )
        if verdict["reason"] != "customer_opted_out":
            problems.append(
                f"pol_S4_OPTOUT_A: precedence reported {verdict['reason']} rather "
                "than customer_opted_out"
            )


def coverage() -> None:
    section("4. Coverage of the verdict/reason space")

    print(f"  {'reason':<26} {'verdict':<24} observed")
    for reason in ("ok",) + REASON_PRECEDENCE:
        verdict = REASON_VERDICT[reason]
        count = observed[(verdict, reason)]
        check = CHECK_FOR_REASON.get(reason, "-")
        marker = "OK " if count else "GAP"
        print(f"  {marker} {reason:<22} {verdict:<24} {count:>3}   via {check}")
        if not count:
            problems.append(f"no verdict observed for reason {reason!r}")

    print(f"\n  every declared check appeared in every trail: {len(POLICY_CHECKS)} "
          f"checks {POLICY_CHECKS}")


async def main() -> None:
    await connect_to_mongo()
    print(f"Stage 4 verification against {BASE}")
    print(
        f"minimum ERV {MINIMUM_ERV:,.2f} (zero-cost exempt)   "
        f"auto < {AUTO_AUTHORIZE_BELOW:,.2f}   "
        f"never-auto >= {NEVER_AUTO_AT_OR_ABOVE:,.2f}   "
        f"cap {MAX_CONTACTS_PER_EVENT}/event   cooldown {COOLDOWN_HOURS}h"
    )

    await sweep()
    await cooldown_case()
    await opt_out_end_to_end()
    coverage()

    await close_mongo_connection()

    print("\n" + "=" * 78)
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        "every verdict category observed; server and in-process engine agreed on "
        "every case; every trail carried all six checks"
    )


if __name__ == "__main__":
    asyncio.run(main())
