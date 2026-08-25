"""Stage 6 Part B — promise-to-pay end to end against the running API.

Six scenarios, each on its own freshly seeded event and its own customer, run over
real HTTP against a real MongoDB and (in scenario C) a real HMAC-signed Razorpay
webhook body built from a live payment-link object.

    A   a promise is recorded, opens as `promised`, and a check before the date is
        a no-op. Duplicate and conflicting creates behave.
    B   the date has passed and nothing was paid -> `broken`, and the follow-up is
        AUTHORIZED by policy and executed -> `reevaluating`.
    B2  same, but the customer was contacted minutes ago -> the follow-up is
        BLOCKED on the cooldown. The promise stays `broken` so a later check retries.
    B3  same, but the customer has opted out -> the follow-up is BLOCKED on consent.
    C-CONTROL   the exact fixture shape scenario C uses, past-due and unpaid, on its
        own event: it breaks, policy authorizes, and a follow-up executes.
    C   THE CRITICAL TEST. Identical setup, except the money arrives first.
        Expected: `honored`, `follow_up: null`, and NOT ONE execution record.

Scenario C is the whole point of the design, and the control is why it proves
anything. The control runs on a SEPARATE event rather than on C's own promise before
paying it: checking C's promise first would set `follow_up_sent=True`, and then the
critical check would have two independent reasons to stay silent — the money and the
flag. It would look identical and prove half as much. As written, C's promise is
`promised` with `follow_up_sent=False` at the moment of the critical check, so
payment is the only thing that can suppress the follow-up. The proof is by
consequence rather than by assertion: the execution-record count for that event is
taken before and after, and it does not move.

WHAT IS REAL AND WHAT IS NOT
----------------------------
Real: every HTTP call, every Mongo write, the policy verdicts, the contact-cap and
cooldown arithmetic, the Razorpay payment link created for scenario C's event, the
HMAC-SHA256 signature over the exact bytes posted, and the verification record that
signature admits.

Not real: the *delivery hop* of scenario C's webhook, for the reason
`scripts/s6_verify.py` documents at length — Razorpay only delivers to ports 80/443
on a public host. And scenario C overrides exactly two fields on the fetched link
object, `status` and `amount_paid`, because completing a card checkout needs a
browser. Both overrides are printed at runtime.

Also not real: the passage of time. Scenarios B, B2, B3 and C use promises dated in
the past rather than waiting for a deadline. `PromiseRequest` accepts a past date on
purpose — a promise is recorded by a human after a conversation, sometimes days
later — so this is the documented input path, not a backdoor into it.

This script writes real records, creates a real Razorpay test-mode payment link, and
moves real event statuses. What it touched is listed at the end.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from bson import ObjectId

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.execution.razorpay import credentials_from_settings
from app.models.policy import check_name
from app.policy import current_fingerprint
from app.policy.rules import CONTACT_INTERVENTIONS
from app.webhooks.signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    expected_signature,
    webhook_secret,
)

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
RAZORPAY = "https://api.razorpay.com/v1"

#: Run-scoped, so the script is re-runnable: event ids, customer refs and the
#: Razorpay event ids minted below are all unique per invocation. Without this the
#: second run would collide on the events' unique index and on the webhook
#: idempotency key, and report both as failures.
TAG = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

passes: list[str] = []
failures: list[str] = []
touched: list[str] = []


class Seed(NamedTuple):
    """One event, seeded to a decision, plus the customer that owns it.

    Each scenario gets its own customer: opt-out is global per customer and
    permanent, and the contact cap and cooldown are per event, so sharing either
    would make one scenario's guardrails fire inside another's.
    """

    slug: str
    surface: str
    #: Chosen so the rules path resolves it. No Gemini quota, and repeatable.
    failure_reason: str
    root_cause: str
    intervention: str
    amount: float
    #: Whether this fixture's intervention must be in `CONTACT_INTERVENTIONS`.
    #:
    #: Asserted rather than assumed, because it is the premise the guardrail
    #: scenarios rest on: opt-out, the contact cap and the cooldown only gate
    #: contact-type interventions. If the matrix were ever re-tuned so this
    #: fixture resolved to a retry, B2 and B3 would stop exercising any guardrail
    #: at all and would still report BLOCKED-shaped passes for the wrong reason.
    contact_type: bool

    @property
    def event_id(self) -> str:
        return f"ptp_{TAG}_{self.slug}"

    @property
    def customer_ref(self) -> str:
        return f"cust_ptp_{TAG}_{self.slug}"


#: Contact-type, so opt-out, the contact cap and the cooldown all actually gate it.
#: Used by every guardrail scenario.
#:
#: The intervention here is `escalating_reminder_sequence`, not the cheaper
#: `reminder`, and that is the decision engine's call rather than a preference of
#: this harness. `receivable/genuine_delay` offers both; the sequence is likelier to
#: work and costs seven times as much, so it wins above a crossover near 170
#: currency units (see the receivable block of `app/decision/matrix.py`). At 900 the
#: sequence is the economically correct choice and the ERV comparison says so. Both
#: candidates are contact-type, so which one wins changes nothing about what the
#: guardrail scenarios below are testing.
def contact_seed(slug: str, amount: float = 900.00) -> Seed:
    return Seed(
        slug=slug,
        surface="receivable",
        failure_reason="cash flow delay, will pay next week",
        root_cause="genuine_delay",
        intervention="escalating_reminder_sequence",
        amount=amount,
        contact_type=True,
    )


#: Link-producing: `delayed_retry` -> `retry_simulated`, which creates a real
#: Razorpay payment link. Scenario C needs one, because a verification record can
#: only be written against a link an execution claims.
#:
#: Deliberately NOT contact-type. `delayed_retry` sits outside
#: `CONTACT_INTERVENTIONS`, so it consumes no cap slot and starts no cooldown —
#: nothing in scenario C's setup can suppress the follow-up except the payment
#: itself, which is the entire point of that scenario.
def link_seed(slug: str, amount: float = 1_650.00) -> Seed:
    return Seed(
        slug=slug,
        surface="payment",
        failure_reason="insufficient_funds",
        root_cause="insufficient_funds",
        intervention="delayed_retry",
        amount=amount,
        contact_type=False,
    )


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Record one assertion. Never raises, so a failure cannot hide later ones."""
    line = f"   {'PASS' if ok else 'FAIL'}  {label}"
    if not ok and detail:
        line += f"\n         {detail}"
    print(line)
    (passes if ok else failures).append(label)
    return ok


def note(text: str) -> None:
    print(f"         {text}")


def heading(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def yesterday() -> str:
    """A date that has definitely passed, in the ISO form the API takes."""
    return (date.today() - timedelta(days=1)).isoformat()


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# HTTP.
# ---------------------------------------------------------------------------


async def seed(client: httpx.AsyncClient, fixture: Seed) -> bool:
    """Ingest -> diagnose -> decide. Nothing is authorized and nothing executes."""
    response = await client.post(
        f"{API}/events",
        json={
            "event_id": fixture.event_id,
            "surface": fixture.surface,
            "amount": fixture.amount,
            "currency": "INR",
            "customer_ref": fixture.customer_ref,
            "raw_failure_reason": fixture.failure_reason,
        },
        timeout=60,
    )
    if response.status_code >= 400:
        return check(f"seed {fixture.slug}", False, f"ingest {response.status_code} {response.text[:200]}")

    diagnosis = await client.post(f"{API}/diagnose/{fixture.event_id}", timeout=120)
    if diagnosis.status_code >= 400:
        return check(f"seed {fixture.slug}", False, f"diagnose {diagnosis.status_code} {diagnosis.text[:200]}")

    decision = await client.post(f"{API}/decide/{fixture.event_id}", timeout=120)
    if decision.status_code >= 400:
        return check(f"seed {fixture.slug}", False, f"decide {decision.status_code} {decision.text[:200]}")

    got_cause = diagnosis.json()["root_cause"]
    got_intervention = decision.json()["recommended_intervention"]
    note(
        f"{fixture.event_id}  {fixture.amount:,.2f}  {got_cause} -> {got_intervention} "
        f"(ERV {decision.json()['expected_recovery_value']:,.2f})"
    )
    resolved = check(
        f"seed {fixture.slug} resolves to {fixture.root_cause} -> {fixture.intervention}",
        got_cause == fixture.root_cause and got_intervention == fixture.intervention,
        f"got {got_cause} -> {got_intervention}",
    )
    # The premise the scenario rests on, asserted against the live policy rules
    # rather than trusted. A contact fixture whose intervention drifted out of
    # CONTACT_INTERVENTIONS would silently stop being gated by opt-out, the cap and
    # the cooldown, and the guardrail scenarios would pass while testing nothing.
    is_contact = got_intervention in CONTACT_INTERVENTIONS
    gated = check(
        f"and it is {'contact-type, so the guardrails gate it' if fixture.contact_type else 'NOT contact-type, so no cap slot or cooldown is involved'}",
        is_contact == fixture.contact_type,
        f"{got_intervention!r} in CONTACT_INTERVENTIONS = {is_contact}, expected {fixture.contact_type}",
    )
    return resolved and gated


async def create_promise(
    client: httpx.AsyncClient, *, event_id: str, amount: float, promised_date: str
) -> httpx.Response:
    return await client.post(
        f"{API}/promises",
        json={
            "event_id": event_id,
            "promised_amount": amount,
            "promised_date": promised_date,
        },
        timeout=60,
    )


async def check_promise(client: httpx.AsyncClient, event_id: str) -> httpx.Response:
    return await client.post(f"{API}/promises/{event_id}/check", timeout=120)


async def event_status(event_id: str) -> str | None:
    document = await get_database()["events"].find_one({"event_id": event_id}, {"status": 1})
    return None if document is None else document.get("status")


async def executions_for(event_id: str) -> list[dict[str, Any]]:
    return (
        await get_database()["executions"].find({"event_id": event_id}).to_list(length=None)
    )


def show_check(payload: dict[str, Any]) -> None:
    """Print a PromiseCheck the way it matters: the re-check first."""
    print(f"         state           {payload['state_before']} -> {payload['state']} (changed={payload['changed']})")
    print(f"         re-checked at   {payload['payment_rechecked_at']}")
    print(f"         verifications   {payload['verifications_examined']} examined")
    print(f"         deadline passed {payload['deadline_passed']}")
    follow_up = payload.get("follow_up")
    if follow_up is None:
        print("         follow_up       null")
    else:
        print(
            f"         follow_up       sent={follow_up['sent']} verdict="
            f"{follow_up['policy_verdict']}/{follow_up['policy_reason']} "
            f"execution={follow_up['execution_id']}"
        )
    print(f"         detail          {payload['detail']}")


# ---------------------------------------------------------------------------
# Scenario A — a promise opens, and an open promise is left alone.
# ---------------------------------------------------------------------------


async def scenario_a(client: httpx.AsyncClient) -> None:
    heading("SCENARIO A — a promise is recorded and starts 'promised'")

    fixture = contact_seed("A")
    if not await seed(client, fixture):
        return

    status_before = await event_status(fixture.event_id)
    response = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=tomorrow()
    )
    print(f"   POST /promises -> HTTP {response.status_code}")
    if not check("promise created with 201", response.status_code == 201, response.text[:300]):
        return
    promise = response.json()
    touched.append(f"promises: {promise['id']} ({fixture.event_id}, state {promise['state']})")
    print(f"         {json.dumps(promise, indent=2)}")

    check("state is 'promised'", promise["state"] == "promised", str(promise["state"]))
    check("resolved_at is null while the promise is open", promise["resolved_at"] is None)
    check("follow_up_sent is False on a new promise", promise["follow_up_sent"] is False)
    check(
        "promised_date round-trips as the date sent, with no time invented",
        promise["promised_date"] == tomorrow(),
        f"{promise['promised_date']!r} != {tomorrow()!r}",
    )

    status_after = await event_status(fixture.event_id)
    check(
        "event moved to 'awaiting_promise'",
        status_after == "awaiting_promise",
        f"{status_before} -> {status_after}",
    )
    touched.append(f"events: {fixture.event_id} status {status_before} -> {status_after}")

    # Idempotency: the same commitment again is the same document, answered 200.
    again = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=tomorrow()
    )
    check(
        "re-posting the identical promise answers 200, not 201 or 409",
        again.status_code == 200,
        f"HTTP {again.status_code}: {again.text[:200]}",
    )
    check(
        "and returns the SAME document, not a second one",
        again.status_code == 200 and again.json()["id"] == promise["id"],
        f"{again.json().get('id')} vs {promise['id']}",
    )

    # A different amount for the same date is a conflict, not a retry.
    conflict = await create_promise(
        client,
        event_id=fixture.event_id,
        amount=fixture.amount + 100,
        promised_date=tomorrow(),
    )
    check(
        "a different amount on the same date is refused 409",
        conflict.status_code == 409,
        f"HTTP {conflict.status_code}: {conflict.text[:250]}",
    )
    if conflict.status_code == 409:
        note(str(conflict.json().get("detail"))[:220])

    stored = await get_database()["promises"].count_documents({"event_id": fixture.event_id})
    check("exactly one promise document exists for this event", stored == 1, f"{stored} found")

    # And the check before the date does nothing except re-check payment.
    resolved = await check_promise(client, fixture.event_id)
    check("check before the deadline -> 200", resolved.status_code == 200, resolved.text[:250])
    if resolved.status_code != 200:
        return
    payload = resolved.json()
    show_check(payload)
    check("state is still 'promised'", payload["state"] == "promised", str(payload["state"]))
    check("nothing changed", payload["changed"] is False)
    check("deadline_passed is False", payload["deadline_passed"] is False)
    check("no follow-up on an open promise", payload["follow_up"] is None)
    check(
        "payment was re-checked anyway — the re-check is unconditional",
        payload["payment_rechecked_at"] is not None,
    )
    after = await executions_for(fixture.event_id)
    check("and nothing was executed", len(after) == 0, f"{len(after)} execution(s)")


# ---------------------------------------------------------------------------
# Scenario B — broken, and the follow-up goes through policy and executes.
# ---------------------------------------------------------------------------


async def scenario_b(client: httpx.AsyncClient) -> None:
    heading("SCENARIO B — the date passed unpaid: 'broken', then an AUTHORIZED follow-up")

    fixture = contact_seed("B")
    if not await seed(client, fixture):
        return

    created = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=yesterday()
    )
    if not check("promise created for a date that has passed", created.status_code == 201, created.text[:250]):
        return
    promise = created.json()
    touched.append(f"promises: {promise['id']} ({fixture.event_id})")
    note(f"promised {promise['promised_amount']:,.2f} by {promise['promised_date']} — yesterday")

    before = await executions_for(fixture.event_id)
    response = await check_promise(client, fixture.event_id)
    if not check("check -> 200", response.status_code == 200, response.text[:400]):
        return
    payload = response.json()
    show_check(payload)

    check("state_before was 'promised'", payload["state_before"] == "promised")
    check("deadline_passed is True", payload["deadline_passed"] is True)
    check(
        "payment was re-checked BEFORE any follow-up",
        payload["payment_rechecked_at"] is not None,
    )
    follow_up = payload.get("follow_up")
    if not check("a follow-up was attempted", follow_up is not None):
        return

    check(
        "the follow-up went through the policy gate and was AUTHORIZED",
        follow_up["policy_verdict"] == "authorized" and follow_up["policy_reason"] == "ok",
        f"{follow_up['policy_verdict']}/{follow_up['policy_reason']}",
    )
    check("the follow-up was sent", follow_up["sent"] is True, str(follow_up))
    check(
        "it produced a real ExecutionRecord",
        bool(follow_up["execution_id"]),
        str(follow_up["execution_id"]),
    )
    check(
        "the action is a contact, not a payment-rail action",
        follow_up["action_type"] == "contact_logged",
        str(follow_up["action_type"]),
    )
    check(
        "promise ended at 'reevaluating' — broken, then chased",
        payload["state"] == "reevaluating",
        str(payload["state"]),
    )

    after = await executions_for(fixture.event_id)
    check(
        "exactly one execution record was written by the follow-up",
        len(after) == len(before) + 1,
        f"{len(before)} -> {len(after)}",
    )
    if after:
        touched.append(f"executions: {after[-1]['_id']} ({fixture.event_id}, follow-up)")

    stored = await get_database()["promises"].find_one({"event_id": fixture.event_id})
    check(
        "follow_up_sent is now True on the stored promise",
        stored is not None and stored.get("follow_up_sent") is True,
        str(stored.get("follow_up_sent") if stored else None),
    )
    check(
        "resolved_at is set now that the promise is no longer open",
        stored is not None and stored.get("resolved_at") is not None,
    )

    # The cap and the cooldown are visible in the verdict trail even though neither
    # fired here. That the checks RAN is the assertion; driving the cap to failure
    # would need three contacts across two 24-hour cooldowns.
    verdicts = await client.get(f"{API}/policy-verdicts?event_id={fixture.event_id}&history=true", timeout=60)
    trail = verdicts.json()[0]["checks_performed"] if verdicts.json() else []
    names = [check_name(entry) for entry in trail]
    note(f"checks performed: {', '.join(names)}")
    check(
        "the follow-up's verdict ran the contact cap and cooldown checks",
        "contact_cap" in names and "contact_cooldown" in names,
        str(names),
    )
    check(
        "and the opt-out check",
        "customer_opt_out" in names,
        str(names),
    )

    # A second check must not contact the customer again.
    second = await check_promise(client, fixture.event_id)
    payload2 = second.json()
    show_check(payload2)
    check(
        "a second check sends nothing — follow_up_sent already True",
        payload2["follow_up"] is None,
        str(payload2["follow_up"]),
    )
    final = await executions_for(fixture.event_id)
    check(
        "and wrote no second execution record",
        len(final) == len(after),
        f"{len(after)} -> {len(final)}",
    )


# ---------------------------------------------------------------------------
# Scenario B2 — the cooldown blocks the follow-up.
# ---------------------------------------------------------------------------


async def scenario_b2(client: httpx.AsyncClient) -> None:
    heading("SCENARIO B2 — a recent contact means the follow-up is BLOCKED on the cooldown")

    fixture = contact_seed("B2")
    if not await seed(client, fixture):
        return

    # Contact the customer through the ordinary Stage 4/5 path first. This is the
    # prior contact the cooldown will measure from — a real authorized, executed
    # reminder, minutes old.
    authorize = await client.post(f"{API}/authorize/{fixture.event_id}", timeout=60)
    execute = await client.post(f"{API}/execute/{fixture.event_id}", timeout=120)
    check(
        "a prior contact was authorized and executed through the normal path",
        authorize.status_code in (200, 201)
        and execute.status_code in (200, 201)
        and execute.json()["status"] == "completed",
        f"authorize {authorize.status_code}, execute {execute.status_code}",
    )
    if execute.status_code in (200, 201):
        note(
            f"prior contact: {execute.json()['intervention']} at "
            f"{execute.json()['executed_at']} (execution {execute.json()['id']})"
        )
        touched.append(f"executions: {execute.json()['id']} ({fixture.event_id}, prior contact)")

    created = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=yesterday()
    )
    if not check("promise created for yesterday", created.status_code == 201, created.text[:250]):
        return
    touched.append(f"promises: {created.json()['id']} ({fixture.event_id})")

    before = await executions_for(fixture.event_id)
    response = await check_promise(client, fixture.event_id)
    if not check("check -> 200", response.status_code == 200, response.text[:400]):
        return
    payload = response.json()
    show_check(payload)

    follow_up = payload.get("follow_up")
    if not check("a follow-up was attempted and reported", follow_up is not None):
        return
    check(
        "policy BLOCKED it on the cooldown",
        follow_up["policy_verdict"] == "blocked"
        and follow_up["policy_reason"] == "cooldown_active",
        f"{follow_up['policy_verdict']}/{follow_up['policy_reason']}",
    )
    check("nothing was sent", follow_up["sent"] is False)
    check("no execution id, because nothing executed", follow_up["execution_id"] is None)

    after = await executions_for(fixture.event_id)
    check(
        "no new execution record — the PTP path is NOT exempt from the cooldown",
        len(after) == len(before),
        f"{len(before)} -> {len(after)}",
    )
    check(
        "the promise stays 'broken' so a later check can retry after the cooldown",
        payload["state"] == "broken",
        str(payload["state"]),
    )
    stored = await get_database()["promises"].find_one({"event_id": fixture.event_id})
    check(
        "follow_up_sent stays False — a blocked follow-up is not a sent one",
        stored is not None and stored.get("follow_up_sent") is False,
        str(stored.get("follow_up_sent") if stored else None),
    )


# ---------------------------------------------------------------------------
# Scenario B3 — opt-out blocks the follow-up.
# ---------------------------------------------------------------------------


async def scenario_b3(client: httpx.AsyncClient) -> None:
    heading("SCENARIO B3 — an opted-out customer is not chased, promise or no promise")

    fixture = contact_seed("B3")
    if not await seed(client, fixture):
        return

    opt_out = await client.post(
        f"{API}/opt-out/{fixture.customer_ref}",
        json={"reason": "s6b verification: consent must gate PTP follow-ups too"},
        timeout=60,
    )
    check(
        "customer opted out through the Stage 4 endpoint",
        opt_out.status_code in (200, 201),
        f"HTTP {opt_out.status_code}: {opt_out.text[:200]}",
    )
    touched.append(f"customer_opt_outs: {fixture.customer_ref}")

    created = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=yesterday()
    )
    if not check(
        "a promise can still be RECORDED for an opted-out customer",
        created.status_code == 201,
        created.text[:250],
    ):
        return
    note("recording what somebody said is not contacting them; only the follow-up is gated")
    touched.append(f"promises: {created.json()['id']} ({fixture.event_id})")

    before = await executions_for(fixture.event_id)
    response = await check_promise(client, fixture.event_id)
    if not check("check -> 200", response.status_code == 200, response.text[:400]):
        return
    payload = response.json()
    show_check(payload)

    follow_up = payload.get("follow_up")
    if not check("a follow-up was attempted and reported", follow_up is not None):
        return
    check(
        "policy BLOCKED it on consent",
        follow_up["policy_verdict"] == "blocked"
        and follow_up["policy_reason"] == "customer_opted_out",
        f"{follow_up['policy_verdict']}/{follow_up['policy_reason']}",
    )
    check("nothing was sent", follow_up["sent"] is False)
    after = await executions_for(fixture.event_id)
    check(
        "no execution record — opt-out gates the PTP path exactly as it gates Stage 5",
        len(after) == len(before),
        f"{len(before)} -> {len(after)}",
    )
    check("the promise stays 'broken'", payload["state"] == "broken", str(payload["state"]))


# ---------------------------------------------------------------------------
# Scenario C — THE CRITICAL TEST.
# ---------------------------------------------------------------------------


def webhook_body(*, event: str, entity: dict[str, Any], created_at: int) -> bytes:
    """Serialise a Razorpay webhook body to the exact bytes that get signed.

    Same construction as `scripts/s6_verify.py`: the bytes returned are the bytes
    signed and the bytes sent, so no second `json.dumps` can drift from the first.
    """
    body = {
        "account_id": "acc_test",
        "contains": ["payment_link"],
        "created_at": created_at,
        "entity": "event",
        "event": event,
        "payload": {"payment_link": {"entity": entity}},
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


async def fetch_link(client: httpx.AsyncClient, link_id: str) -> dict[str, Any]:
    """Fetch a payment link object from Razorpay's live API."""
    creds = credentials_from_settings()
    response = await client.get(
        f"{RAZORPAY}/payment_links/{link_id}",
        auth=(creds.key_id, creds.key_secret),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


async def scenario_c_control(client: httpx.AsyncClient) -> int:
    """The control for scenario C: same fixture shape, same passed deadline, no money.

    Run on its OWN event, deliberately. Checking the test event before paying it
    would set `follow_up_sent=True` on that promise, and the critical check would
    then have two independent reasons to send nothing — the money and the flag. The
    result would look identical and prove half as much.

    Returns the number of executions this control produced, which is the number the
    critical scenario must NOT produce.
    """
    heading("SCENARIO C-CONTROL — the same setup with no payment DOES chase")

    fixture = link_seed("CCTRL")
    if not await seed(client, fixture):
        return -1

    created = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=yesterday()
    )
    if not check("control promise created, dated yesterday", created.status_code == 201, created.text[:250]):
        return -1
    touched.append(f"promises: {created.json()['id']} ({fixture.event_id}, control)")

    response = await check_promise(client, fixture.event_id)
    if not check("control check -> 200", response.status_code == 200, response.text[:400]):
        return -1
    payload = response.json()
    show_check(payload)

    follow_up = payload.get("follow_up")
    check(
        "the passed deadline broke the promise",
        payload["state_before"] == "promised" and payload["state"] in {"broken", "reevaluating"},
        f"{payload['state_before']} -> {payload['state']}",
    )
    if not check(
        "a follow-up WAS attempted on this fixture shape",
        follow_up is not None,
        "no attempt — the critical test below would prove nothing",
    ):
        return -1
    check(
        "and policy authorized it, so nothing but payment could suppress it",
        follow_up["policy_verdict"] == "authorized",
        f"{follow_up['policy_verdict']}/{follow_up['policy_reason']}",
    )
    check("it was sent", follow_up["sent"] is True, str(follow_up["detail"]))

    executions = await executions_for(fixture.event_id)
    check(
        "and it wrote an execution record",
        len(executions) == 1,
        f"{len(executions)} execution(s)",
    )
    for execution in executions:
        touched.append(f"executions: {execution['_id']} ({fixture.event_id}, control follow-up)")
        if execution.get("razorpay_payment_link_id"):
            touched.append(
                f"razorpay: payment link {execution['razorpay_payment_link_id']} CREATED (test mode)"
            )
    print(
        f"\n   CONTROL ESTABLISHED: an unpaid past-due promise on this fixture shape\n"
        f"   produces {len(executions)} execution(s). The critical scenario below is\n"
        f"   identical except that the money arrives first, and must produce 0."
    )
    return len(executions)


async def scenario_c(client: httpx.AsyncClient, control_executions: int) -> None:
    heading(
        "SCENARIO C — THE CRITICAL TEST: money arrived, deadline passed, NO FOLLOW-UP"
    )
    print(
        "  Identical to the control above: same surface, same root cause, same\n"
        "  intervention, a promise dated YESTERDAY, and a promise that has never been\n"
        "  followed up. The one difference is that the money arrives before the check."
    )

    fixture = link_seed("C")
    if not await seed(client, fixture):
        return

    # A real link, so a real verification can be matched to this event. `delayed_retry`
    # is not a contact intervention, so this execution consumes no contact-cap slot and
    # starts no cooldown — it cannot be what suppresses the follow-up later.
    authorize = await client.post(f"{API}/authorize/{fixture.event_id}", timeout=60)
    execute = await client.post(f"{API}/execute/{fixture.event_id}", timeout=180)
    if not check(
        "a real Razorpay payment link was created for this event",
        execute.status_code in (200, 201)
        and execute.json().get("razorpay_payment_link_id"),
        f"authorize {authorize.status_code}, execute {execute.status_code}: {execute.text[:250]}",
    ):
        return
    record = execute.json()
    link_id = record["razorpay_payment_link_id"]
    note(f"execution {record['id']}  {record['intervention']} -> {link_id}")
    note(f"LIVE URL  {record['razorpay_payment_link_url']}")
    touched.append(f"executions: {record['id']} ({fixture.event_id}, {record['action_type']})")
    touched.append(f"razorpay: payment link {link_id} CREATED (test mode)")

    created = await create_promise(
        client, event_id=fixture.event_id, amount=fixture.amount, promised_date=yesterday()
    )
    if not check(
        "promise created, dated yesterday — this deadline HAS passed",
        created.status_code == 201,
        created.text[:250],
    ):
        return
    promise = created.json()
    touched.append(f"promises: {promise['id']} ({fixture.event_id})")
    check(
        "and it has never been followed up, so no flag can suppress the follow-up",
        promise["follow_up_sent"] is False and promise["state"] == "promised",
        f"state={promise['state']} follow_up_sent={promise['follow_up_sent']}",
    )

    # ---- the money arrives, through the real signed webhook path ----
    link = await fetch_link(client, link_id)
    entity = dict(link)
    really_paid = entity.get("status") == "paid" and (entity.get("amount_paid") or 0) > 0
    if really_paid:
        print("\n   This link was genuinely paid at Razorpay. No field is overridden.")
    else:
        entity["status"] = "paid"
        entity["amount_paid"] = entity["amount"]
        print(
            "\n   OVERRIDDEN, because completing a card checkout needs a browser this\n"
            "   harness does not have:\n"
            f"     status      : {link.get('status')!r} -> 'paid'\n"
            f"     amount_paid : {link.get('amount_paid')!r} -> {entity['amount_paid']!r}\n"
            f"   Every other field is Razorpay's own value for {entity['id']}."
        )

    razorpay_event_id = f"s6b_{TAG}_paid_C"
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
    )
    delivery = await client.post(
        f"{API}/webhooks/razorpay",
        content=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: expected_signature(body=body, secret=webhook_secret()),
            EVENT_ID_HEADER: razorpay_event_id,
        },
        timeout=60,
    )
    print(f"   webhook -> HTTP {delivery.status_code}  {delivery.json().get('detail')}")
    check("the signed payment_link.paid was accepted", delivery.status_code == 200)
    check("and processed", delivery.json().get("processed") is True, delivery.text[:250])

    verification = await get_database()["verifications"].find_one(
        {"razorpay_event_id": razorpay_event_id}
    )
    if not check("a VerificationRecord exists for the payment", verification is not None):
        return
    touched.append(f"verifications: {verification['_id']} (recovered for {fixture.event_id})")
    check(
        "its outcome is 'recovered'",
        verification["outcome"] == "recovered",
        str(verification["outcome"]),
    )
    check(
        "and it belongs to this scenario's event",
        verification["event_id"] == fixture.event_id,
        str(verification["event_id"]),
    )

    # ---- THE ASSERTION THE WHOLE DESIGN EXISTS FOR ----
    print("\n   -- THE CRITICAL CHECK: passed deadline, unchased promise, money now in --")
    before = await executions_for(fixture.event_id)
    response = await check_promise(client, fixture.event_id)
    if not check("check -> 200", response.status_code == 200, response.text[:400]):
        return
    payload = response.json()
    show_check(payload)

    check(
        "the promise is HONORED",
        payload["state"] == "honored",
        f"state={payload['state']!r}",
    )
    check(
        "NO FOLLOW-UP WAS SENT — follow_up is null",
        payload["follow_up"] is None,
        f"follow_up={payload['follow_up']!r}",
    )
    check(
        "state_before was 'promised', so this is the FIRST check of this promise",
        payload["state_before"] == "promised",
        str(payload["state_before"]),
    )
    check(
        "the deadline HAD passed, so suppression was not the deadline's doing",
        payload["deadline_passed"] is True,
        str(payload["deadline_passed"]),
    )
    check(
        "the response names the verification that honored it",
        bool(payload["recovered_verification_id"])
        and payload["recovered_verification_id"] == str(verification["_id"]),
        f"{payload['recovered_verification_id']} vs {verification['_id']}",
    )
    check(
        "the re-check is reported explicitly, so a caller can see it happened",
        payload["payment_rechecked_at"] is not None
        and payload["verifications_examined"] >= 1,
        f"at={payload['payment_rechecked_at']} examined={payload['verifications_examined']}",
    )
    check(
        "the detail says out loud that no follow-up was sent",
        "NO follow-up" in payload["detail"],
        payload["detail"],
    )

    after = await executions_for(fixture.event_id)
    check(
        "NOT ONE new execution record — proved by count, not by assertion",
        len(after) == len(before),
        f"{len(before)} -> {len(after)}",
    )
    check(
        f"the control produced {control_executions} follow-up execution(s) here, this produced 0",
        control_executions >= 1 and len(after) == len(before),
        f"control={control_executions} critical delta={len(after) - len(before)}",
    )

    stored = await get_database()["promises"].find_one({"_id": ObjectId(promise["id"])})
    check(
        "the stored promise is honored with resolved_at set",
        stored is not None
        and stored["state"] == "honored"
        and stored.get("resolved_at") is not None,
        str(stored),
    )
    check(
        "follow_up_sent is still False — nothing was ever sent for this promise",
        stored is not None and stored.get("follow_up_sent") is False,
        str(stored.get("follow_up_sent") if stored else None),
    )

    status = await event_status(fixture.event_id)
    check(
        "the event itself is 'recovered'",
        status == "recovered",
        f"status={status}",
    )
    touched.append(f"events: {fixture.event_id} status -> {status}")

    # An honored promise is terminal, and a re-check still re-checks payment.
    idem = await check_promise(client, fixture.event_id)
    idem_payload = idem.json()
    check(
        "re-checking an honored promise is a safe no-op",
        idem.status_code == 200
        and idem_payload["state"] == "honored"
        and idem_payload["changed"] is False
        and idem_payload["follow_up"] is None,
        json.dumps(idem_payload)[:300],
    )
    check(
        "and it re-checked payment again rather than trusting the stored state",
        idem_payload["payment_rechecked_at"] is not None,
    )
    final = await executions_for(fixture.event_id)
    check(
        "the second check wrote nothing either",
        len(final) == len(after),
        f"{len(after)} -> {len(final)}",
    )

    # A promise cannot be recorded against money that has already arrived.
    settled = await create_promise(
        client,
        event_id=fixture.event_id,
        amount=fixture.amount,
        promised_date=(date.today() + timedelta(days=7)).isoformat(),
    )
    check(
        "a NEW promise on a recovered event is refused 422",
        settled.status_code == 422,
        f"HTTP {settled.status_code}: {settled.text[:250]}",
    )
    if settled.status_code == 422:
        note(str(settled.json().get("detail"))[:220])


# ---------------------------------------------------------------------------
# Readback.
# ---------------------------------------------------------------------------


async def readback(client: httpx.AsyncClient) -> None:
    heading("READBACK — GET /promises")

    response = await client.get(f"{API}/promises?history=true", timeout=60)
    check("GET /promises?history=true -> 200", response.status_code == 200, response.text[:250])
    if response.status_code != 200:
        return
    records = response.json()
    mine = [r for r in records if TAG in r["event_id"]]
    print(f"  {len(records)} promise(s) total, {len(mine)} from this run:")
    for record in sorted(mine, key=lambda r: r["event_id"]):
        print(
            f"    {record['promised_date']}  {record['state']:<13} "
            f"{record['promised_amount']:>9,.2f}  follow_up_sent={str(record['follow_up_sent']):<5} "
            f"{record['event_id']}"
        )

    stored = await get_database()["promises"].count_documents({})
    check(
        "the history view returns every stored promise",
        len(records) == stored,
        f"http={len(records)} mongo={stored}",
    )

    states = {r["state"] for r in mine}
    # All four states, and each from the scenario that can actually leave a promise
    # sitting in it: `promised` from A (dated tomorrow, nothing due), `broken` from
    # B2 and B3 (policy refused the follow-up, so the promise stays broken for a
    # later retry), `reevaluating` from B and C-CONTROL (chased), `honored` from C.
    # `broken` is only observable here BECAUSE a blocked follow-up leaves it — a
    # successfully chased promise passes through broken to reevaluating inside one
    # call, so B alone would never show it in a readback.
    expected_states = {"promised", "broken", "reevaluating", "honored"}
    check(
        "this run exercised promised, broken, reevaluating and honored",
        expected_states <= states,
        f"states seen: {sorted(states)}, missing: {sorted(expected_states - states)}",
    )

    broken = await client.get(f"{API}/promises?state=broken&history=true", timeout=60)
    broken_records = broken.json() if broken.status_code == 200 else []
    check(
        "the state filter returns only that state, and is not vacuously empty",
        broken.status_code == 200
        and len(broken_records) >= 1
        and all(r["state"] == "broken" for r in broken_records),
        f"HTTP {broken.status_code}, {len(broken_records)} record(s): "
        f"{sorted({r['state'] for r in broken_records})}",
    )
    bad = await client.get(f"{API}/promises?state=pending", timeout=60)
    check("an unknown state filter is refused 422", bad.status_code == 422, bad.text[:200])

    missing = await client.post(f"{API}/promises/no_such_event_{TAG}/check", timeout=60)
    check(
        "checking an event with no promise is 404, not an invented promise",
        missing.status_code == 404,
        f"HTTP {missing.status_code}: {missing.text[:200]}",
    )

    from app.ptp import count_open_overdue

    overdue = await count_open_overdue()
    note(
        f"count_open_overdue() = {overdue} — the query the deliberately-unbuilt "
        "scheduler would run"
    )


async def main() -> int:
    await connect_to_mongo()
    print(f"Stage 6 Part B — promise-to-pay against {API}")
    print(f"run tag {TAG}")
    print(f"rulebook in force: {current_fingerprint()}")
    print(
        "\nEvery promise below is recorded through POST /promises with an explicit\n"
        "amount and date. No free text is parsed and Gemini is not involved in the\n"
        "promise path at all."
    )
    try:
        async with httpx.AsyncClient() as client:
            await scenario_a(client)
            await scenario_b(client)
            await scenario_b2(client)
            await scenario_b3(client)
            control_executions = await scenario_c_control(client)
            await scenario_c(client, control_executions)
            await readback(client)
    finally:
        await close_mongo_connection()

    heading("SUMMARY")
    print(f"  {len(passes)} passed, {len(failures)} failed")
    for label in failures:
        print(f"    FAILED: {label}")
    print("\n  Real state this run changed:")
    for line in touched:
        print(f"    - {line}")
    print(
        "\n  The safety property, restated as what was measured: an unpaid past-due\n"
        "  promise on the C fixture shape produces a follow-up execution (C-CONTROL,\n"
        "  on its own event). The same shape with the money in produces honored,\n"
        "  follow_up null, and an execution count that does not move — from a promise\n"
        "  that was still 'promised' with follow_up_sent False, so payment was the\n"
        "  only thing that could have suppressed the contact."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
