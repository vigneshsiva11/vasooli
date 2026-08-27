"""Stage 8 Part B.3-B.4 — register opt-outs, decide 200, authorize 197.

Three phases, in an order that is not arbitrary:

1. **Opt-outs first.** Consent is checked at authorization time, so a do-not-contact
   record registered after the authorize would block nothing and prove nothing. The
   three opted-out customers go on the list before any verdict is asked for.
2. **Decide all 200.** The decision engine takes a diagnosis and an event and nothing
   else — it has no consent input and no database — so registering opt-outs cannot
   change a single recommendation. That the recommendations are identical either way
   is the architecture working, not a coincidence, and it is asserted below.
3. **Authorize 197.** The three `_hold_from_authorize` events are skipped so that a
   Part B.7 promise-to-pay follow-up has something left to authorize through the
   ordinary gate rather than around it.

Everything is checked against a prediction made **before** the live calls, in-process,
by the same pure functions the server runs: `app.decision.engine.decide` and
`app.policy.engine.evaluate`. Neither needs a database or a network, which is what
makes the prediction a property of production code rather than of a restated table.

The prediction here is exact rather than a range, unlike checkpoint 0's. Checkpoint 0
had to forecast the 16 Gemini cases; those diagnoses now exist, so this script reads
the real stored diagnosis for every event — real `_id`, real version, real root cause,
real confidence — and predicts from that. A live/predicted divergence is therefore a
genuine finding about the server, not slack in the forecast.

Usage:
    .venv/Scripts/python.exe scripts/s8_authorize.py --dry-run
    .venv/Scripts/python.exe scripts/s8_authorize.py
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.decision.engine import decide  # noqa: E402
from app.models import (  # noqa: E402
    NO_ACTION_INTERVENTIONS,
    DecisionRecord,
    DiagnosisRecord,
    RevenueEvent,
)
from app.policy.engine import PolicyContext, evaluate as evaluate_policy  # noqa: E402
from app.policy.rules import current_fingerprint  # noqa: E402

PASSED = 0
FAILED = 0


def heading(text: str) -> None:
    print()
    print("=" * 96)
    print(text)
    print("=" * 96)


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def money(value: float) -> str:
    return f"{value:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="predict everything, write nothing (no opt-out, no decide, no authorize)",
    )
    args = parser.parse_args()

    specs = {s["event_id"]: s for s in ds.generate()}
    spec_list = list(specs.values())
    optout_refs = ds.opted_out_refs(spec_list)
    held_back = set(ds.held_back_ids(spec_list))
    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=120.0)

    health = http.get("/").json()
    print(f"server {args.base}   database={health.get('database')}")
    print(f"rulebook in force: {current_fingerprint()}")
    print(f"events: {len(specs)}   opt-out customers: {len(optout_refs)}   "
          f"held back from authorize: {len(held_back)}")

    # =====================================================================
    heading("0. PRECONDITIONS — is this the first authorize for these events?")
    # =====================================================================
    # The prediction below assumes no prior authorized contact on any demo event,
    # because that is what makes the contact cap and cooldown unable to fire. If a
    # verdict already exists the assumption is false and the prediction is worthless,
    # so this is checked rather than asserted in a comment.
    existing_verdicts = [
        v for v in http.get("/policy-verdicts", params={"history": True}).json()
        if v["event_id"] in specs
    ]
    check(
        "no demo event has a policy verdict yet",
        not existing_verdicts,
        f"{len(existing_verdicts)} already exist: "
        f"{sorted({v['event_id'] for v in existing_verdicts})}"
        if existing_verdicts
        else "0 verdicts, so cap and cooldown start from zero for all 200",
    )

    # Cap and cooldown are counted per EVENT (`prior_authorized_contacts(event_id)`),
    # not per customer, so events sharing a customer do not interfere. Only opt-out is
    # per customer — so an opted-out customer with two events would block both, which
    # would be correct but would change the expected block count.
    ref_counts = Counter(s["customer_ref"] for s in specs.values())
    multi_event_optouts = {
        ref: ref_counts[ref] for ref in optout_refs if ref_counts[ref] > 1
    }
    check(
        "each opted-out customer owns exactly one demo event",
        not multi_event_optouts,
        f"{multi_event_optouts}" if multi_event_optouts else
        f"so exactly {len(optout_refs)} events can block on consent",
    )

    already_opted_out = {r["customer_ref"] for r in http.get("/opt-outs").json()}
    collisions = already_opted_out & set(ref_counts)
    check(
        "no demo customer is already on the do-not-contact list",
        not collisions,
        f"pre-existing: {sorted(collisions)}" if collisions else
        f"{len(already_opted_out)} unrelated opt-outs exist and are left alone",
    )

    # =====================================================================
    heading("1. PREDICTION — real decide() and evaluate(), in-process, no I/O")
    # =====================================================================
    diagnoses: dict[str, dict] = {}
    for eid in specs:
        versions = http.get("/diagnoses", params={"event_id": eid}).json()
        if versions:
            diagnoses[eid] = max(versions, key=lambda d: d["version"])
    check(
        "every event has a stored diagnosis to decide from",
        len(diagnoses) == len(specs),
        f"{len(diagnoses)} of {len(specs)}"
        + (f"; missing {sorted(set(specs) - set(diagnoses))}" if len(diagnoses) != len(specs) else ""),
    )
    if len(diagnoses) != len(specs):
        heading(f"ABORTED — {PASSED} passed, {FAILED} failed")
        http.close()
        return 1

    predicted_decision: dict[str, object] = {}
    predicted_verdict: dict[str, object] = {}
    frozen_now = datetime.now(timezone.utc)
    for index, (eid, spec) in enumerate(specs.items(), start=1):
        event = RevenueEvent(**ds.api_body(spec))
        record = DiagnosisRecord(**diagnoses[eid])
        decision = decide(diagnosis=record, event=event)
        predicted_decision[eid] = decision

        # A decision has no id until it is stored; the policy engine only reads the
        # id for the trail, so a syntactically valid stand-in is enough to predict a
        # verdict. The live check compares verdict and reason, not the id.
        as_record = DecisionRecord(id=f"{index:024x}", version=1, **decision.model_dump())
        predicted_verdict[eid] = evaluate_policy(
            decision=as_record,
            context=PolicyContext(
                customer_ref=spec["customer_ref"],
                customer_opted_out=spec["_opted_out"],
                prior_authorized_contacts=0,
                last_authorized_contact_at=None,
                now=frozen_now,
            ),
        )

    pred_iv = Counter(d.recommended_intervention for d in predicted_decision.values())
    print("\n  predicted interventions across all 200")
    for iv, n in pred_iv.most_common():
        print(f"    {iv:<32} {n:>3}")
    actionable = [
        eid for eid, d in predicted_decision.items()
        if d.recommended_intervention not in NO_ACTION_INTERVENTIONS
    ]
    print(f"\n  actionable: {len(actionable)}   no-action: {len(specs) - len(actionable)}")

    to_authorize = [eid for eid in specs if eid not in held_back]
    pred_v = Counter(
        (predicted_verdict[eid].verdict, predicted_verdict[eid].reason)
        for eid in to_authorize
    )
    print(f"\n  predicted verdicts for the {len(to_authorize)} to be authorized")
    for (verdict, reason), n in pred_v.most_common():
        print(f"    {verdict:<24} {reason:<28} {n:>3}")

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
        heading(f"PREDICTION ONLY — {PASSED} passed, {FAILED} failed")
        http.close()
        return 0 if FAILED == 0 else 1

    # =====================================================================
    heading("2. OPT-OUT REGISTRATION — POST /opt-out/{customer_ref}")
    # =====================================================================
    optout_errors: list[str] = []
    for ref in optout_refs:
        owner = next(s for s in specs.values() if s["customer_ref"] == ref)
        response = http.post(
            f"/opt-out/{ref}",
            json={"reason": "demo dataset: customer asked not to be contacted"},
        )
        ok = response.status_code in (200, 201)
        if not ok:
            optout_errors.append(f"{ref}: {response.status_code} {response.text[:160]}")
        print(f"  {response.status_code}  {ref:<28} (event {owner['event_id']}, "
              f"{owner['surface']}, {money(owner['amount'])})")
    check("every opt-out was registered", not optout_errors,
          "\n           ".join(optout_errors) if optout_errors else f"{len(optout_refs)} customers")

    listed = {r["customer_ref"] for r in http.get("/opt-outs").json()}
    check(
        "all three are readable back from GET /opt-outs",
        set(optout_refs) <= listed,
        f"missing {sorted(set(optout_refs) - listed)}"
        if not set(optout_refs) <= listed
        else f"list now holds {len(listed)} customers",
    )

    # =====================================================================
    heading("3. DECIDE — POST /decide/{event_id} for all 200")
    # =====================================================================
    live_decision: dict[str, dict] = {}
    decide_errors: list[str] = []
    started = time.monotonic()
    for index, eid in enumerate(specs, start=1):
        response = http.post(f"/decide/{eid}")
        if response.status_code != 201:
            decide_errors.append(f"{eid}: {response.status_code} {response.text[:160]}")
            continue
        live_decision[eid] = response.json()
        if index % 50 == 0 or index == len(specs):
            print(f"  {index}/{len(specs)} decided ({time.monotonic() - started:.1f}s)")
    check("every event was decided", not decide_errors,
          "\n           ".join(decide_errors) if decide_errors else f"{len(live_decision)} decisions")

    # Compare every economically meaningful field, not just the intervention. A right
    # action for the wrong reason is still a finding.
    fields = (
        "recommended_intervention",
        "estimated_cost",
        "recovery_probability",
        "revenue_at_risk",
        "expected_recovery_value",
    )
    divergences: list[str] = []
    for eid, live in live_decision.items():
        pred = predicted_decision[eid]
        for field in fields:
            want, got = getattr(pred, field), live[field]
            if isinstance(want, float) and round(want, 4) != round(got, 4):
                divergences.append(f"{eid}.{field}: predicted {want}, live {got}")
            elif not isinstance(want, float) and want != got:
                divergences.append(f"{eid}.{field}: predicted {want!r}, live {got!r}")
    check(
        f"every live decision matches the in-process prediction on all {len(fields)} fields",
        not divergences,
        "\n           ".join(divergences[:12]) + (f"\n           (+{len(divergences)-12} more)" if len(divergences) > 12 else "")
        if divergences
        else f"{len(live_decision)} decisions x {len(fields)} fields compared",
    )

    pinning = [
        f"{eid}: decision pins diagnosis {live['diagnosis_id']} v{live['diagnosis_version']}, "
        f"latest is {diagnoses[eid]['id']} v{diagnoses[eid]['version']}"
        for eid, live in live_decision.items()
        if live["diagnosis_id"] != diagnoses[eid]["id"]
        or live["diagnosis_version"] != diagnoses[eid]["version"]
    ]
    check(
        "every decision pins the current diagnosis, id and version",
        not pinning,
        "\n           ".join(pinning) if pinning else "no decision rests on a superseded diagnosis",
    )

    # The architectural claim, checked rather than asserted: opt-outs were registered
    # in phase 2, BEFORE these decisions were made. If consent had leaked into the
    # decision engine, the three opted-out events would recommend something different
    # from what the engine predicted while knowing nothing about consent.
    consent_leak = [
        f"{eid}: predicted {predicted_decision[eid].recommended_intervention}, "
        f"live {live_decision[eid]['recommended_intervention']}"
        for eid, s in specs.items()
        if s["_opted_out"] and eid in live_decision
        and predicted_decision[eid].recommended_intervention
        != live_decision[eid]["recommended_intervention"]
    ]
    check(
        "consent did not reach the decision engine — opted-out events recommend the same action",
        not consent_leak,
        "\n           ".join(consent_leak) if consent_leak else
        "3 opted-out events decided identically to the no-consent prediction; "
        "the block happens at the policy layer, not by suppressing the recommendation",
    )

    # =====================================================================
    heading("4. AUTHORIZE — POST /authorize/{event_id} for 197")
    # =====================================================================
    print(f"  holding back {len(held_back)}: {sorted(held_back)}")
    print("  (a Part B.7 promise-to-pay follow-up authorizes these through the same gate)")
    live_verdict: dict[str, dict] = {}
    authorize_errors: list[str] = []
    started = time.monotonic()
    for index, eid in enumerate(to_authorize, start=1):
        response = http.post(f"/authorize/{eid}")
        if response.status_code != 201:
            authorize_errors.append(f"{eid}: {response.status_code} {response.text[:160]}")
            continue
        live_verdict[eid] = response.json()
        if index % 50 == 0 or index == len(to_authorize):
            print(f"  {index}/{len(to_authorize)} authorized "
                  f"({time.monotonic() - started:.1f}s)")
    check("every one of the 197 got a verdict", not authorize_errors,
          "\n           ".join(authorize_errors) if authorize_errors else
          f"{len(live_verdict)} verdicts")
    check(
        "exactly 197 were attempted",
        len(to_authorize) == 197,
        f"{len(to_authorize)} = {len(specs)} - {len(held_back)}",
    )

    verdict_divergences = [
        f"{eid}: predicted {predicted_verdict[eid].verdict}/{predicted_verdict[eid].reason}, "
        f"live {live['verdict']}/{live['reason']}"
        for eid, live in live_verdict.items()
        if live["verdict"] != predicted_verdict[eid].verdict
        or live["reason"] != predicted_verdict[eid].reason
    ]
    check(
        "every live verdict matches the in-process prediction, verdict and reason",
        not verdict_divergences,
        "\n           ".join(verdict_divergences[:12]) if verdict_divergences
        else f"{len(live_verdict)} verdicts compared",
    )

    fingerprints = Counter(v["rulebook_fingerprint"] for v in live_verdict.values())
    sources = Counter(v["rulebook_fingerprint_source"] for v in live_verdict.values())
    check(
        "every verdict names the rulebook actually in force",
        set(fingerprints) == {current_fingerprint()},
        f"{dict(fingerprints)}",
    )
    check(
        "every verdict is labelled `evaluated`, not reconstructed or backfilled",
        set(sources) == {"evaluated"},
        f"{dict(sources)} — these ran live, so the fingerprint is evidence, not identification",
    )

    # The held-back three must have no verdict at all. A verdict here would mean the
    # blanket authorize leaked past its own exclusion list.
    leaked = [
        eid for eid in held_back
        if http.get("/policy-verdicts", params={"event_id": eid, "history": True}).json()
    ]
    check(
        "the three held-back events have no verdict",
        not leaked,
        f"leaked: {leaked}" if leaked else f"{sorted(held_back)} still unauthorized",
    )

    # =====================================================================
    heading("5. WHAT THE POLICY LAYER ACTUALLY DID")
    # =====================================================================
    pairs = Counter((v["verdict"], v["reason"]) for v in live_verdict.values())
    print(f"  {'verdict':<24} {'reason':<28} {'n':>4}  {'money':>14}")
    print("  " + "-" * 74)
    for (verdict, reason), n in pairs.most_common():
        amount = sum(
            specs[eid]["amount"] for eid, v in live_verdict.items()
            if (v["verdict"], v["reason"]) == (verdict, reason)
        )
        print(f"  {verdict:<24} {reason:<28} {n:>4}  {money(amount):>14}")
    total = sum(specs[eid]["amount"] for eid in live_verdict)
    print("  " + "-" * 74)
    print(f"  {'TOTAL':<24} {'':<28} {len(live_verdict):>4}  {money(total):>14}")

    authorized = {eid: v for eid, v in live_verdict.items() if v["verdict"] == "authorized"}
    auth_money = sum(specs[eid]["amount"] for eid in authorized)
    print(f"\n  authorized: {len(authorized)} events, {money(auth_money)} "
          f"({auth_money / total * 100:.1f}% of the authorized-batch money)")

    # Consent gating, visibly, on real demo data.
    print("\n  consent gating on the three opted-out events")
    for eid, spec in specs.items():
        if not spec["_opted_out"]:
            continue
        v = live_verdict.get(eid)
        if v is None:
            print(f"    {eid}: NO VERDICT")
            continue
        print(f"    {eid:<14} {spec['surface']:<12} {money(spec['amount']):>10}  "
              f"{live_decision[eid]['recommended_intervention']:<28} "
              f"-> {v['verdict']}/{v['reason']}")
        for line in v["checks_performed"]:
            if line.startswith("customer_opt_out"):
                print(f"       {line}")
    blocked_on_consent = [
        eid for eid, v in live_verdict.items() if v["reason"] == "customer_opted_out"
    ]
    check(
        "consent blocked exactly the three opted-out events, and nothing else",
        set(blocked_on_consent) == {eid for eid, s in specs.items() if s["_opted_out"]},
        f"blocked on consent: {sorted(blocked_on_consent)}",
    )

    heading(f"CHECKPOINT 3 — {PASSED} passed, {FAILED} failed")
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
