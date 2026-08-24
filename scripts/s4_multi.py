"""Stage 4 — the maximal simultaneous-failure case.

The sweep in `scripts/s4_verify.py` produces trails with up to two failures. This
script builds the worst legitimately reachable case and shows that the trail still
comes back whole, with the ratified precedence picking the right reason out of
four competing ones.

The scenario is not contrived. A receivable is chased three times over several
days; the customer then asks to be left alone; meanwhile the outstanding balance
grows past the never-auto ceiling. Now every protection fires at once:

    customer_opt_out    FAIL  consent withdrawn
    contact_cap         FAIL  3 of 3 contacts already used
    contact_cooldown    FAIL  last contact 2h ago
    amount_tier         FAIL  60,000 is above the never-auto ceiling

Four is the maximum — provably, not incidentally:

* `decision_is_actionable` can only fail for a `no_action*` recommendation, which
  is not a contact and costs nothing, so failing it forces opt-out, cap, cooldown
  and the ERV floor to all pass. Its ceiling is 2 failures.
* `erv_minimum` and `amount_tier` are mutually exclusive. The floor only bites at
  ERV < 25 with a non-zero cost, which requires a tiny amount — and a tiny amount
  is in the auto tier. A tier failure needs >= 5,000, whose ERV clears 25 easily.

So the six checks admit at most four simultaneous failures, and this is one.

Run:  python scripts/s4_multi.py http://127.0.0.1:8123
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision import latest_decision
from app.ingestion import upsert_event
from app.models import DecisionRecord, RevenueEvent
from app.models.policy import REASON_PRECEDENCE, check_failed, check_name
from app.policy import (
    NEVER_AUTO_AT_OR_ABOVE,
    PolicyContext,
    append as append_verdict,
    evaluate,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

EVENT_ID = "pol_S4_MULTI"
CUSTOMER = "cust_pol_S4_multi"

#: Starting balance — inside the auto tier, so the three chases below are genuinely
#: authorized by the engine rather than forced.
OPENING_AMOUNT = 1_500.00
#: What the balance has grown to by the time of the fourth attempt. Above the
#: never-auto ceiling, so the agent may no longer act on it alone.
GROWN_AMOUNT = 60_000.00

#: Hours before now for the three prior chases. Gaps of 30h and 28h clear the 24h
#: cooldown, so all three are authorized; the last at t-2h leaves the cooldown
#: active for the fourth attempt.
CONTACT_HOURS = (60, 30, 2)

problems: list[str] = []


def post(path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def print_trail(trail: list[str], indent: str = "      ") -> None:
    failures = [entry for entry in trail if check_failed(entry)]
    print(f"{indent}{len(trail)} checks recorded, {len(failures)} FAILED:")
    for entry in trail:
        name, rest = entry.split(": ", 1)
        detail = rest.split("(", 1)[1].rsplit(")", 1)[0]
        marker = "X" if check_failed(entry) else " "
        status = "FAIL" if check_failed(entry) else "pass"
        print(f"{indent}  {marker} {name:<22} {status}  {detail}")


async def ingest(amount: float) -> None:
    await connect_to_mongo()
    document_id, created = await upsert_event(
        RevenueEvent(
            event_id=EVENT_ID,
            surface="receivable",
            amount=amount,
            currency="INR",
            customer_ref=CUSTOMER,
            raw_failure_reason="no_response",
        )
    )
    stored = await get_database()["events"].find_one({"event_id": EVENT_ID}, {"amount": 1})
    print(
        f"   {EVENT_ID} amount now {stored['amount']:,.2f} "
        f"(created={created})"
    )
    if abs(float(stored["amount"]) - amount) > 0.001:
        problems.append(
            f"re-ingestion did not update the amount: wanted {amount}, stored "
            f"{stored['amount']}"
        )
    await close_mongo_connection()


async def three_prior_chases() -> None:
    """Authorize three real contacts, spaced so the engine permits each one."""
    await connect_to_mongo()
    print("\n3. Three prior chases, spaced past the cooldown (clock injected)")

    decision = DecisionRecord.from_document(await latest_decision(EVENT_ID))
    print(
        f"   authorizing decision {decision.id} v{decision.version} "
        f"({decision.recommended_intervention}, {decision.revenue_at_risk:,.2f})"
    )

    now = datetime.now(timezone.utc)
    prior, last = 0, None
    for hours_ago in CONTACT_HOURS:
        moment = now - timedelta(hours=hours_ago)
        verdict = evaluate(
            decision=decision,
            context=PolicyContext(
                customer_ref=CUSTOMER,
                customer_opted_out=False,
                prior_authorized_contacts=prior,
                last_authorized_contact_at=last,
                now=moment,
            ),
        )
        _, version = await append_verdict(verdict)
        gap = "first" if last is None else f"{(moment - last).total_seconds()/3600:.0f}h later"
        print(
            f"   t-{hours_ago:>3}h  verdict v{version} {verdict.verdict:<12} "
            f"reason={verdict.reason:<10} ({gap})"
        )
        if verdict.verdict != "authorized":
            problems.append(
                f"chase at t-{hours_ago}h was not authorized ({verdict.reason}), so it "
                "cannot stand as a prior contact"
            )
            await close_mongo_connection()
            return
        prior, last = prior + 1, moment

    print(f"   cap is now full ({prior}/3) and the last contact was {CONTACT_HOURS[-1]}h ago")
    await close_mongo_connection()


async def main() -> None:
    print(f"Stage 4 — maximal simultaneous-failure case, against {BASE}")

    print(f"\n1. Ingest {EVENT_ID} at the opening balance")
    await ingest(OPENING_AMOUNT)

    print("\n2. Diagnose and decide")
    status, diagnosis = post(f"/diagnose/{EVENT_ID}")
    if status >= 400:
        print(f"   FAILED diagnose: {status} {diagnosis}")
        sys.exit(1)
    status, decision = post(f"/decide/{EVENT_ID}")
    if status >= 400:
        print(f"   FAILED decide: {status} {decision}")
        sys.exit(1)
    print(
        f"   {diagnosis['surface']}/{diagnosis['root_cause']} conf "
        f"{diagnosis['confidence']:.2f} ({diagnosis['method']}) -> "
        f"{decision['recommended_intervention']} cost "
        f"{decision['estimated_cost']:,.2f} ERV "
        f"{decision['expected_recovery_value']:,.2f} (decision v{decision['version']})"
    )

    await three_prior_chases()

    print(f"\n4. The customer withdraws consent")
    status, body = post(f"/opt-out/{CUSTOMER}", {"reason": "stop contacting me"})
    print(f"   {status} created={body.get('created')} at {body.get('opted_out_at')}")

    print(f"\n5. The balance grows past the {NEVER_AUTO_AT_OR_ABOVE:,.2f} ceiling")
    await ingest(GROWN_AMOUNT)
    status, decision = post(f"/decide/{EVENT_ID}")
    if status >= 400:
        print(f"   FAILED re-decide: {status} {decision}")
        sys.exit(1)
    print(
        f"   re-decided: {decision['recommended_intervention']} on "
        f"{decision['revenue_at_risk']:,.2f} ERV "
        f"{decision['expected_recovery_value']:,.2f} (decision v{decision['version']})"
    )

    print("\n6. A fourth attempt, with every protection now failing at once")
    status, verdict = post(f"/authorize/{EVENT_ID}")
    if status >= 400:
        print(f"   FAILED authorize: {status} {verdict}")
        sys.exit(1)

    print(
        f"\n   {EVENT_ID}  ->  {verdict['verdict'].upper()}  "
        f"reason={verdict['reason']}  (verdict v{verdict['version']}, "
        f"decision v{verdict['decision_version']})"
    )
    print_trail(verdict["checks_performed"])

    failed = [
        check_name(entry) for entry in verdict["checks_performed"] if check_failed(entry)
    ]
    expected = {"customer_opt_out", "contact_cap", "contact_cooldown", "amount_tier"}
    if set(failed) != expected:
        problems.append(
            f"expected exactly {sorted(expected)} to fail, got {sorted(failed)}"
        )
    if len(verdict["checks_performed"]) != 6:
        problems.append(f"trail has {len(verdict['checks_performed'])} entries, not 6")
    if verdict["reason"] != "customer_opted_out":
        problems.append(
            f"precedence reported {verdict['reason']}, not customer_opted_out"
        )
    if verdict["verdict"] != "blocked":
        problems.append(
            f"verdict was {verdict['verdict']}; a consent violation must block, not "
            "route for review — otherwise the never-auto tier would upgrade an "
            "opt-out breach into something a human could wave through"
        )

    print("\n7. What precedence had to choose between")
    contributed = {
        "customer_opt_out": "customer_opted_out",
        "contact_cap": "contact_cap_exceeded",
        "contact_cooldown": "cooldown_active",
        "amount_tier": "amount_never_auto",
    }
    reasons = [contributed[name] for name in failed if name in contributed]
    for reason in sorted(reasons, key=REASON_PRECEDENCE.index):
        rank = REASON_PRECEDENCE.index(reason) + 1
        chosen = " <- reported" if reason == verdict["reason"] else ""
        print(f"   {rank}. {reason}{chosen}")
    print(
        f"\n   All four are in the trail; the softest of them "
        f"(amount_never_auto, a review) did not get to speak for the verdict."
    )

    print("\n" + "=" * 78)
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        "four simultaneous failures, all six checks recorded, highest-precedence "
        "reason reported, and a hard block was not softened into a review"
    )


if __name__ == "__main__":
    asyncio.run(main())
