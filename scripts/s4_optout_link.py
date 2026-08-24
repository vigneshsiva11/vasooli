"""Stage 4 — opt-out end to end for both payment links.

Ratified: `payment_method_update_link` and `recovery_payment_link` are both
contact-type, on the principle that any link sent to a customer is outreach.
Before that, an opted-out customer could be sent either one, and neither consumed
a contact. This script proves both gaps are closed, that the reclassification is
what closed them, and that the widening did not overreach into gating everything.

Why every run needs a fresh customer: an opt-out is permanent by design — the
endpoint is idempotent and will not move `opted_out_at` — so a consent test can
never be idempotent on a fixed customer reference. Run 2 would find the customer
already opted out and the "authorized before consent was withdrawn" phase would
be untestable. Each run therefore derives its own customer and event ids from a
tag, printed below and overridable as argv[2] to reproduce a specific run's names.

Six phases:

1. Seed one PRE and one FRESH event for each link, plus a non-contact control.
   Seeding diagnoses and decides; it authorizes nothing.
2. Authorize both PRE events while consent still stands. Both permitted. Their
   trails are inspected for the *real* consent/cap/cooldown branches rather than
   "not applicable", which is what shows the reclassification is live.
3. The customer withdraws consent, once, for everything.
4. Re-authorize both PRE events. Blocked on consent — and the trails now show
   that phase 2's links consumed a contact and started the cooldown, which they
   would not have done before.
5. Authorize both FRESH events. No cap or cooldown history on those, so consent
   is the ONLY failed check and the block cannot be credited to anything else.
6. The non-contact control, and an A/B of each FRESH decision under the original
   contact set — identical inputs, differing only in the frozenset.

Run:  python scripts/s4_optout_link.py http://127.0.0.1:8123 [tag]
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo
from app.decision import latest_decision
from app.models import DecisionRecord
from app.models.policy import check_failed, check_name
from app.policy import (
    SUPERSEDED_RULEBOOKS,
    PolicyContext,
    current_rulebook,
    evaluate,
    rules,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
TAG = (
    sys.argv[2]
    if len(sys.argv) > 2
    else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
)

CUSTOMER = f"cust_pol_S4_link_{TAG}"

#: The contact set as the Stage 4 brief originally defined it, before either link
#: was ratified as contact-type. Phase 6 evaluates against this to attribute the
#: change in outcome to the reclassification and nothing else.
ORIGINAL_CONTACT_SET = frozenset(
    {"reminder", "escalating_reminder_sequence", "manual_escalation"}
)

#: The archived rulebook carrying that set. Looked up rather than indexed by
#: position, so this test names the parameter set it means and fails loudly if the
#: archive stops containing it, instead of silently A/B-ing the wrong two rulebooks.
LAUNCH_RULEBOOK = next(
    (
        rulebook
        for rulebook in SUPERSEDED_RULEBOOKS
        if rulebook.contact_interventions == ORIGINAL_CONTACT_SET
    ),
    None,
)


class Scenario(NamedTuple):
    """One link intervention and a failure that deterministically produces it."""

    #: Short label used in event ids.
    slug: str
    #: The intervention the matrix must recommend.
    link: str
    surface: str
    #: A failure code the rules path resolves without an LLM, so runs are
    #: reproducible and no quota is spent.
    failure_reason: str
    #: The root cause that code must resolve to.
    root_cause: str
    #: Both inside the autonomous tier so `amount_tier` cannot fire; both large
    #: enough that ERV clears the 25.00 floor by orders of magnitude.
    pre_amount: float
    fresh_amount: float

    def pre_event(self) -> str:
        return f"pol_S4_{TAG}_{self.slug}_PRE"

    def fresh_event(self) -> str:
        return f"pol_S4_{TAG}_{self.slug}_FRESH"


SCENARIOS = (
    Scenario(
        slug="PMUL",
        link="payment_method_update_link",
        surface="payment",
        failure_reason="card_expired",
        root_cause="card_expired",
        pre_amount=2_000.00,
        fresh_amount=2_200.00,
    ),
    Scenario(
        slug="RPL",
        link="recovery_payment_link",
        surface="checkout",
        failure_reason="payment_method_unavailable",
        root_cause="payment_method_unavailable",
        pre_amount=2_400.00,
        fresh_amount=2_600.00,
    ),
)

#: A non-contact intervention for the same opted-out customer. The original spec
#: required that an opt-out block contact-type interventions while still allowing
#: things like a retry through; after widening the contact set twice, that is worth
#: re-proving rather than assuming.
CONTROL = Scenario(
    slug="RETRY",
    link="immediate_retry",
    surface="payment",
    failure_reason="gateway_timeout",
    root_cause="temporary_processing_error",
    pre_amount=0.0,
    fresh_amount=1_800.00,
)

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        problems.append(f"{label} — {detail}" if detail else label)
    return ok


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


def detail_for(trail: list[str], name: str) -> str:
    for entry in trail:
        if check_name(entry) == name:
            return entry.split(": ", 1)[1].split("(", 1)[1].rsplit(")", 1)[0]
    return ""


def failures_in(trail: list[str]) -> set[str]:
    return {check_name(entry) for entry in trail if check_failed(entry)}


def seed(scenario: Scenario, event_id: str, amount: float) -> bool:
    """Ingest, diagnose and decide one failure. Authorizes nothing."""
    status, body = post(
        "/events",
        {
            "event_id": event_id,
            "surface": scenario.surface,
            "amount": amount,
            "currency": "INR",
            "customer_ref": CUSTOMER,
            "raw_failure_reason": scenario.failure_reason,
        },
    )
    if status >= 400:
        print(f"   FAILED ingest {event_id}: {status} {body}")
        return False

    status, diagnosis = post(f"/diagnose/{event_id}")
    if status >= 400:
        print(f"   FAILED diagnose {event_id}: {status} {diagnosis}")
        return False

    status, decision = post(f"/decide/{event_id}")
    if status >= 400:
        print(f"   FAILED decide {event_id}: {status} {decision}")
        return False

    print(
        f"   {event_id:<34} {amount:>8,.2f}  {diagnosis['root_cause']} "
        f"({diagnosis['confidence']:.2f} {diagnosis['method']}) -> "
        f"{decision['recommended_intervention']} "
        f"ERV {decision['expected_recovery_value']:,.2f}"
    )
    return check(
        f"{event_id} resolves to {scenario.root_cause} -> {scenario.link}",
        diagnosis["root_cause"] == scenario.root_cause
        and decision["recommended_intervention"] == scenario.link,
        f"got {diagnosis['root_cause']} -> {decision['recommended_intervention']}",
    )


def authorize(event_id: str) -> dict:
    status, verdict = post(f"/authorize/{event_id}")
    if status >= 400:
        print(f"   FAILED authorize {event_id}: {status} {verdict}")
        sys.exit(1)
    print(
        f"\n   {event_id}  ->  {verdict['verdict'].upper()}  "
        f"reason={verdict['reason']}  (verdict v{verdict['version']})"
    )
    print_trail(verdict["checks_performed"])
    return verdict


def run_http_phases() -> dict[str, dict]:
    print(f"Stage 4 — opt-out end to end for both payment links, against {BASE}")
    print(f"run tag {TAG}, customer {CUSTOMER}")
    print(f"contact-type set now: {sorted(rules.CONTACT_INTERVENTIONS)}")

    missing = [s.link for s in SCENARIOS if s.link not in rules.CONTACT_INTERVENTIONS]
    if missing:
        print(f"\nABORT: {missing} not in CONTACT_INTERVENTIONS; nothing to test.")
        sys.exit(1)
    if CONTROL.link in rules.CONTACT_INTERVENTIONS:
        print(f"\nABORT: {CONTROL.link} is contact-type, so it cannot be the control.")
        sys.exit(1)

    # -- 1 ------------------------------------------------------------------
    print("\n1. Seed the fixtures (diagnose and decide only — nothing authorized)")
    for scenario in SCENARIOS:
        if not seed(scenario, scenario.pre_event(), scenario.pre_amount):
            sys.exit(1)
        if not seed(scenario, scenario.fresh_event(), scenario.fresh_amount):
            sys.exit(1)
    if not seed(CONTROL, CONTROL.fresh_event(), CONTROL.fresh_amount):
        sys.exit(1)

    # -- 2 ------------------------------------------------------------------
    print("\n2. Authorize both PRE events while consent still stands")
    for scenario in SCENARIOS:
        verdict = authorize(scenario.pre_event())
        check(
            f"{scenario.link} is authorized before consent is withdrawn",
            verdict["verdict"] == "authorized" and verdict["reason"] == "ok",
            f"{verdict['verdict']}/{verdict['reason']}",
        )
        # Before the reclassification these three read "not applicable: <link>
        # does not contact the customer". Real details prove the new set is live.
        for name in ("customer_opt_out", "contact_cap", "contact_cooldown"):
            detail = detail_for(verdict["checks_performed"], name)
            check(
                f"{name} treats {scenario.link} as a real contact",
                "not applicable" not in detail,
                detail,
            )

    # -- 3 ------------------------------------------------------------------
    print("\n3. The customer withdraws consent, once, for everything")
    status, opt = post(f"/opt-out/{CUSTOMER}", {"reason": "stop sending me links"})
    print(f"   {status} created={opt.get('created')} at {opt.get('opted_out_at')}")
    check(
        "the opt-out was recorded",
        status == 201 and opt.get("created") is True,
        json.dumps(opt),
    )

    # -- 4 ------------------------------------------------------------------
    print("\n4. Re-authorize both PRE events now that consent is withdrawn")
    for scenario in SCENARIOS:
        verdict = authorize(scenario.pre_event())
        check(
            f"{scenario.link} is now blocked on consent",
            verdict["verdict"] == "blocked"
            and verdict["reason"] == "customer_opted_out",
            f"{verdict['verdict']}/{verdict['reason']}",
        )
        cap_detail = detail_for(verdict["checks_performed"], "contact_cap")
        check(
            f"phase 2's {scenario.link} consumed one of the three contacts",
            cap_detail.startswith("1 of 3"),
            cap_detail,
        )
        check(
            f"phase 2's {scenario.link} also started the cooldown",
            "contact_cooldown" in failures_in(verdict["checks_performed"]),
            detail_for(verdict["checks_performed"], "contact_cooldown"),
        )

    # -- 5 ------------------------------------------------------------------
    print("\n5. Authorize the FRESH events — no cap or cooldown history to blame")
    fresh: dict[str, dict] = {}
    for scenario in SCENARIOS:
        verdict = authorize(scenario.fresh_event())
        fresh[scenario.link] = verdict
        check(
            f"{scenario.link} blocked with customer_opted_out",
            verdict["verdict"] == "blocked"
            and verdict["reason"] == "customer_opted_out",
            f"{verdict['verdict']}/{verdict['reason']}",
        )
        check(
            "consent is the ONLY failed check, so nothing else can be credited",
            failures_in(verdict["checks_performed"]) == {"customer_opt_out"},
            f"failed: {sorted(failures_in(verdict['checks_performed']))}",
        )
        check(
            "the full trail is still recorded, not truncated at the failure",
            len(verdict["checks_performed"]) == 6,
            f"{len(verdict['checks_performed'])} entries",
        )
    return fresh


def run_control() -> None:
    print("\n6a. The non-contact control, for the same opted-out customer")
    verdict = authorize(CONTROL.fresh_event())
    check(
        f"{CONTROL.link} is still authorized despite the opt-out",
        verdict["verdict"] == "authorized" and verdict["reason"] == "ok",
        f"{verdict['verdict']}/{verdict['reason']}",
    )
    detail = detail_for(verdict["checks_performed"], "customer_opt_out")
    check(
        "consent does not apply to it — the widening did not gate everything",
        "not applicable" in detail,
        detail,
    )


async def old_versus_new(fresh: dict[str, dict]) -> None:
    """Re-evaluate each FRESH decision under the rulebook in force before the change.

    Identical decision, identical context, and the *only* difference is which
    rulebook the engine is handed — so any difference in verdict is attributable to
    the reclassification and nothing else. Nothing here is persisted.

    The old set is applied by passing the archived rulebook as an argument. It used
    to be applied by reassigning `rules.CONTACT_INTERVENTIONS` for the duration of
    one call, which only worked by accident: patching a module global reaches
    whichever readers happen to look it up late, and any parameter the engine had
    bound at import would have stayed at today's value while the classification
    moved. An A/B where one side is half-applied proves nothing.
    """
    print("\n6b. The same decisions under the original contact set (not stored)")

    if LAUNCH_RULEBOOK is None:
        print(
            "   ABORT: no archived rulebook carries the original contact set "
            f"{sorted(ORIGINAL_CONTACT_SET)}; app/policy/rulebook.py no longer "
            "records the policy this comparison is against."
        )
        sys.exit(1)

    await connect_to_mongo()
    decisions = {
        scenario.link: DecisionRecord.from_document(
            await latest_decision(scenario.fresh_event())
        )
        for scenario in SCENARIOS
    }
    await close_mongo_connection()

    live = current_rulebook()
    print(f"   original: {LAUNCH_RULEBOOK.fingerprint}  "
          f"{sorted(LAUNCH_RULEBOOK.contact_interventions)}")
    print(f"   ratified: {live.fingerprint}  "
          f"{sorted(live.contact_interventions)}")
    print(
        f"   the two rulebooks differ in: "
        f"{sorted(live.differences_from(LAUNCH_RULEBOOK))}"
    )

    for scenario in SCENARIOS:
        decision = decisions[scenario.link]
        stored = fresh[scenario.link]
        context = PolicyContext(
            customer_ref=CUSTOMER,
            customer_opted_out=True,
            prior_authorized_contacts=0,
            last_authorized_contact_at=None,
            # The stored verdict's own clock, so the two evaluations differ in the
            # classification and in nothing else at all.
            now=datetime.fromisoformat(stored["evaluated_at"].replace("Z", "+00:00")),
        )

        current = evaluate(decision=decision, context=context, rulebook=live)
        before = evaluate(decision=decision, context=context, rulebook=LAUNCH_RULEBOOK)

        print(f"\n   {scenario.link}")
        print(f"     original -> {before.verdict.upper():<10} reason={before.reason}")
        print(f"     ratified -> {current.verdict.upper():<10} reason={current.reason}")

        check(
            f"under the original set an opted-out customer got {scenario.link} anyway",
            before.verdict == "authorized" and before.reason == "ok",
            f"{before.verdict}/{before.reason}",
        )
        check(
            "under the ratified set the same inputs are blocked",
            current.verdict == "blocked"
            and current.reason == "customer_opted_out",
            f"{current.verdict}/{current.reason}",
        )
        check(
            "the stored verdict matches the in-process re-derivation",
            current.verdict == stored["verdict"] and current.reason == stored["reason"],
            f"stored {stored['verdict']}/{stored['reason']} vs "
            f"{current.verdict}/{current.reason}",
        )
        # Each verdict names the rulebook that produced it, so the A/B is visible in
        # the records themselves and not only in this script's prose.
        check(
            "each side stamps its own rulebook fingerprint",
            before.rulebook_fingerprint == LAUNCH_RULEBOOK.fingerprint
            and current.rulebook_fingerprint == live.fingerprint
            and stored["rulebook_fingerprint"] == live.fingerprint,
            f"before {before.rulebook_fingerprint}, now "
            f"{current.rulebook_fingerprint}, stored "
            f"{stored.get('rulebook_fingerprint')}",
        )


if __name__ == "__main__":
    result = run_http_phases()
    run_control()
    asyncio.run(old_versus_new(result))

    print("\n" + "=" * 78)
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        "both payment links are consent-gated and consume a contact; an opted-out "
        "customer is blocked, not messaged, while a retry still proceeds"
    )
