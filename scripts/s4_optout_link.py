"""Stage 4 — opt-out end to end for `payment_method_update_link`.

Ratified change under test: `payment_method_update_link` is contact-type. Before
it, an opted-out customer whose card had expired would be sent a
payment-method-update link anyway, and the link would not consume one of their
three contacts. This script proves the gap is closed, and — importantly — that
the reclassification is what closed it rather than something incidental.

Five phases:

1. A card-expiry failure for a customer who has NOT opted out. Authorized, as it
   should be. The trail is inspected for the *real* consent/cap/cooldown branches
   instead of "not applicable", which is what shows the reclassification is live.
2. The customer withdraws consent.
3. The same event, re-authorized. Blocked on consent — and the trail now shows
   that phase 1's link consumed a contact and started the cooldown, which it
   would not have done before.
4. A SECOND card-expiry failure for the same opted-out customer. Fresh event, so
   no cap or cooldown history: consent is the ONLY thing that fails, which makes
   the block unambiguously attributable to the opt-out rather than to a
   coincidental cooldown.
5. The same decision from phase 4 re-evaluated under the OLD classification, in
   process and unpersisted, to show identical inputs producing `authorized` then
   and `blocked` now.

Run:  python scripts/s4_optout_link.py http://127.0.0.1:8123
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo
from app.decision import latest_decision
from app.models import DecisionRecord
from app.models.policy import check_failed, check_name
from app.policy import PolicyContext, evaluate, rules

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

CUSTOMER = "cust_pol_S4_link"
EVENT_A = "pol_S4_LINK_A"
EVENT_B = "pol_S4_LINK_B"

#: Both inside the autonomous tier, so the amount check cannot fire and muddy the
#: attribution. With p=0.45 the ERV clears the 25.00 floor by three orders of
#: magnitude, so that cannot fire either.
AMOUNT_A = 2_000.00
AMOUNT_B = 2_200.00

#: Diagnosed by the rules path at 0.97 confidence — no LLM involved, so the
#: scenario is byte-reproducible.
FAILURE_REASON = "card_expired"

#: What the classification change is about.
LINK = "payment_method_update_link"

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        problems.append(f"{label} — {detail}" if detail else label)
    return ok


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data if method == "POST" else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def post(path: str, payload: dict | None = None) -> tuple[int, dict]:
    return request("POST", path, payload)


def print_trail(trail: list[str], indent: str = "      ") -> None:
    failures = [entry for entry in trail if check_failed(entry)]
    print(f"{indent}{len(trail)} checks recorded, {len(failures)} FAILED:")
    for entry in trail:
        name, rest = entry.split(": ", 1)
        detail = rest.split("(", 1)[1].rsplit(")", 1)[0]
        marker = "X" if check_failed(entry) else " "
        status = "FAIL" if check_failed(entry) else "pass"
        print(f"{indent}  {marker} {name:<22} {status}  {detail}")


def detail_for(trail: list[str], name: str) -> str:
    for entry in trail:
        if check_name(entry) == name:
            return entry.split(": ", 1)[1].split("(", 1)[1].rsplit(")", 1)[0]
    return ""


def failures_in(trail: list[str]) -> set[str]:
    return {check_name(entry) for entry in trail if check_failed(entry)}


def seed(event_id: str, amount: float) -> bool:
    """Ingest, diagnose and decide one card-expiry failure."""
    status, body = post(
        "/events",
        {
            "event_id": event_id,
            "surface": "payment",
            "amount": amount,
            "currency": "INR",
            "customer_ref": CUSTOMER,
            "raw_failure_reason": FAILURE_REASON,
        },
    )
    if status >= 400:
        print(f"   FAILED ingest: {status} {body}")
        return False

    status, diagnosis = post(f"/diagnose/{event_id}")
    if status >= 400:
        print(f"   FAILED diagnose: {status} {diagnosis}")
        return False

    status, decision = post(f"/decide/{event_id}")
    if status >= 400:
        print(f"   FAILED decide: {status} {decision}")
        return False

    print(
        f"   {event_id}  {amount:,.2f}  -> {diagnosis['root_cause']} "
        f"(conf {diagnosis['confidence']:.2f}, {diagnosis['method']}) -> "
        f"{decision['recommended_intervention']} "
        f"cost {decision['estimated_cost']:,.2f} "
        f"ERV {decision['expected_recovery_value']:,.2f}"
    )
    ok = check(
        f"{event_id} diagnoses as card_expired and is recommended {LINK}",
        diagnosis["root_cause"] == "card_expired"
        and decision["recommended_intervention"] == LINK,
        f"got {diagnosis['root_cause']} -> {decision['recommended_intervention']}",
    )
    return ok


def main() -> None:
    print(f"Stage 4 — opt-out end to end for {LINK}, against {BASE}")
    print(f"contact-type set now: {sorted(rules.CONTACT_INTERVENTIONS)}")
    if LINK not in rules.CONTACT_INTERVENTIONS:
        print(f"\nABORT: {LINK} is not in CONTACT_INTERVENTIONS; nothing to test.")
        sys.exit(1)

    # -- 1 ------------------------------------------------------------------
    print(f"\n1. A card-expiry failure for a customer who has not opted out")
    if not seed(EVENT_A, AMOUNT_A):
        sys.exit(1)

    status, first = post(f"/authorize/{EVENT_A}")
    if status >= 400:
        print(f"   FAILED authorize: {status} {first}")
        sys.exit(1)
    print(
        f"\n   {EVENT_A}  ->  {first['verdict'].upper()}  reason={first['reason']}  "
        f"(verdict v{first['version']})"
    )
    print_trail(first["checks_performed"])

    check(
        "consent has not been withdrawn yet, so the link is authorized",
        first["verdict"] == "authorized" and first["reason"] == "ok",
        f"{first['verdict']}/{first['reason']}",
    )
    # The point of this assertion: before the reclassification these three checks
    # returned "not applicable: payment_method_update_link does not contact the
    # customer". Real details are the proof the new classification is in force.
    for name in ("customer_opt_out", "contact_cap", "contact_cooldown"):
        detail = detail_for(first["checks_performed"], name)
        check(
            f"{name} evaluated {LINK} as a real contact, not 'not applicable'",
            "not applicable" not in detail,
            detail,
        )

    # -- 2 ------------------------------------------------------------------
    print(f"\n2. The customer withdraws consent")
    status, opt = post(f"/opt-out/{CUSTOMER}", {"reason": "stop emailing me"})
    print(f"   {status} created={opt.get('created')} at {opt.get('opted_out_at')}")
    check("the opt-out was recorded", status == 201 and opt.get("created") is True,
          json.dumps(opt))

    # -- 3 ------------------------------------------------------------------
    print(f"\n3. The same event, re-authorized now that consent is withdrawn")
    status, second = post(f"/authorize/{EVENT_A}")
    if status >= 400:
        print(f"   FAILED authorize: {status} {second}")
        sys.exit(1)
    print(
        f"\n   {EVENT_A}  ->  {second['verdict'].upper()}  reason={second['reason']}  "
        f"(verdict v{second['version']})"
    )
    print_trail(second["checks_performed"])

    check(
        "the link is now blocked on consent",
        second["verdict"] == "blocked" and second["reason"] == "customer_opted_out",
        f"{second['verdict']}/{second['reason']}",
    )
    cap_detail = detail_for(second["checks_performed"], "contact_cap")
    check(
        "phase 1's link consumed one of the three contacts",
        cap_detail.startswith("1 of 3"),
        cap_detail,
    )
    check(
        "phase 1's link also started the cooldown",
        "contact_cooldown" in failures_in(second["checks_performed"]),
        detail_for(second["checks_performed"], "contact_cooldown"),
    )

    # -- 4 ------------------------------------------------------------------
    print(f"\n4. A second card-expiry failure for the same opted-out customer")
    print("   Fresh event: no cap or cooldown history, so consent stands alone.")
    if not seed(EVENT_B, AMOUNT_B):
        sys.exit(1)

    status, clean = post(f"/authorize/{EVENT_B}")
    if status >= 400:
        print(f"   FAILED authorize: {status} {clean}")
        sys.exit(1)
    print(
        f"\n   {EVENT_B}  ->  {clean['verdict'].upper()}  reason={clean['reason']}  "
        f"(verdict v{clean['version']})"
    )
    print_trail(clean["checks_performed"])

    check(
        "blocked with customer_opted_out",
        clean["verdict"] == "blocked" and clean["reason"] == "customer_opted_out",
        f"{clean['verdict']}/{clean['reason']}",
    )
    check(
        "consent is the ONLY failed check, so nothing else can be credited",
        failures_in(clean["checks_performed"]) == {"customer_opt_out"},
        f"failed: {sorted(failures_in(clean['checks_performed']))}",
    )
    check(
        "the full trail is still recorded, not truncated at the failure",
        len(clean["checks_performed"]) == 6,
        f"{len(clean['checks_performed'])} entries",
    )

    return clean


async def old_versus_new(clean: dict) -> None:
    """Re-evaluate phase 4's decision under the old classification.

    Identical decision, identical context, only `CONTACT_INTERVENTIONS` differs —
    so any difference in verdict is attributable to the reclassification and
    nothing else. Nothing here is persisted.
    """
    print(f"\n5. The same decision under the old classification (in process, not stored)")
    await connect_to_mongo()
    decision = DecisionRecord.from_document(await latest_decision(EVENT_B))
    await close_mongo_connection()

    context = PolicyContext(
        customer_ref=CUSTOMER,
        customer_opted_out=True,
        prior_authorized_contacts=0,
        last_authorized_contact_at=None,
        # The stored verdict's own clock, so the two evaluations differ in the
        # classification and in nothing else at all.
        now=datetime.fromisoformat(clean["evaluated_at"].replace("Z", "+00:00")),
    )

    current = evaluate(decision=decision, context=context)

    narrowed = frozenset(rules.CONTACT_INTERVENTIONS - {LINK})
    original = rules.CONTACT_INTERVENTIONS
    try:
        rules.CONTACT_INTERVENTIONS = narrowed
        before = evaluate(decision=decision, context=context)
    finally:
        rules.CONTACT_INTERVENTIONS = original

    print(f"   contact set {sorted(narrowed)}")
    print(f"     -> {before.verdict.upper():<10} reason={before.reason}")
    print(f"   contact set {sorted(original)}")
    print(f"     -> {current.verdict.upper():<10} reason={current.reason}")

    check(
        f"under the old classification an opted-out customer got the link anyway",
        before.verdict == "authorized" and before.reason == "ok",
        f"{before.verdict}/{before.reason}",
    )
    check(
        "under the ratified classification the same inputs are blocked",
        current.verdict == "blocked" and current.reason == "customer_opted_out",
        f"{current.verdict}/{current.reason}",
    )
    check(
        "the stored verdict matches the in-process re-derivation",
        current.verdict == clean["verdict"] and current.reason == clean["reason"],
        f"stored {clean['verdict']}/{clean['reason']} vs {current.verdict}/{current.reason}",
    )


if __name__ == "__main__":
    result = main()
    asyncio.run(old_versus_new(result))

    print("\n" + "=" * 78)
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        f"{LINK} is consent-gated and consumes a contact; an opted-out customer "
        "with an expired card is blocked, not messaged"
    )
