"""Stage 5 — execution end to end against real Razorpay test mode.

Every payment link printed by this script is a live test-mode link at a real URL.
Nothing here checks whether anybody paid: that question does not exist in Stage 5,
and the record this stage writes has no field capable of answering it.

Seven fixtures, one per executable intervention, chosen so the whole
diagnose→decide→authorize chain resolves deterministically without the LLM. Between
them they cover all three action types:

* `payment_method_update_link`, `recovery_payment_link`  -> payment_link_generated
* `immediate_retry`, `delayed_retry`                     -> retry_simulated
* `reminder`, `escalating_reminder_sequence`,
  `manual_escalation`                                    -> contact_logged

Each fixture is its own event, so the contact cap and cooldown — both scoped per
event — start clean and no fixture can block another.

Phases:

1. Seed all seven. Diagnose and decide only; nothing is authorized.
2. Authorize each. Every one must come back `authorized`, since a blocked fixture
   would make the execution test unreachable rather than failing it.
3. Execute each. One real Razorpay call per link and per retry, and a rendered
   template per contact. The URL, the link id and the send timestamp are printed.
4. Idempotency: execute every fixture a second time. Each must return 200 with a
   byte-identical record — same link id, same `executed_at` — and the collection
   must not grow.
5. The cooldown re-point: re-authorize an executed contact-type event and read the
   `contact_cooldown` entry in the fresh trail. It must name the execution's
   `executed_at`, not the authorizing verdict's `evaluated_at`. This is the whole
   point of Stage 5 for Stage 4.
6. What the records do NOT say, read back over HTTP.

Run:  python scripts/s5_verify.py http://127.0.0.1:8123 [tag]
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models.policy import check_failed, check_name
from app.policy import current_fingerprint, rules

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
TAG = (
    sys.argv[2]
    if len(sys.argv) > 2
    else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
)

#: One customer per run. Opt-out state is global per customer and permanent, so a
#: fixed reference would eventually collide with another script's opt-out and every
#: fixture here would come back blocked on consent.
CUSTOMER = f"cust_s5_{TAG}"


class Fixture(NamedTuple):
    """One executable intervention and inputs that deterministically produce it."""

    slug: str
    surface: str
    #: Resolved by the rules path, so no Gemini quota is spent and runs repeat.
    failure_reason: str
    root_cause: str
    intervention: str
    action_type: str
    #: Inside the autonomous tier (< 5,000 INR) unless the intervention needs a
    #: large amount to win its matrix comparison, in which case still < 25,000 so
    #: `amount_tier` routes to auto rather than to a human.
    amount: float

    @property
    def event_id(self) -> str:
        return f"exe_S5_{TAG}_{self.slug}"


FIXTURES = (
    # -- payment_link_generated: two real Razorpay links -------------------------
    Fixture(
        slug="PMUL",
        surface="payment",
        failure_reason="card_expired",
        root_cause="card_expired",
        intervention="payment_method_update_link",
        action_type="payment_link_generated",
        amount=2_400.00,
    ),
    Fixture(
        slug="RPL",
        surface="checkout",
        failure_reason="payment_method_unavailable",
        root_cause="payment_method_unavailable",
        intervention="recovery_payment_link",
        action_type="payment_link_generated",
        amount=3_100.00,
    ),
    # -- retry_simulated: a link too, recorded as a retry ------------------------
    Fixture(
        slug="IRETRY",
        surface="payment",
        failure_reason="gateway_timeout",
        root_cause="temporary_processing_error",
        intervention="immediate_retry",
        action_type="retry_simulated",
        amount=1_800.00,
    ),
    Fixture(
        slug="DRETRY",
        surface="payment",
        failure_reason="insufficient_funds",
        root_cause="insufficient_funds",
        intervention="delayed_retry",
        action_type="retry_simulated",
        amount=2_050.00,
    ),
    # -- contact_logged: no Razorpay call at all ---------------------------------
    # A single reminder beats the escalating sequence below roughly 170 INR, so the
    # two receivable fixtures differ only in amount.
    Fixture(
        slug="REMIND",
        surface="receivable",
        failure_reason="cash flow delay, will pay next week",
        root_cause="genuine_delay",
        intervention="reminder",
        action_type="contact_logged",
        amount=120.00,
    ),
    Fixture(
        slug="ESCAL",
        surface="receivable",
        failure_reason="cash flow delay, will pay next week",
        root_cause="genuine_delay",
        intervention="escalating_reminder_sequence",
        action_type="contact_logged",
        amount=4_400.00,
    ),
    Fixture(
        slug="MANUAL",
        surface="receivable",
        failure_reason="no_response",
        root_cause="non_responsive",
        intervention="manual_escalation",
        action_type="contact_logged",
        amount=4_800.00,
    ),
)

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Report one check. `detail` explains a failure, so it prints only on one.

    Several details here are phrased as the reason something went wrong ("absent
    from…"), which read as contradictions when echoed next to a PASS.
    """
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{'' if ok else f'  [{detail}]'}")
    if not ok:
        problems.append(f"{label} — {detail}" if detail else label)
    return ok


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def post(path: str, payload: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get(path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=90) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def detail_for(trail: list[str], name: str) -> str:
    for entry in trail:
        if check_name(entry) == name:
            return entry.split(": ", 1)[1].split("(", 1)[1].rsplit(")", 1)[0]
    return ""


# ---------------------------------------------------------------------------
# 1. Seed
# ---------------------------------------------------------------------------


def seed(fixture: Fixture) -> bool:
    status, body = post(
        "/events",
        {
            "event_id": fixture.event_id,
            "surface": fixture.surface,
            "amount": fixture.amount,
            "currency": "INR",
            "customer_ref": CUSTOMER,
            "raw_failure_reason": fixture.failure_reason,
        },
    )
    if status >= 400:
        return check(f"ingest {fixture.event_id}", False, f"{status} {body}")

    status, diagnosis = post(f"/diagnose/{fixture.event_id}")
    if status >= 400:
        return check(f"diagnose {fixture.event_id}", False, f"{status} {diagnosis}")

    status, decision = post(f"/decide/{fixture.event_id}")
    if status >= 400:
        return check(f"decide {fixture.event_id}", False, f"{status} {decision}")

    print(
        f"   {fixture.slug:<7} {fixture.amount:>9,.2f}  {diagnosis['root_cause']:<28} "
        f"{diagnosis['confidence']:.2f} {diagnosis['method']:<6} -> "
        f"{decision['recommended_intervention']:<30} ERV "
        f"{decision['expected_recovery_value']:>9,.2f}"
    )
    return check(
        f"{fixture.slug} resolves to {fixture.root_cause} -> {fixture.intervention}",
        diagnosis["root_cause"] == fixture.root_cause
        and decision["recommended_intervention"] == fixture.intervention,
        f"got {diagnosis['root_cause']} -> {decision['recommended_intervention']}",
    )


# ---------------------------------------------------------------------------
# 3. Execute
# ---------------------------------------------------------------------------


def show_record(record: dict[str, Any], indent: str = "     ") -> None:
    print(f"{indent}action_type   {record['action_type']}")
    print(f"{indent}intervention  {record['intervention']}")
    print(f"{indent}status        {record['status']}")
    print(f"{indent}executed_at   {record['executed_at']}")
    if record.get("razorpay_payment_link_id"):
        print(f"{indent}link id       {record['razorpay_payment_link_id']}")
        print(f"{indent}LIVE URL      {record['razorpay_payment_link_url']}")
    if record.get("contact_channel"):
        print(f"{indent}channel       {record['contact_channel']}")
        print(f"{indent}summary       {record['contact_message_summary']}")
    if record.get("failure_reason"):
        print(f"{indent}failed why    {record['failure_reason']}")


async def main() -> None:
    await connect_to_mongo()
    database = get_database()

    print(f"Stage 5 — execution end to end against {BASE}")
    print(f"run tag {TAG}, customer {CUSTOMER}")
    print(f"rulebook in force: {current_fingerprint()}")
    print(f"cooldown measured from: {rules.COOLDOWN_MEASURED_FROM}")
    print(
        "\nEvery link below is a real Razorpay TEST-MODE payment link. No claim is\n"
        "made anywhere that money came back — Stage 5 records what was done, and\n"
        "the record has no field that could say otherwise."
    )

    before = await database["executions"].count_documents({})

    # -- 1 -------------------------------------------------------------------
    section("1. Seed all seven fixtures (diagnose and decide; nothing authorized)")
    for fixture in FIXTURES:
        if not seed(fixture):
            print("\nABORT: a fixture did not resolve as intended.")
            sys.exit(1)

    # -- 2 -------------------------------------------------------------------
    section("2. Authorize each one")
    verdicts: dict[str, dict[str, Any]] = {}
    for fixture in FIXTURES:
        status, verdict = post(f"/authorize/{fixture.event_id}")
        if status >= 400:
            check(f"authorize {fixture.slug}", False, f"{status} {verdict}")
            sys.exit(1)
        verdicts[fixture.slug] = verdict
        check(
            f"{fixture.slug:<7} authorized (v{verdict['version']}, "
            f"{verdict['rulebook_fingerprint']})",
            verdict["verdict"] == "authorized" and verdict["reason"] == "ok",
            f"{verdict['verdict']}/{verdict['reason']}",
        )

    # -- 3 -------------------------------------------------------------------
    section("3. Execute each one — real Razorpay calls for links and retries")
    records: dict[str, dict[str, Any]] = {}
    for fixture in FIXTURES:
        status, record = post(f"/execute/{fixture.event_id}")
        print(f"\n   {fixture.slug}  HTTP {status}")
        if status >= 400:
            check(f"execute {fixture.slug}", False, f"{status} {record}")
            continue
        records[fixture.slug] = record
        show_record(record)

        check(
            f"{fixture.slug} created a NEW record (201)",
            status == 201,
            f"got {status}",
        )
        check(
            f"{fixture.slug} action_type is {fixture.action_type}",
            record["action_type"] == fixture.action_type,
            record["action_type"],
        )
        check(
            f"{fixture.slug} status is completed",
            record["status"] == "completed",
            f"{record['status']}: {record.get('failure_reason')}",
        )
        check(
            f"{fixture.slug} names the verdict that authorized it",
            record["policy_verdict_id"] == verdicts[fixture.slug]["id"],
            f"{record['policy_verdict_id']} vs {verdicts[fixture.slug]['id']}",
        )

        if fixture.action_type in {"payment_link_generated", "retry_simulated"}:
            url = record.get("razorpay_payment_link_url") or ""
            check(
                f"{fixture.slug} carries a live https Razorpay URL",
                url.startswith("https://"),
                url or "(none)",
            )
            check(
                f"{fixture.slug} carries a Razorpay link id",
                bool(record.get("razorpay_payment_link_id")),
                str(record.get("razorpay_payment_link_id")),
            )
            check(
                f"{fixture.slug} logged no contact fields",
                record.get("contact_channel") is None
                and record.get("contact_message_summary") is None,
                f"{record.get('contact_channel')} / "
                f"{record.get('contact_message_summary')}",
            )
        else:
            check(
                f"{fixture.slug} made NO Razorpay call",
                record.get("razorpay_payment_link_id") is None
                and record.get("razorpay_payment_link_url") is None,
                f"{record.get('razorpay_payment_link_id')} / "
                f"{record.get('razorpay_payment_link_url')}",
            )
            summary = record.get("contact_message_summary") or ""
            channel = record.get("contact_channel") or ""
            # The summary is "<template_id> v<n> via <channel> — <subject>": a
            # template identity and a rendered subject, never the body. Checked
            # structurally so a body accidentally stored here would fail.
            check(
                f"{fixture.slug} recorded a template summary, not a body",
                bool(channel)
                and f" via {channel} —" in summary
                and summary.split(" v", 1)[0] != "",
                summary or "(none)",
            )

    check(
        "all three action types were exercised",
        {record["action_type"] for record in records.values()}
        == {"payment_link_generated", "retry_simulated", "contact_logged"},
        str(sorted({record["action_type"] for record in records.values()})),
    )
    check(
        "all seven executable interventions were exercised",
        {record["intervention"] for record in records.values()}
        == {fixture.intervention for fixture in FIXTURES},
        str(sorted({record["intervention"] for record in records.values()})),
    )

    after_first = await database["executions"].count_documents({})
    check(
        "the collection grew by exactly seven",
        after_first - before == len(FIXTURES),
        f"{before} -> {after_first}",
    )

    # -- 4 -------------------------------------------------------------------
    section("4. Idempotency — execute every fixture a second time")
    print(
        "   A 200 means nothing happened. The record must come back byte-identical:\n"
        "   a fresh Razorpay call would mint a different link id, and a re-stamped\n"
        "   executed_at would move a cooldown that has already started.\n"
    )
    for fixture in FIXTURES:
        if fixture.slug not in records:
            continue
        first = records[fixture.slug]
        status, again = post(f"/execute/{fixture.event_id}")
        same = again == first
        check(
            f"{fixture.slug:<7} second execute returned 200 and the same record",
            status == 200 and same,
            f"HTTP {status}"
            + (
                ""
                if same
                else "; differs in "
                + str(
                    sorted(
                        key
                        for key in set(first) | set(again)
                        if first.get(key) != again.get(key)
                    )
                )
            ),
        )

    after_second = await database["executions"].count_documents({})
    check(
        "no second execute wrote a document",
        after_second == after_first,
        f"{after_first} -> {after_second}",
    )
    check(
        "the unique index that guarantees it is live",
        "uniq_policy_verdict_id"
        in await database["executions"].index_information(),
        str(sorted(await database["executions"].index_information())),
    )

    # -- 5 -------------------------------------------------------------------
    section("5. The cooldown now measures from the real send time")
    print(
        "   Re-authorize an executed contact-type event. The cooldown check must\n"
        "   name the execution's executed_at, NOT the first verdict's evaluated_at.\n"
        "   Those two differ by the Razorpay round trip, so the trail can only be\n"
        "   quoting one of them.\n"
    )
    for slug in ("PMUL", "REMIND", "MANUAL"):
        if slug not in records:
            continue
        fixture = next(f for f in FIXTURES if f.slug == slug)
        executed_at = records[slug]["executed_at"]
        evaluated_at = verdicts[slug]["evaluated_at"]

        status, verdict = post(f"/authorize/{fixture.event_id}")
        if status >= 400:
            check(f"re-authorize {slug}", False, f"{status} {verdict}")
            continue

        cooldown = detail_for(verdict["checks_performed"], "contact_cooldown")
        print(f"   {slug}  {fixture.intervention}")
        print(f"     verdict v1 evaluated_at  {evaluated_at}")
        print(f"     execution  executed_at   {executed_at}")
        print(f"     re-authorize v{verdict['version']} -> {verdict['verdict']}")
        print(f"     cooldown check: {cooldown}")

        # The trail renders the anchor as an ISO timestamp. Compare on the value
        # rather than on prose, so this cannot pass on a coincidental substring.
        stamp_execution = executed_at.replace("+00:00", "").rstrip("Z")
        stamp_verdict = evaluated_at.replace("+00:00", "").rstrip("Z")
        check(
            f"{slug} cooldown quotes the EXECUTION timestamp",
            stamp_execution in cooldown,
            f"executed_at {stamp_execution} absent from: {cooldown}",
        )
        check(
            f"{slug} cooldown does NOT quote the verdict timestamp",
            stamp_verdict not in cooldown or stamp_verdict == stamp_execution,
            f"evaluated_at {stamp_verdict} is what the trail names",
        )
        check(
            f"{slug} is blocked while the cooldown runs",
            verdict["verdict"] == "blocked"
            and verdict["reason"] == "cooldown_active",
            f"{verdict['verdict']}/{verdict['reason']}",
        )

    # A non-contact execution must start no cooldown at all.
    if "IRETRY" in records:
        status, verdict = post(f"/authorize/{FIXTURES[2].event_id}")
        cooldown = detail_for(verdict["checks_performed"], "contact_cooldown")
        print(f"\n   IRETRY (immediate_retry, non-contact) re-authorize -> "
              f"{verdict['verdict']}/{verdict['reason']}")
        print(f"     cooldown check: {cooldown}")
        check(
            "a non-contact execution started no cooldown",
            verdict["verdict"] == "authorized",
            f"{verdict['verdict']}/{verdict['reason']}",
        )

    # -- 6 -------------------------------------------------------------------
    section("6. What the stored records do not say")
    status, listed = get(f"/executions?event_id={FIXTURES[0].event_id}")
    check("GET /executions?event_id= returns the record", status == 200 and listed)
    if listed:
        fields = set(listed[0])
        forbidden = {
            "money_recovered",
            "customer_paid",
            "amount_received",
            "outcome",
            "success",
            "paid",
            "recovered",
            "verified",
        }
        check(
            "no field claims the money came back",
            not (fields & forbidden),
            str(sorted(fields & forbidden)),
        )
        print(f"     fields present: {sorted(fields)}")

    status, latest = get("/executions")
    status2, history = get("/executions?history=true")
    check(
        "the default view is latest-per-event and history is longer or equal",
        status == 200 and status2 == 200 and len(history) >= len(latest),
        f"latest {len(latest)}, history {len(history)}",
    )
    status, filtered = get("/executions?action_type=contact_logged")
    check(
        "action_type filtering returns only contacts",
        status == 200
        and all(record["action_type"] == "contact_logged" for record in filtered),
        f"{status}, {len(filtered)} record(s)",
    )
    status, bad = get("/executions?status=recovered")
    check(
        "a status of 'recovered' is not a thing that exists",
        status == 422,
        f"HTTP {status}: {bad}",
    )

    await close_mongo_connection()

    print("\n" + "=" * 78)
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        "every executable intervention executed once against real Razorpay test mode, "
        "each exactly once,\nand the cooldown now measures from the send that actually "
        "happened"
    )


if __name__ == "__main__":
    asyncio.run(main())
