"""Stage 3 adversarial verification — try to break the recommend/authorize boundary.

Two kinds of attack, matching the Stage 2 discipline:

1. Construct a `Decision` directly, bypassing the engine entirely, and try to
   make it carry a lie (a flattering ERV, a spend on a no-action) or an
   execution-shaped field (`authorized`, `executed`, a payment-link id).
2. Reach the store with a decision whose `diagnosis_id` points at nothing, at
   another event's diagnosis, or at the wrong version.

Plus structural checks: that the decision package imports no policy, execution,
or Razorpay module, and that the matrix's import-time validation actually fires
when the table is made inconsistent.

Run:  python scripts/s3_adversarial.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pydantic import ValidationError

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision import DanglingDiagnosisReference, append, decide
from app.models import (
    ALLOWED_INTERVENTIONS,
    CONFIDENCE_FLOOR,
    Decision,
    DiagnosisRecord,
    RevenueEvent,
)

passes = 0
failures: list[str] = []


def expect_rejected(label: str, payload: dict) -> None:
    """A Decision built from `payload` must not be constructable."""
    global passes
    try:
        decision = Decision(**payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "(model)"
        message = first["msg"].replace("Value error, ", "")
        print(f"  BLOCKED  {label}")
        print(f"           {location}: {message[:150]}")
        passes += 1
    else:
        failures.append(label)
        print(f"  ACCEPTED {label}  <-- BOUNDARY HOLE")
        print(f"           {decision.model_dump()}")


def expect_accepted(label: str, payload: dict) -> None:
    """A well-formed Decision must still be constructable (no over-blocking)."""
    global passes
    try:
        Decision(**payload)
    except ValidationError as exc:
        failures.append(f"{label} (wrongly rejected)")
        print(f"  REJECTED {label}  <-- OVER-BLOCKING: {exc.errors()[0]['msg']}")
    else:
        print(f"  ALLOWED  {label}  (control: a valid decision is still buildable)")
        passes += 1


# A genuine, valid decision. Every attack below is this with one thing changed,
# so nothing passes or fails for an incidental reason.
VALID = {
    "event_id": "dec_S3_TEMP",
    "diagnosis_id": "0" * 24,
    "diagnosis_version": 1,
    "recommended_intervention": "immediate_retry",
    "estimated_cost": 0.0,
    "recovery_probability": 0.65,
    "revenue_at_risk": 6500.0,
    "expected_recovery_value": 4225.0,
    "reasoning": "control case",
}


def attack(**changes) -> dict:
    """Return the valid payload with fields replaced or added."""
    payload = deepcopy(VALID)
    payload.update(changes)
    return payload


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def model_level_attacks() -> None:
    section("1. Control")
    expect_accepted("unmodified valid decision", attack())

    section("2. Lying about the arithmetic")
    expect_rejected(
        "ERV inflated to 999999 while cost/probability stay honest",
        attack(expected_recovery_value=999999.0),
    )
    expect_rejected(
        "negative ERV disguised as positive (-12.50 stored as 12.50)",
        attack(
            event_id="dec_S3_DUNNING",
            recommended_intervention="manual_escalation",
            estimated_cost=50.0,
            recovery_probability=0.25,
            revenue_at_risk=150.0,
            expected_recovery_value=12.50,
        ),
    )
    expect_rejected(
        "ERV nudged by 1.00 (just outside the paise tolerance)",
        attack(expected_recovery_value=4226.0),
    )
    expect_accepted(
        "ERV off by 0.005 (inside the rounding tolerance)",
        attack(expected_recovery_value=4225.005),
    )

    section("3. Making a no-action spend money")
    for name in ("no_action", "no_action_low_confidence", "no_action_negative_erv"):
        expect_rejected(
            f"{name} carrying a 50.00 cost",
            attack(
                recommended_intervention=name,
                estimated_cost=50.0,
                recovery_probability=0.0,
                expected_recovery_value=-50.0,
            ),
        )
    expect_rejected(
        "no_action_low_confidence claiming a 0.65 success chance",
        attack(
            recommended_intervention="no_action_low_confidence",
            estimated_cost=0.0,
            recovery_probability=0.65,
            expected_recovery_value=4225.0,
        ),
    )

    section("4. Recommending an action that cannot work")
    expect_rejected(
        "immediate_retry with recovery_probability 0.0",
        attack(recovery_probability=0.0, expected_recovery_value=0.0),
    )
    expect_rejected(
        "manual_escalation at p=0 and a 50.00 spend",
        attack(
            recommended_intervention="manual_escalation",
            estimated_cost=50.0,
            recovery_probability=0.0,
            expected_recovery_value=-50.0,
        ),
    )

    section("5. Smuggling execution-shaped fields (extra='forbid')")
    for field, value in (
        ("authorized", True),
        ("approved", True),
        ("executed", True),
        ("status", "executed"),
        ("razorpay_payment_link_id", "plink_TESTFAKE123"),
        ("recipient_email", "victim@example.com"),
        ("execute_now", True),
        ("amount_to_charge", 6500.0),
    ):
        expect_rejected(f"extra field {field}={value!r}", attack(**{field: value}))

    section("6. Inventing an intervention outside the catalogue")
    for invented in (
        "full_refund_and_apology",
        "charge_customer_directly",
        "immediate_retry_twice",
        "IMMEDIATE_RETRY",
        "",
    ):
        expect_rejected(
            f"recommended_intervention={invented!r}",
            attack(recommended_intervention=invented),
        )

    section("7. Out-of-range and malformed values")
    expect_rejected("negative estimated_cost", attack(estimated_cost=-5.0,
                                                     expected_recovery_value=4230.0))
    expect_rejected("recovery_probability 1.5", attack(recovery_probability=1.5,
                                                      expected_recovery_value=9750.0))
    expect_rejected("negative revenue_at_risk", attack(revenue_at_risk=-6500.0,
                                                      expected_recovery_value=-4225.0))
    expect_rejected("diagnosis_version 0", attack(diagnosis_version=0))
    expect_rejected("empty reasoning", attack(reasoning=""))
    expect_rejected("diagnosis_id 'not-an-objectid'", attack(diagnosis_id="not-an-objectid"))
    expect_rejected("diagnosis_id too short", attack(diagnosis_id="abc123"))
    expect_rejected("diagnosis_id with a non-hex character", attack(diagnosis_id="z" * 24))
    expect_rejected("diagnosis_id absent entirely", {
        key: value for key, value in VALID.items() if key != "diagnosis_id"
    })


def field_surface_check() -> None:
    section("8. The model has no vocabulary for authorization or execution")
    fields = set(Decision.model_fields)
    print(f"  Decision fields ({len(fields)}): {', '.join(sorted(fields))}")

    forbidden = {
        "authorized", "authorization", "approved", "approval", "executed",
        "execution", "status", "state", "payment_link", "payment_link_id",
        "razorpay_id", "recipient", "sent_at", "attempted_at", "outcome",
        "result", "charged",
    }
    present = fields & forbidden
    if present:
        failures.append(f"Decision exposes execution-shaped fields: {sorted(present)}")
        print(f"  FAIL: execution-shaped fields present: {sorted(present)}")
    else:
        print("  OK: no authorization/execution/outcome field exists to be set")
        globals()["passes"] += 1


def import_boundary_check() -> None:
    section("9. The decision package cannot reach policy, execution, or Razorpay")
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:app\.(?:policy|execution|verification)|razorpay|"
        r"requests|httpx)\b",
        re.MULTILINE,
    )
    clean = True
    for path in sorted((ROOT / "app" / "decision").glob("*.py")):
        hits = forbidden.findall(path.read_text(encoding="utf-8"))
        marker = "OK " if not hits else "FAIL"
        print(f"  {marker} {path.relative_to(ROOT)}: {hits or 'no policy/execution/HTTP imports'}")
        if hits:
            clean = False
            failures.append(f"{path.name} imports {hits}")

    engine = (ROOT / "app" / "decision" / "engine.py").read_text(encoding="utf-8")
    for banned in ("get_database", "AsyncIOMotor", "generate_content", "gemini"):
        if banned in engine:
            clean = False
            failures.append(f"engine.py references {banned}")
            print(f"  FAIL engine.py references {banned}")
    if clean:
        print("  OK: engine.py holds no database handle, no LLM call, no HTTP client")
        globals()["passes"] += 1


def matrix_validation_check() -> None:
    section("10. The matrix's import-time validation actually fires")
    from app.decision import matrix as matrix_module

    original = matrix_module.INTERVENTION_MATRIX
    cases: list[tuple[str, dict]] = []

    # Drop a mapped pair: the completeness check must notice the hole.
    without_pair = dict(original)
    without_pair.pop(("payment", "card_expired"))
    cases.append(("a valid (surface, root_cause) pair left unmapped", without_pair))

    # Map a non-recoverable cause to a real, spending intervention.
    fraud_acts = dict(original)
    fraud_acts[("payment", "suspected_fraud")] = (
        matrix_module.Candidate(
            intervention="manual_escalation",
            recovery_probability=0.9,
            assumption="chase a suspected-fraud case anyway",
        ),
    )
    cases.append(("a non-recoverable cause mapped to a real intervention", fraud_acts))

    # Probability outside [0, 1].
    bad_probability = dict(original)
    bad_probability[("payment", "card_expired")] = (
        matrix_module.Candidate(
            intervention="payment_method_update_link",
            recovery_probability=1.4,
            assumption="impossible certainty",
        ),
    )
    cases.append(("a recovery_probability of 1.4", bad_probability))

    # An intervention name that is not in the catalogue.
    invented = dict(original)
    invented[("payment", "card_expired")] = (
        matrix_module.Candidate(
            intervention="wire_transfer_request",
            recovery_probability=0.5,
            assumption="an action nobody approved",
        ),
    )
    cases.append(("an intervention outside the catalogue", invented))

    for label, table in cases:
        matrix_module.INTERVENTION_MATRIX = table
        try:
            matrix_module._validate_matrix()
        except RuntimeError as exc:
            print(f"  BLOCKED  {label}")
            print(f"           {str(exc)[:170]}")
            globals()["passes"] += 1
        else:
            failures.append(f"matrix validation missed: {label}")
            print(f"  PASSED   {label}  <-- VALIDATION HOLE")
        finally:
            matrix_module.INTERVENTION_MATRIX = original

    matrix_module._validate_matrix()
    print("  (restored table re-validates cleanly)")


def gate_precedence_check() -> None:
    section("11. Gates cannot be outbid by a large amount")

    def diagnosis(**changes) -> DiagnosisRecord:
        base = {
            "id": "1" * 24,
            "version": 1,
            "event_id": "gate_probe",
            "surface": "receivable",
            "root_cause": "non_responsive",
            "confidence": 0.9,
            "recoverable": True,
            "evidence": ["synthetic gate probe"],
            "method": "rules",
        }
        base.update(changes)
        return DiagnosisRecord(**base)

    event = RevenueEvent(
        event_id="gate_probe",
        surface="receivable",
        amount=10_000_000.0,
        currency="INR",
        customer_ref="cust_gate_probe",
    )

    # A ten-million-rupee receivable: manual_escalation would score ~5.5 million.
    baseline = decide(diagnosis=diagnosis(), event=event)
    print(f"  ungated  10,000,000 at conf 0.90 -> {baseline.recommended_intervention} "
          f"(ERV {baseline.expected_recovery_value:,.2f})")

    low = decide(diagnosis=diagnosis(confidence=0.49), event=event)
    ok = low.recommended_intervention == "no_action_low_confidence"
    print(f"  {'OK  ' if ok else 'FAIL'} same amount at conf 0.49 (floor {CONFIDENCE_FLOOR}) "
          f"-> {low.recommended_intervention} (ERV {low.expected_recovery_value:,.2f})")
    if ok:
        globals()["passes"] += 1
    else:
        failures.append("confidence floor was outbid by a large amount")

    blocked = decide(
        diagnosis=diagnosis(recoverable=False, confidence=0.99), event=event
    )
    ok = blocked.recommended_intervention == "no_action"
    print(f"  {'OK  ' if ok else 'FAIL'} same amount, recoverable=False at conf 0.99 "
          f"-> {blocked.recommended_intervention} (ERV {blocked.expected_recovery_value:,.2f})")
    if ok:
        globals()["passes"] += 1
    else:
        failures.append("recoverable=False was outbid by a large amount")

    # recoverable=False must win even when confidence is also low: the first gate
    # is the one that should be reported, so the reason stays accurate.
    both = decide(diagnosis=diagnosis(recoverable=False, confidence=0.1), event=event)
    ok = both.recommended_intervention == "no_action"
    print(f"  {'OK  ' if ok else 'FAIL'} recoverable=False AND conf 0.10 "
          f"-> {both.recommended_intervention} (hard block reported, not the floor)")
    if ok:
        globals()["passes"] += 1
    else:
        failures.append("gate precedence wrong when both gates apply")

    # Mismatched diagnosis/event must not be decidable at all.
    other = RevenueEvent(
        event_id="some_other_event",
        surface="receivable",
        amount=500.0,
        currency="INR",
        customer_ref="cust_other",
    )
    try:
        decide(diagnosis=diagnosis(), event=other)
    except ValueError as exc:
        print(f"  BLOCKED deciding a diagnosis against a different event's amount")
        print(f"          {str(exc)[:150]}")
        globals()["passes"] += 1
    else:
        failures.append("decide() accepted a mismatched diagnosis/event pair")
        print("  ACCEPTED mismatched diagnosis/event  <-- BOUNDARY HOLE")


async def referential_attacks() -> None:
    section("12. Reaching the store with a diagnosis reference that does not hold")
    await connect_to_mongo()
    database = get_database()

    real = await database["diagnoses"].find_one({"event_id": "dec_S3_TEMP"})
    other = await database["diagnoses"].find_one({"event_id": "pay_S2_EXP"})
    print(f"  real diagnosis for dec_S3_TEMP: {real['_id']} v{real['version']}")
    print(f"  real diagnosis for pay_S2_EXP:  {other['_id']} v{other['version']}")

    before = await database["decisions"].count_documents({})

    async def expect_dangling(label: str, decision: Decision) -> None:
        try:
            await append(decision)
        except DanglingDiagnosisReference as exc:
            print(f"  BLOCKED  {label}")
            print(f"           {str(exc)[:160]}")
            globals()["passes"] += 1
        else:
            failures.append(label)
            print(f"  STORED   {label}  <-- BOUNDARY HOLE")

    await expect_dangling(
        "well-formed but nonexistent diagnosis_id (all zeroes)",
        Decision(**attack(diagnosis_id="0" * 24)),
    )
    await expect_dangling(
        "diagnosis_id belonging to a different event (pay_S2_EXP's diagnosis)",
        Decision(**attack(diagnosis_id=str(other["_id"]))),
    )
    await expect_dangling(
        "correct diagnosis_id, wrong diagnosis_version (claims v9)",
        Decision(**attack(diagnosis_id=str(real["_id"]), diagnosis_version=9)),
    )

    after = await database["decisions"].count_documents({})
    if after == before:
        print(f"  OK: decisions collection unchanged ({before} before, {after} after)")
        globals()["passes"] += 1
    else:
        failures.append(f"decision count changed: {before} -> {after}")
        print(f"  FAIL: decision count changed {before} -> {after}")

    await close_mongo_connection()


def main() -> None:
    print("Stage 3 adversarial verification")
    print(f"catalogue size: {len(ALLOWED_INTERVENTIONS)}   confidence floor: {CONFIDENCE_FLOOR}")

    model_level_attacks()
    field_surface_check()
    import_boundary_check()
    matrix_validation_check()
    gate_precedence_check()
    asyncio.run(referential_attacks())

    print("\n" + "=" * 78)
    print(f"checks passed: {passes}")
    if failures:
        print(f"BOUNDARY HOLES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("no boundary holes found")


if __name__ == "__main__":
    main()
