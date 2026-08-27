"""Stage 8 checkpoint 0 — generate the demo batch and forecast it. Writes nothing.

What this script establishes, before a single event is ingested:

1. **The composition is what was ratified.** Counts per surface, per root cause,
   per amount band, and the customer-repeat structure, all asserted rather than
   eyeballed.
2. **The Gemini routing split is real.** Every rule confidence in
   `app/diagnosis/rules.py` is at or above the 0.80 floor, so the ONLY route to
   the LLM is `classify()` returning None. This calls the real `classify()` on all
   200 events and asserts that exactly the 16 intended cases return None, and that
   the other 184 return the intended root cause. A reason string that drifted into
   a keyword rule fails here, at zero cost, instead of quietly using the rules path
   at ingestion.
3. **The downstream forecast comes from the real engines.** `decision.engine.decide`
   and `policy.engine.evaluate` are pure functions, so the predicted intervention
   and predicted verdict for all 184 rules cases are computed by the same code that
   will run for real — not by a table restated here. Restating the thresholds would
   only prove this script agrees with itself.

Nothing here connects to MongoDB, calls Razorpay, or calls Gemini. The final section
asserts that mechanically: `app.db._client` is still None on exit.

Usage:
    .venv/Scripts/python.exe scripts/s8_dryrun.py
"""

from __future__ import annotations

import socket
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median


class NetworkUsed(RuntimeError):
    """Raised if anything in this process tries to open a socket."""


def _refuse_connection(self, address):  # noqa: ANN001, ARG001
    raise NetworkUsed(
        f"this script opened a socket to {address!r} — it is supposed to be offline"
    )


# Installed BEFORE any app module is imported. Every outbound path this project has
# — Motor/pymongo to Atlas, httpx to Razorpay, httpx to Gemini — ends at
# socket.connect, so blocking it here is not a promise that the dry run touches
# nothing, it is a guarantee: any attempt raises instead of succeeding quietly.
socket.socket.connect = _refuse_connection  # type: ignore[method-assign]
socket.socket.connect_ex = _refuse_connection  # type: ignore[method-assign]

import app.db  # noqa: E402
from app.decision.engine import decide
from app.diagnosis import rules
from app.diagnosis.service import FALLBACK_CONFIDENCE, LLM_CONFIDENCE_CEILING
from app.models import (
    ALLOWED_ROOT_CAUSES,
    CONFIDENCE_FLOOR,
    NO_ACTION_INTERVENTIONS,
    Decision,
    DecisionRecord,
    Diagnosis,
    DiagnosisRecord,
    RevenueEvent,
    is_recoverable,
)
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    CONTACT_ACTION_TYPES,
    LINK_ACTION_TYPES,
)
from app.policy.engine import PolicyContext, evaluate as evaluate_policy
from app.policy.rules import current_fingerprint, current_rulebook

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

PASSED = 0
FAILED = 0

#: Confidence a plausible Gemini answer lands at, used only to forecast the 16
#: ambiguous cases. Any value at or above CONFIDENCE_FLOOR gives the same
#: intervention, so the exact number does not move the forecast — the fallback
#: case below is the one that behaves differently.
ASSUMED_LLM_CONFIDENCE = 0.75


def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


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


# ---------------------------------------------------------------------------
# Forecasting helpers. Both build the minimum object the real engine needs, so
# the engine does the work.
# ---------------------------------------------------------------------------


def fake_id(index: int) -> str:
    """A syntactically valid ObjectId string, so the models accept the link."""
    return f"{index:024x}"


def as_event(spec: dict) -> RevenueEvent:
    """The exact object ingestion would store, built from the spec."""
    return RevenueEvent(**ds.api_body(spec))


def forecast_decision(
    spec: dict, index: int, root_cause: str, confidence: float, method: str
) -> Decision:
    """Run the real decision engine against a hypothetical diagnosis."""
    event = as_event(spec)
    diagnosis = DiagnosisRecord(
        id=fake_id(index),
        version=1,
        method=method,
        event_id=spec["event_id"],
        surface=spec["surface"],
        root_cause=root_cause,
        confidence=confidence,
        evidence=["forecast only — no diagnosis has been produced"],
        recoverable=is_recoverable(root_cause),
    )
    return decide(diagnosis=diagnosis, event=event)


def forecast_verdict(spec: dict, index: int, decision: Decision):
    """Run the real policy engine against a hypothetical decision.

    Context reflects a brand-new event: no prior authorized contact, so neither the
    cap nor the cooldown can fire. That is the truth for every event in this batch
    at the moment Part B.4 authorizes it, which is why the forecast is exact rather
    than indicative.
    """
    record = DecisionRecord(id=fake_id(index), version=1, **decision.model_dump())
    context = PolicyContext(
        customer_ref=spec["customer_ref"],
        customer_opted_out=spec["_opted_out"],
        prior_authorized_contacts=0,
        last_authorized_contact_at=None,
        now=datetime.now(timezone.utc),
    )
    return evaluate_policy(decision=record, context=context)


def main() -> int:
    book = current_rulebook()
    specs = ds.generate()

    print(f"seed: {ds.SEED}   id prefix: {ds.PREFIX!r}   events: {len(specs)}")
    print(f"rulebook in force: {current_fingerprint()}")
    print(
        f"thresholds: auto < {money(book.auto_authorize_below)}   "
        f"never-auto >= {money(book.never_auto_at_or_above)}   "
        f"ERV floor {money(book.minimum_erv)}   "
        f"zero-cost exempt: {book.zero_cost_exempt_from_erv_floor}"
    )
    print("NOTHING IS WRITTEN BY THIS SCRIPT. No database, no Razorpay, no Gemini.")

    # =====================================================================
    heading("1. COMPOSITION")
    # =====================================================================
    by_surface = Counter(spec["surface"] for spec in specs)
    print(f"  {'surface':<14} {'n':>4} {'share':>7}")
    for surface in ("payment", "checkout", "subscription", "receivable"):
        n = by_surface[surface]
        print(f"  {surface:<14} {n:>4} {n / len(specs) * 100:>6.1f}%")
    check(
        "surface mix matches the ratified plan",
        dict(by_surface)
        == {"payment": 100, "checkout": 40, "subscription": 30, "receivable": 30},
        str(dict(by_surface)),
    )

    print("\n  root cause per surface (None = deliberately routed to Gemini)")
    for surface in ("payment", "checkout", "subscription", "receivable"):
        counts = Counter(
            spec["_intended_root_cause"]
            for spec in specs
            if spec["surface"] == surface
        )
        rendered = ", ".join(
            f"{cause or 'AMBIGUOUS->LLM'} {n}"
            for cause, n in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0] or "~")
            )
        )
        print(f"    {surface:<13} {rendered}")
        check(
            f"{surface} root-cause counts match the plan",
            dict(counts) == ds.COMPOSITION[surface],
            f"got {dict(counts)}",
        )

    print("\n  amount distribution")
    print(
        f"  {'surface':<14} {'band':<7} {'n':>4} {'min':>12} {'median':>12} "
        f"{'max':>12} {'sum':>14}"
    )
    total_at_risk = 0.0
    for surface in ("payment", "checkout", "subscription", "receivable"):
        for band in ("auto", "review"):
            amounts = [
                spec["amount"]
                for spec in specs
                if spec["surface"] == surface and spec["_band"] == band
            ]
            total_at_risk += sum(amounts)
            print(
                f"  {surface:<14} {band:<7} {len(amounts):>4} "
                f"{money(min(amounts)):>12} {money(median(amounts)):>12} "
                f"{money(max(amounts)):>12} {money(sum(amounts)):>14}"
            )
            expected = ds.BANDS[surface][band][0]
            check(
                f"{surface}/{band} count",
                len(amounts) == expected,
                f"{len(amounts)} vs planned {expected}",
            )
    print(f"  {'TOTAL':<14} {'':<7} {len(specs):>4} {'':>12} {'':>12} {'':>12} "
          f"{money(total_at_risk):>14}")

    check(
        "every auto-band amount is strictly below the autonomous limit",
        all(
            spec["amount"] < book.auto_authorize_below
            for spec in specs
            if spec["_band"] == "auto"
        ),
    )
    check(
        "every review-band amount is at or above the autonomous limit",
        all(
            spec["amount"] >= book.auto_authorize_below
            for spec in specs
            if spec["_band"] == "review"
        ),
    )
    round_hundreds = [s["event_id"] for s in specs if s["amount"] % 100 == 0]
    check(
        "no amount lands on a round hundred",
        not round_hundreds,
        f"{len(round_hundreds)} did: {round_hundreds[:5]}",
    )
    whole = sum(1 for s in specs if s["amount"] == int(s["amount"]))
    print(
        f"  whole-rupee amounts: {whole}/{len(specs)} "
        f"({whole / len(specs) * 100:.0f}%); the rest carry paise"
    )
    big_invoices = [
        s for s in specs if s["surface"] == "receivable" and s["amount"] >= 60_000
    ]
    check(
        "the receivable book has a genuine high-value tail",
        len(big_invoices) >= 3,
        f"{len(big_invoices)} invoices at or above 60,000",
    )
    never_auto = [s for s in specs if s["amount"] >= book.never_auto_at_or_above]
    print(
        f"  at or above the never-auto ceiling: {len(never_auto)} events, "
        f"{money(sum(s['amount'] for s in never_auto))} "
        f"({sum(s['amount'] for s in never_auto) / total_at_risk * 100:.1f}% of "
        "the batch's money)"
    )

    print("\n  customers")
    refs = Counter(spec["customer_ref"] for spec in specs)
    repeats = {ref: n for ref, n in refs.items() if n > 1}
    print(
        f"    distinct references: {len(refs)} across {len(specs)} events; "
        f"{len(repeats)} appear more than once "
        f"({sorted(Counter(repeats.values()).items())} as [events, customers])"
    )
    check(
        "188 distinct customer references, 10 of them repeating",
        len(refs) == 188 and len(repeats) == 10,
        f"{len(refs)} distinct, {len(repeats)} repeating",
    )
    crossed = [
        ref
        for ref, _n in repeats.items()
        if len({s["surface"] in ds.BUSINESS_SURFACES for s in specs
                if s["customer_ref"] == ref}) > 1
    ]
    check(
        "no reference is shared between a consumer surface and a receivable",
        not crossed,
        f"crossed: {crossed}",
    )

    oldest = min(spec["created_at"] for spec in specs)
    newest = max(spec["created_at"] for spec in specs)
    print(f"\n  created_at spans {oldest} .. {newest}")
    print(
        "    LIMITATION: every downstream record (diagnosis, decision, verdict, "
        "execution)\n    will be stamped when the pipeline actually runs, which is "
        "today. Only the\n    events are backdated."
    )

    # =====================================================================
    heading("2. THE GEMINI ROUTING GATE — real rules.classify() on all 200")
    # =====================================================================
    print(
        f"  RULE_CONFIDENCE_FLOOR = {rules.RULE_CONFIDENCE_FLOOR}. Every confidence "
        "in the rule\n  tables is at or above 0.82, so every rule match is "
        "confident and the ONLY\n  route to Gemini is classify() returning None."
    )
    lowest = 1.0
    llm_specs: list[dict] = []
    rules_specs: list[tuple[dict, rules.RuleMatch]] = []
    wrong_cause: list[str] = []
    unexpected_llm: list[str] = []
    unexpected_rules: list[str] = []

    for spec in specs:
        match = rules.classify(as_event(spec), prior_event_count=0)
        if spec["_expects_llm"]:
            if match is None:
                llm_specs.append(spec)
            else:
                unexpected_rules.append(
                    f"{spec['event_id']} -> {match.root_cause} @ {match.confidence}"
                )
        else:
            if match is None:
                unexpected_llm.append(
                    f"{spec['event_id']} ({spec['raw_failure_reason']!r})"
                )
            else:
                rules_specs.append((spec, match))
                lowest = min(lowest, match.confidence)
                if match.root_cause != spec["_intended_root_cause"]:
                    wrong_cause.append(
                        f"{spec['event_id']}: intended "
                        f"{spec['_intended_root_cause']}, rules say "
                        f"{match.root_cause} ({spec['raw_failure_reason']!r})"
                    )

    print(f"\n  rules path: {len(rules_specs)}    LLM path: {len(llm_specs)}")
    check(
        "exactly 16 events reach the LLM path",
        len(llm_specs) == 16,
        f"{len(llm_specs)} did",
    )
    check(
        "exactly 184 events resolve on the rules path",
        len(rules_specs) == 184,
        f"{len(rules_specs)} did",
    )
    check(
        "no intended-rules event silently fell through to the LLM",
        not unexpected_llm,
        "\n           ".join(unexpected_llm) if unexpected_llm else "",
    )
    check(
        "no intended-LLM event was caught by a rule",
        not unexpected_rules,
        "\n           ".join(unexpected_rules) if unexpected_rules else "",
    )
    check(
        "every rules match returns the intended root cause",
        not wrong_cause,
        "\n           ".join(wrong_cause) if wrong_cause else "",
    )
    check(
        "every rules match clears the confidence floor",
        lowest >= rules.RULE_CONFIDENCE_FLOOR,
        f"lowest confidence in the batch is {lowest}",
    )
    conf = Counter(round(match.confidence, 2) for _s, match in rules_specs)
    print(
        "  rule confidences present: "
        + ", ".join(f"{c}x{n}" for c, n in sorted(conf.items()))
    )

    print("\n  the 16 events routed to Gemini:")
    for spec in llm_specs:
        reason = spec["raw_failure_reason"]
        shown = "(no failure text supplied)" if reason is None else f"{reason!r}"
        print(f"    {spec['event_id']}  {spec['surface']:<12} "
              f"{money(spec['amount']):>12}")
        print(f"        {shown}")

    # =====================================================================
    heading("3. PREDICTED DECISIONS — real decision.engine.decide()")
    # =====================================================================
    predicted: dict[str, Decision] = {}
    for index, (spec, match) in enumerate(rules_specs, start=1):
        predicted[spec["event_id"]] = forecast_decision(
            spec, index, match.root_cause, match.confidence, "rules"
        )

    interventions = Counter(d.recommended_intervention for d in predicted.values())
    print(f"  {'intervention':<32} {'n':>4}  {'cost each':>10}  {'money at risk':>15}")
    for intervention, n in interventions.most_common():
        at_risk = sum(
            d.revenue_at_risk
            for d in predicted.values()
            if d.recommended_intervention == intervention
        )
        cost = next(
            d.estimated_cost
            for d in predicted.values()
            if d.recommended_intervention == intervention
        )
        print(f"  {intervention:<32} {n:>4}  {money(cost):>10}  {money(at_risk):>15}")

    actionable = sum(
        n for i, n in interventions.items() if i not in NO_ACTION_INTERVENTIONS
    )
    print(f"\n  actionable recommendations: {actionable} of {len(predicted)}")

    print(
        "\n  HOW EACH (surface, root cause) RESOLVES — every group in the batch.\n"
        "  One row per group because the matrix is deterministic: the same pair always\n"
        "  yields the same intervention, and only the amount varies within a row."
    )
    print(
        f"  {'surface':<13} {'root cause':<27} {'n':>3}  {'intervention':<29} "
        f"{'p':>5} {'money at risk':>14}"
    )
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for spec, match in rules_specs:
        groups[(spec["surface"], match.root_cause)].append(spec)
    for surface in ("payment", "checkout", "subscription", "receivable"):
        for (grp_surface, cause), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))
        ):
            if grp_surface != surface:
                continue
            sample = predicted[members[0]["event_id"]]
            distinct = {
                predicted[m["event_id"]].recommended_intervention for m in members
            }
            marker = "" if len(distinct) == 1 else f"  <- {sorted(distinct)}"
            print(
                f"  {surface:<13} {cause:<27} {len(members):>3}  "
                f"{sample.recommended_intervention:<29} "
                f"{sample.recovery_probability:>5.2f} "
                f"{money(sum(m['amount'] for m in members)):>14}{marker}"
            )
        print()
    check(
        "each (surface, root cause) group resolves to a single intervention",
        all(
            len({predicted[m["event_id"]].recommended_intervention for m in members})
            == 1
            for members in groups.values()
        ),
        f"{len(groups)} groups checked; a split would mean the ERV ordering flipped "
        "somewhere inside a group, which only the amount could cause",
    )
    non_recoverable = [
        spec
        for spec, match in rules_specs
        if not is_recoverable(match.root_cause)
    ]
    print(
        f"  non-recoverable by root cause: {len(non_recoverable)} events, "
        f"{money(sum(s['amount'] for s in non_recoverable))} at risk "
        f"({len(non_recoverable) / len(specs) * 100:.1f}% of the batch) — "
        f"{dict(Counter(s['_intended_root_cause'] for s in non_recoverable))}"
    )
    check(
        "the non-recoverable share lands in the ratified 5-8% band",
        5.0 <= len(non_recoverable) / len(specs) * 100 <= 8.0,
        f"{len(non_recoverable) / len(specs) * 100:.1f}%",
    )

    # =====================================================================
    heading("4. PREDICTED VERDICTS — real policy.engine.evaluate()")
    # =====================================================================
    verdicts = {}
    for index, (spec, _match) in enumerate(rules_specs, start=1):
        verdicts[spec["event_id"]] = forecast_verdict(
            spec, index, predicted[spec["event_id"]]
        )

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for event_id, verdict in verdicts.items():
        grouped[(verdict.verdict, verdict.reason)].append(event_id)

    print(f"  {'verdict':<26} {'reason':<28} {'n':>4}  {'money':>15}")
    amount_of = {spec["event_id"]: spec["amount"] for spec in specs}
    for (name, reason), ids in sorted(
        grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        print(
            f"  {name:<26} {reason:<28} {len(ids):>4}  "
            f"{money(sum(amount_of[i] for i in ids)):>15}"
        )

    # Worked examples per bucket. Widest and narrowest by amount plus one from the
    # middle, so the sample shows the edges of each bucket rather than three events
    # that happen to sit next to each other in the id order.
    print("\n  EXAMPLES FROM EACH BUCKET (smallest, median, largest by amount)")
    spec_by_id = {spec["event_id"]: spec for spec in specs}
    for (name, reason), ids in sorted(
        grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        ordered = sorted(ids, key=lambda i: amount_of[i])
        picks = (
            ordered
            if len(ordered) <= 3
            else [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
        )
        print(f"\n    {name} / {reason}   ({len(ids)} events)")
        for event_id in picks:
            spec = spec_by_id[event_id]
            decision = predicted[event_id]
            print(
                f"      {event_id}  {spec['surface']:<12} "
                f"{money(spec['amount']):>12}  {spec['_intended_root_cause']}"
            )
            print(
                f"        -> {decision.recommended_intervention:<30} "
                f"p={decision.recovery_probability:.2f}  "
                f"cost={money(decision.estimated_cost)}  "
                f"ERV={money(decision.expected_recovery_value)}"
            )

    authorized_ids = [
        event_id
        for event_id, verdict in verdicts.items()
        if verdict.verdict == "authorized"
    ]
    print(
        f"\n  authorized {len(authorized_ids)}   "
        f"requires_manual_review "
        f"{sum(1 for v in verdicts.values() if v.verdict == 'requires_manual_review')}"
        f"   blocked "
        f"{sum(1 for v in verdicts.values() if v.verdict == 'blocked')}"
    )

    optout_ids = {s["event_id"] for s in specs if s["_opted_out"]}
    optout_blocked = [
        event_id
        for event_id in optout_ids
        if verdicts.get(event_id)
        and verdicts[event_id].reason == "customer_opted_out"
    ]
    check(
        "all 3 opt-out demo events are blocked for customer_opted_out",
        len(optout_blocked) == 3,
        f"{len(optout_blocked)} of {len(optout_ids)}: {sorted(optout_blocked)}",
    )
    for event_id in sorted(optout_ids):
        spec = next(s for s in specs if s["event_id"] == event_id)
        decision = predicted.get(event_id)
        print(
            f"    {event_id}  {spec['surface']:<12} "
            f"{decision.recommended_intervention if decision else '?':<28} "
            f"{verdicts[event_id].reason}"
        )

    floor_blocked = [
        event_id
        for event_id, verdict in verdicts.items()
        if verdict.reason == "erv_below_minimum"
    ]
    print(
        f"\n  refused for erv_below_minimum: {len(floor_blocked)} — "
        f"{sorted(floor_blocked)}"
    )
    for event_id in sorted(floor_blocked):
        decision = predicted[event_id]
        print(
            f"    {event_id}  {money(decision.revenue_at_risk)} x "
            f"{decision.recovery_probability:.2f} - "
            f"{money(decision.estimated_cost)} = ERV "
            f"{money(decision.expected_recovery_value)}, floor "
            f"{money(book.minimum_erv)}"
        )
    forced = next(spec for spec in specs if spec.get("_note"))
    check(
        "the deliberately-placed sub-floor cart is the one refused",
        forced["event_id"] in floor_blocked,
        f"{forced['event_id']} at {money(forced['amount'])}; refused set is "
        f"{sorted(floor_blocked)}",
    )

    # =====================================================================
    heading("5. THE 16 GEMINI CASES — forecast range, not a prediction")
    # =====================================================================
    print(
        "  Which root cause Gemini returns is not ours to predict, so each case is\n"
        "  forecast across every root cause its surface allows. A fallback answer\n"
        f"  ({FALLBACK_CONFIDENCE}) sits below the {CONFIDENCE_FLOOR} decision "
        "confidence floor and\n  always yields no_action_low_confidence; a confident "
        f"answer (ceiling {LLM_CONFIDENCE_CEILING})\n  yields the interventions "
        "below."
    )
    llm_always = 0
    llm_never = 0
    llm_depends: list[str] = []
    for index, spec in enumerate(llm_specs, start=1000):
        outcomes: dict[str, tuple[str, str]] = {}
        for cause in sorted(ALLOWED_ROOT_CAUSES[spec["surface"]]):
            decision = forecast_decision(
                spec, index, cause, ASSUMED_LLM_CONFIDENCE, "llm"
            )
            verdict = forecast_verdict(spec, index, decision)
            outcomes[cause] = (decision.recommended_intervention, verdict.verdict)
        # Judge only against causes the matrix has an actual intervention for. A cause
        # that maps to no_action is blocked by policy for `no_action_recommended`
        # whatever the amount, so counting it would make every event "it depends" and
        # say nothing about the amount, which is the only thing under our control here.
        actionable_only = {
            cause: result
            for cause, result in outcomes.items()
            if result[0] not in NO_ACTION_INTERVENTIONS
        }
        authorized = [
            c for c, (_i, v) in actionable_only.items() if v == "authorized"
        ]
        if len(authorized) == len(actionable_only):
            llm_always += 1
        elif not authorized:
            llm_never += 1
        else:
            llm_depends.append(spec["event_id"])
        print(
            f"\n    {spec['event_id']}  {spec['surface']:<12} "
            f"{money(spec['amount']):>12}  band={spec['_band']}"
        )
        for cause, (intervention, verdict) in outcomes.items():
            print(f"        {cause:<28} -> {intervention:<30} {verdict}")

    print(
        f"\n  Of the 16, judged only on causes the matrix acts on, assuming Gemini "
        f"answers\n  above the {CONFIDENCE_FLOOR} confidence floor:"
    )
    print(
        f"    {llm_always:>2}  authorized whichever actionable cause comes back — "
        "the amount clears every gate"
    )
    print(
        f"    {llm_never:>2}  never authorized — review-band amounts, so a human "
        "decides regardless of cause"
    )
    print(
        f"    {len(llm_depends):>2}  depends on the cause, because the ERV floor "
        f"bites for the weaker ones: {llm_depends}"
    )
    print(
        "  Separately, a no-action cause (fraud, churn, dispute, mandate revoked, "
        "unknown)\n  is blocked for `no_action_recommended` at any amount, and a "
        f"fallback answer at\n  {FALLBACK_CONFIDENCE} is blocked for low confidence. "
        "So the upper bound on authorizations\n  from this group is "
        f"{llm_always + len(llm_depends)}, not 16."
    )

    # =====================================================================
    heading("6. EXECUTION SELECTION — Part B.5")
    # =====================================================================
    held = set(ds.held_back_ids(specs))
    print(
        f"  Held back from the blanket authorize: {len(held)} receivable events\n"
        f"  {sorted(held)}\n"
        "  Their first verdict is the one the promise-to-pay follow-up asks for.\n"
        "  Authorizing them now would start the 24h cooldown against a reservation\n"
        "  and make the `reevaluating` state unreachable for the whole batch."
    )

    spec_of = {spec["event_id"]: spec for spec in specs}
    eligible = [
        event_id
        for event_id in authorized_ids
        if event_id not in held
        and predicted[event_id].recommended_intervention in ACTION_FOR_INTERVENTION
    ]
    receivable_contacts = sorted(
        i for i in eligible if spec_of[i]["surface"] == "receivable"
    )
    others = [i for i in eligible if spec_of[i]["surface"] != "receivable"]

    # Systematic 1-in-2 within each (surface, root cause) group, by event id. Every
    # other event in a sorted list, not a random sample: the intervention mix stays
    # proportional and the selection can be re-derived by anyone reading this.
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for event_id in others:
        spec = spec_of[event_id]
        buckets[(spec["surface"], spec["_intended_root_cause"])].append(event_id)
    sampled: list[str] = []
    for key in sorted(buckets):
        group = sorted(buckets[key])
        sampled.extend(group[::2])

    to_execute = sorted(receivable_contacts + sampled)
    print(
        f"\n  authorized and executable: {len(eligible)}\n"
        f"    receivable contacts, all of them:            "
        f"{len(receivable_contacts)}\n"
        f"    link/retry events, 1-in-2 by (surface, cause): {len(sampled)} of "
        f"{len(others)}\n"
        f"    TOTAL TO EXECUTE IN PART B.5:                 {len(to_execute)}"
    )

    link_count = 0
    contact_count = 0
    print(f"\n  {'intervention':<32} {'action_type':<20} {'n':>4}  {'money':>15}")
    per_intervention = Counter(
        predicted[i].recommended_intervention for i in to_execute
    )
    for intervention, n in per_intervention.most_common():
        action = ACTION_FOR_INTERVENTION[intervention]
        if action in LINK_ACTION_TYPES:
            link_count += n
        elif action in CONTACT_ACTION_TYPES:
            contact_count += n
        at_risk = sum(
            spec_of[i]["amount"]
            for i in to_execute
            if predicted[i].recommended_intervention == intervention
        )
        print(f"  {intervention:<32} {action:<20} {n:>4}  {money(at_risk):>15}")

    print(
        f"\n  RAZORPAY TEST-MODE PAYMENT LINKS TO BE CREATED: {link_count}\n"
        "  (both retry variants create a real link too — retries are recorded under\n"
        "  action_type=retry_simulated but go through the same _execute_link path)\n"
        f"  contact records with no artifact: {contact_count}"
    )
    spend = sum(predicted[i].estimated_cost for i in to_execute)
    print(f"  modelled intervention spend across the batch: {money(spend)}")

    # =====================================================================
    heading("7. VERIFICATION FORECAST — probabilities from the matrix itself")
    # =====================================================================
    print(
        "  Each executed link's paid/unpaid outcome is drawn against the "
        "recovery_probability\n  the decision engine already stored for that event. "
        "No new rate is invented.\n"
    )
    link_ids = [
        i
        for i in to_execute
        if ACTION_FOR_INTERVENTION[predicted[i].recommended_intervention]
        in LINK_ACTION_TYPES
    ]
    by_prob: dict[float, list[str]] = defaultdict(list)
    for event_id in link_ids:
        by_prob[predicted[event_id].recovery_probability].append(event_id)
    print(f"  {'p(recovery)':>12} {'links':>6} {'money exposed':>15} "
          f"{'expected recovery':>18}")
    expected_recovered = 0.0
    for prob in sorted(by_prob, reverse=True):
        ids = by_prob[prob]
        exposed = sum(spec_of[i]["amount"] for i in ids)
        expected_recovered += exposed * prob
        print(
            f"  {prob:>12.2f} {len(ids):>6} {money(exposed):>15} "
            f"{money(exposed * prob):>18}"
        )
    exposed_total = sum(spec_of[i]["amount"] for i in link_ids)
    blended = expected_recovered / exposed_total * 100 if exposed_total else 0.0
    print(
        f"  {'BLENDED':>12} {len(link_ids):>6} {money(exposed_total):>15} "
        f"{money(expected_recovered):>18}   ({blended:.1f}% of money exposed)"
    )
    print(
        f"\n  expected paid links: about {sum(len(v) * p for p, v in by_prob.items()):.1f} "
        f"of {len(link_ids)}"
    )
    print(
        "  Honesty split, to be labelled per record in Part B.6:\n"
        "    - up to 3 driven through Razorpay's hosted test checkout for real "
        "(reported\n      either way if the iframe cannot be driven);\n"
        "    - the rest simulated by fetching the REAL link object and overriding "
        "exactly\n      two fields, status -> paid and amount_paid -> amount, then "
        "signing with the\n      real webhook secret (the Stage 6 Part A technique);\n"
        "    - 8 links genuinely cancelled through Razorpay's cancel API, so the "
        "entity\n      really is cancelled and only delivery is simulated;\n"
        "    - the remaining links get NO webhook. An outstanding link is the most\n"
        "      realistic outcome and requires no fabrication at all."
    )

    # =====================================================================
    heading("8. PROMISE-TO-PAY ROSTER — Part B.7")
    # =====================================================================
    grouped_roles = ds.roles(specs)
    now = datetime.now(timezone.utc)
    explain = {
        ds.ROLE_PTP_HONORED: (
            "link paid AFTER the promise is recorded, so check() finds the money and "
            "transitions to honored"
        ),
        ds.ROLE_PTP_SUPPRESSED: (
            "executed in B.5, so the 24h cooldown suppresses the follow-up; stays "
            "broken, which is the guardrail working"
        ),
        ds.ROLE_PTP_REEVALUATING: (
            "held back from B.4, so the follow-up's authorize is the first one and "
            "succeeds; moves to reevaluating"
        ),
        ds.ROLE_PTP_PROMISED: (
            "future date, so check() confirms the deadline branch without resolving "
            "it; stays promised"
        ),
    }
    total_promises = 0
    for role in (
        ds.ROLE_PTP_HONORED,
        ds.ROLE_PTP_SUPPRESSED,
        ds.ROLE_PTP_REEVALUATING,
        ds.ROLE_PTP_PROMISED,
    ):
        members = grouped_roles.get(role, [])
        total_promises += len(members)
        print(f"\n  {role}  ({len(members)})")
        print(f"    {explain[role]}")
        for spec in members:
            offset = spec["_promised_in_days"]
            when = (now + timedelta(days=offset)).date().isoformat()
            decision = predicted.get(spec["event_id"])
            print(
                f"      {spec['event_id']}  {spec['surface']:<12} "
                f"{money(spec['amount']):>12}  promised {when} "
                f"({offset:+d}d)  "
                f"{decision.recommended_intervention if decision else 'via LLM'}"
            )
    check(
        "11 promises planned",
        total_promises == 11,
        f"{total_promises} planned",
    )
    print(
        "\n  CORRECTION to the figure quoted when the plan was ratified. I said 12 new\n"
        "  promises with 4 reaching `reevaluating`. The 6 sub-5,000 receivables cannot\n"
        "  fund 4: one is spent on the opt-out demonstration and two on the suppressed\n"
        "  follow-ups, leaving 3. So it is 11 new promises and 3 reevaluating, not 12\n"
        "  and 4. The honor rate is unaffected — `reevaluating` is not a resolved state\n"
        "  and does not enter that denominator."
    )
    print(
        "\n  Portfolio after this batch, against the 9 promises already stored\n"
        "  (honored 3, broken 2, promised 1, reevaluating 3):\n"
        "    honored 7   broken 4   promised 3   reevaluating 6   total 20\n"
        "    honor_rate 7 / (7 + 4) = 63.64%, unchanged from today"
    )

    # =====================================================================
    heading("9. FORECAST DASHBOARD IMPACT")
    # =====================================================================
    existing_at_risk = 744_127.75
    existing_recovered = 7_500.00
    combined_at_risk = existing_at_risk + total_at_risk
    combined_recovered = existing_recovered + expected_recovered
    print(f"  {'':<34} {'existing 105':>16} {'new 200':>16} {'combined':>16}")
    print(
        f"  {'events':<34} {105:>16} {len(specs):>16} {105 + len(specs):>16}"
    )
    print(
        f"  {'revenue at risk':<34} {money(existing_at_risk):>16} "
        f"{money(total_at_risk):>16} {money(combined_at_risk):>16}"
    )
    print(
        f"  {'revenue recovered (expected)':<34} {money(existing_recovered):>16} "
        f"{money(expected_recovered):>16} {money(combined_recovered):>16}"
    )
    print(
        f"\n  HEADLINE recovery_rate            "
        f"{combined_recovered / combined_at_risk * 100:>5.2f}%  "
        f"(recovered / all money at risk)"
    )
    print(
        "  ^ FORECAST, NOT A MEASUREMENT. Its numerator is probability x amount over\n"
        "    the links this plan intends to create, plus the 7,500 already real. No\n"
        "    money in it has moved. This figure was quoted as '~2.4%' and it never\n"
        "    reproduced once the batch was actually executed: the measured headline\n"
        "    is 1.35% (29,605.14 of 2,187,218.02) across the full 305-event dataset,\n"
        "    and 2.4% is reachable on no cohort — fixture-only 1.01%, demo-only\n"
        "    1.53%. Ratified 2026-08-27; see docs/data-corrections.md. Report 1.35%."
    )
    print(
        f"  EXECUTED-COHORT recovery rate     {blended:>5.2f}%  "
        f"(recovered / money actually chased)"
    )
    print(
        "\n  These are two different denominators and are reported as two numbers.\n"
        "  The gap is the never-auto tier: "
        f"{money(sum(s['amount'] for s in never_auto))} of this batch is invoices\n"
        "  the agent deliberately refuses to chase without a human, so that money "
        "sits\n  in the headline denominator and never in its numerator."
    )

    # =====================================================================
    heading("10. THIS SCRIPT TOUCHED NOTHING")
    # =====================================================================
    check(
        "no MongoDB client was ever constructed",
        app.db._client is None,
        f"app.db._client is {app.db._client!r}",
    )
    forbidden = [
        name
        for name in sys.modules
        if name.startswith("app.") and name.endswith(("store", "gemini", "razorpay"))
    ]
    print(
        "  Modules that CAN do I/O and were pulled in by package init: "
        f"{sorted(forbidden)}"
    )
    print(
        "  Being imported is not the same as being called. The only app functions this\n"
        "  script invoked are rules.classify(), decision.engine.decide() and\n"
        "  policy.engine.evaluate() — all three pure."
    )
    check(
        "socket.connect was blocked for the whole run and never fired",
        socket.socket.connect is _refuse_connection,
        "installed before any app import; reaching this line means nothing tried to "
        "connect, since an attempt would have raised NetworkUsed",
    )

    heading(f"CHECKPOINT 0 — {PASSED} passed, {FAILED} failed")
    if FAILED:
        print("  Nothing proceeds until these pass.")
    else:
        print("  Generation logic is sound. Awaiting go-ahead for checkpoint 1.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
