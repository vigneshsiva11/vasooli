"""Stage 4 seed — events that reach the policy branches live data cannot.

The existing 21 decisions already cover most of Stage 4's verdict space (see
`scripts/s4_verify.py`), but three cases are unreachable without new data:

* An **isolated contact-cap failure**. The cap is 3 contacts per event and the
  cooldown is 24h, so the cap can never be reached by authorizing repeatedly in
  one session — the cooldown blocks the second attempt first. Reaching it needs an
  event whose prior contacts are spaced further apart than the cooldown.
* An **opt-out that demonstrably changed the outcome**: the same customer with a
  contact-type decision authorized *before* the opt-out and another blocked
  *after* it, so the block is attributable to consent rather than to the event.
* A **non-contact decision for that same opted-out customer**, to show the
  opt-out gates contact and nothing else.

The three prior contacts on `pol_S4_CAP` are produced by the real engine, not
hand-written: only the clock is injected, via `PolicyContext.now`, which exists
for exactly this reason. They re-derive correctly under `scripts/s4_audit.py`.

Run:  python scripts/s4_seed.py http://127.0.0.1:8123
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

from app.db import close_mongo_connection, connect_to_mongo
from app.decision import latest_decision
from app.ingestion import upsert_event
from app.models import DecisionRecord, RevenueEvent
from app.policy import PolicyContext, append as append_verdict, evaluate

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

OPT_OUT_CUSTOMER = "cust_pol_S4_optout"

# Each event exists for one named reason. Amounts are all inside the auto tier and
# all yield an ERV well above the 25.00 floor, so the only check that can fail is
# the one the case is built to exercise.
#
# `raw_failure_reason` is set to the exact codes the deterministic classifier
# recognises, not prose. Prose falls through to the LLM, and a seed whose root
# cause depends on a live model call is a seed that can produce a different
# fixture on a different day — the first run of this script proved the point when
# "no_response after 3 follow-ups" matched neither keyword table, went to Gemini,
# came back a `fallback` at 0.20 confidence, and turned the cap fixture into a
# no-action decision.
EVENTS = [
    # Contact-type in the auto tier, clean economics: the vehicle for the
    # isolated contact-cap test once three spaced-out contacts are on record.
    {
        "event_id": "pol_S4_CAP",
        "surface": "receivable",
        "amount": 1500.00,
        "currency": "INR",
        "customer_ref": "cust_pol_S4_cap",
        "raw_failure_reason": "no_response",
    },
    # Contact-type, authorized BEFORE the customer opts out. Establishes that the
    # opt-out block later is caused by consent, not by something about the event.
    {
        "event_id": "pol_S4_OPTOUT_A",
        "surface": "receivable",
        "amount": 1400.00,
        "currency": "INR",
        "customer_ref": OPT_OUT_CUSTOMER,
        "raw_failure_reason": "no_response",
    },
    # Contact-type, same customer, authorized AFTER the opt-out. Fresh event, so
    # its own cap and cooldown are untouched and consent is the only failure.
    {
        "event_id": "pol_S4_OPTOUT_B",
        "surface": "receivable",
        "amount": 1300.00,
        "currency": "INR",
        "customer_ref": OPT_OUT_CUSTOMER,
        "raw_failure_reason": "unreachable",
    },
    # Non-contact, same opted-out customer: a retry touches the payment rail, not
    # the person, so the opt-out must not stand in its way.
    {
        "event_id": "pol_S4_OPTOUT_RETRY",
        "surface": "payment",
        "amount": 1200.00,
        "currency": "INR",
        "customer_ref": OPT_OUT_CUSTOMER,
        "raw_failure_reason": "gateway_timeout",
    },
]

#: Hours before now at which the three prior contacts on `pol_S4_CAP` were
#: authorized. Gaps of 30h each, all outside the 24h cooldown, so every one of
#: them is a verdict the engine genuinely authorizes — and the most recent is 40h
#: old, so the cooldown is clear and the cap is the only thing left to fail.
BACKDATED_CONTACT_HOURS = (100, 70, 40)


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


async def seed_events() -> None:
    await connect_to_mongo()
    print("1. Events")
    for payload in EVENTS:
        event = RevenueEvent(**payload)
        document_id, created = await upsert_event(event)
        print(
            f"   {event.event_id:<24} {event.currency} {event.amount:>9,.2f}  "
            f"{event.customer_ref:<22} created={created}"
        )
    await close_mongo_connection()


def diagnose_and_decide() -> None:
    print("\n2. Diagnose then decide (through the running app, rules path)")
    for payload in EVENTS:
        event_id = payload["event_id"]

        status, diagnosis = post(f"/diagnose/{event_id}")
        if status >= 400:
            print(f"   FAILED diagnose {event_id}: {status} {diagnosis}")
            sys.exit(1)

        status, decision = post(f"/decide/{event_id}")
        if status >= 400:
            print(f"   FAILED decide {event_id}: {status} {decision}")
            sys.exit(1)

        print(
            f"   {event_id:<24} {diagnosis['surface']}/{diagnosis['root_cause']} "
            f"conf {diagnosis['confidence']:.2f} ({diagnosis['method']}) -> "
            f"{decision['recommended_intervention']} "
            f"cost {decision['estimated_cost']:,.2f} "
            f"ERV {decision['expected_recovery_value']:,.2f} "
            f"(decision v{decision['version']})"
        )


async def backdate_prior_contacts() -> None:
    """Record three spaced-out prior contacts on `pol_S4_CAP`.

    Each verdict comes out of the real engine with a real context; the only thing
    supplied artificially is `now`. Because the gaps exceed the cooldown, all
    three are genuinely authorized rather than forced.
    """
    await connect_to_mongo()
    print("\n3. Three prior authorized contacts on pol_S4_CAP (clock injected)")

    document = await latest_decision("pol_S4_CAP")
    decision = DecisionRecord.from_document(document)
    print(
        f"   authorizing decision {decision.id} v{decision.version} "
        f"({decision.recommended_intervention}, contact-type)"
    )

    now = datetime.now(timezone.utc)
    prior = 0
    last: datetime | None = None

    for hours_ago in BACKDATED_CONTACT_HOURS:
        moment = now - timedelta(hours=hours_ago)
        context = PolicyContext(
            customer_ref="cust_pol_S4_cap",
            customer_opted_out=False,
            prior_authorized_contacts=prior,
            last_authorized_contact_at=last,
            now=moment,
        )
        verdict = evaluate(decision=decision, context=context)
        document_id, version = await append_verdict(verdict)
        gap = "first contact" if last is None else f"{(moment - last).total_seconds()/3600:.0f}h after the last"
        print(
            f"   t-{hours_ago:>3}h  verdict v{version} {verdict.verdict:<22} "
            f"reason={verdict.reason:<12} ({gap})"
        )
        if verdict.verdict != "authorized":
            print(
                f"   ABORT: the engine refused this one, so it cannot stand as a "
                f"prior contact: {verdict.reason}"
            )
            sys.exit(1)
        prior += 1
        last = moment

    print(
        f"   pol_S4_CAP now has {prior} authorized contacts, most recent "
        f"{BACKDATED_CONTACT_HOURS[-1]}h ago (cooldown is clear, cap is full)"
    )
    await close_mongo_connection()


def main() -> None:
    print(f"Stage 4 seed against {BASE}")
    asyncio.run(seed_events())
    diagnose_and_decide()
    asyncio.run(backdate_prior_contacts())
    print(
        f"\nNote: {OPT_OUT_CUSTOMER} is NOT opted out yet. "
        "scripts/s4_verify.py opts them out partway through, so the before/after "
        "difference is observable."
    )


if __name__ == "__main__":
    main()
