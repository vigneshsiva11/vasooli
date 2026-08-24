"""Stage 4 adversarial tests — try to make the policy layer misbehave.

Same posture as `scripts/s3_adversarial.py`: every case here is an *attack*, and
passing means the attack was refused. Five fronts:

1. **Execution smuggling.** A `PolicyVerdict` must have no field capable of
   saying anything happened, and must refuse extras that try to add one.
2. **Contradiction.** A verdict must not be able to disagree with its own reason,
   its own trail, or the precedence order — including the user's example,
   `verdict="authorized"` with `reason="customer_opted_out"`.
3. **Trail tampering.** A partial, padded, reordered, or unknown-check trail must
   be unconstructable, so a short-circuited evaluation cannot be represented.
4. **Boundary imports.** No module under `app/policy/` may import Razorpay, an
   HTTP client, or an LLM client — checked by reading the source, not by trusting
   the docstrings.
5. **Referential attacks.** Verdicts pointing at a missing, foreign, misversioned,
   or superseded decision must be refused at the write boundary.

Run:  python scripts/s4_adversarial.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pydantic import ValidationError

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision import latest_decision
from app.models import DecisionRecord
from app.models.policy import (
    POLICY_CHECKS,
    PolicyVerdict,
    format_check,
)
from app.policy import (
    DanglingDecisionReference,
    PolicyContext,
    StaleDecisionReference,
    append as append_verdict,
    evaluate,
    rules,
)

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def refused(name: str, build) -> None:
    """Assert that constructing something invalid raises."""
    try:
        result = build()
    except (ValidationError, ValueError, TypeError, RuntimeError) as exc:
        first = str(exc).strip().splitlines()
        message = next(
            (line.strip() for line in first if "Value error" in line or "Extra" in line),
            first[0] if first else "",
        )
        check(name, True)
        print(f"        refused: {message[:150]}")
    else:
        check(name, False, f"accepted it and returned {result!r}"[:200])


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------
# A valid verdict to mutate. Everything below is a perturbation of this.
# ---------------------------------------------------------------------------

VALID_TRAIL = [
    format_check("decision_is_actionable", True, "reminder is a real intervention"),
    format_check("customer_opt_out", True, "customer cust_x has not opted out"),
    format_check("contact_cap", True, "0 of 3 contacts used for event evt_x"),
    format_check("contact_cooldown", True, "no prior authorized contact"),
    format_check("erv_minimum", True, "ERV 500.00 clears the 25.00 minimum"),
    format_check("amount_tier", True, "1,000.00 is below the 5,000.00 limit"),
]

BASE = {
    "event_id": "evt_x",
    "decision_id": "a" * 24,
    "decision_version": 1,
    "verdict": "authorized",
    "reason": "ok",
    "checks_performed": list(VALID_TRAIL),
}


def fail_at(index: int, name: str, detail: str) -> list[str]:
    """A trail where one named check FAILs instead of passing."""
    trail = list(VALID_TRAIL)
    trail[index] = format_check(name, False, detail)
    return trail


def test_baseline() -> None:
    section("0. Baseline: the honest verdict must still be constructable")
    verdict = PolicyVerdict(**BASE)
    check(
        "a clean authorized verdict validates",
        verdict.verdict == "authorized" and verdict.reason == "ok",
    )
    check(
        "the trail carries every declared check",
        len(verdict.checks_performed) == len(POLICY_CHECKS),
        f"{len(verdict.checks_performed)} entries",
    )


def test_execution_smuggling() -> None:
    section("1. Execution smuggling — a permission record must not carry outcomes")

    for field, value in [
        ("razorpay_payment_link_id", "plink_00000000000000"),
        ("executed", True),
        ("amount_charged", 1000.0),
        ("razorpay_order_id", "order_000000000000"),
        ("notification_sent", True),
        ("execution_status", "success"),
        ("customer_email", "someone@example.com"),
    ]:
        refused(
            f"extra field {field!r} rejected",
            lambda field=field, value=value: PolicyVerdict(**{**BASE, field: value}),
        )

    surface = set(PolicyVerdict.model_fields)
    print(f"\n        field surface: {sorted(surface)}")
    forbidden = re.compile(
        r"razorpay|execut|charg|sent|deliver|paid|refund|link_id|notify|notification",
        re.IGNORECASE,
    )
    offenders = {name for name in surface if forbidden.search(name)}
    check(
        "no field name suggests an action was performed",
        not offenders,
        f"suspicious: {sorted(offenders)}",
    )
    check(
        "the surface is exactly the six agreed fields",
        surface
        == {
            "event_id",
            "decision_id",
            "decision_version",
            "verdict",
            "reason",
            "checks_performed",
            "evaluated_at",
        },
        f"got {sorted(surface)}",
    )


def test_contradictions() -> None:
    section("2. Contradiction — the verdict, reason, and trail must agree")

    # The user's example, stated verbatim in the spec.
    refused(
        'verdict="authorized" with reason="customer_opted_out"',
        lambda: PolicyVerdict(
            **{**BASE, "verdict": "authorized", "reason": "customer_opted_out"}
        ),
    )
    refused(
        'verdict="blocked" with reason="ok"',
        lambda: PolicyVerdict(**{**BASE, "verdict": "blocked"}),
    )
    refused(
        'verdict="requires_manual_review" with reason="ok"',
        lambda: PolicyVerdict(**{**BASE, "verdict": "requires_manual_review"}),
    )
    refused(
        'a block reason carrying verdict="requires_manual_review"',
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "requires_manual_review",
                "reason": "contact_cap_exceeded",
                "checks_performed": fail_at(2, "contact_cap", "3 of 3 contacts used"),
            }
        ),
    )
    refused(
        'a review reason carrying verdict="blocked"',
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "blocked",
                "reason": "amount_never_auto",
                "checks_performed": fail_at(5, "amount_tier", "48,000.00 is too large"),
            }
        ),
    )
    refused(
        "an invented reason code",
        lambda: PolicyVerdict(**{**BASE, "reason": "seems_fine_to_me"}),
    )
    refused(
        "an invented verdict value",
        lambda: PolicyVerdict(**{**BASE, "verdict": "probably"}),
    )

    section("2b. The reason must be backed by an actual failure in the trail")
    refused(
        "blocked on a reason whose check PASSED",
        lambda: PolicyVerdict(
            **{**BASE, "verdict": "blocked", "reason": "customer_opted_out"}
        ),
    )
    refused(
        "authorized while a check in the trail FAILED",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "checks_performed": fail_at(
                    1, "customer_opt_out", "customer cust_x is on the list"
                ),
            }
        ),
    )
    refused(
        "reporting a failure other than the one recorded",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "blocked",
                "reason": "cooldown_active",
                "checks_performed": fail_at(2, "contact_cap", "3 of 3 contacts used"),
            }
        ),
    )

    section("2c. Precedence cannot be gamed to report a softer reason")
    # Opted out AND capped: reporting the cap hides the consent violation, and
    # reporting a review-tier reason would upgrade a hard block into a maybe.
    both = fail_at(1, "customer_opt_out", "customer cust_x is on the list")
    both[2] = format_check("contact_cap", False, "3 of 3 contacts used")
    refused(
        "two failures, reporting the lower-precedence one",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "blocked",
                "reason": "contact_cap_exceeded",
                "checks_performed": both,
            }
        ),
    )
    accepted = PolicyVerdict(
        **{
            **BASE,
            "verdict": "blocked",
            "reason": "customer_opted_out",
            "checks_performed": both,
        }
    )
    check(
        "two failures, reporting the highest-precedence one is accepted",
        accepted.reason == "customer_opted_out",
    )

    # A block outranking a review must not be downgraded to a review.
    block_and_review = fail_at(1, "customer_opt_out", "customer cust_x is on the list")
    block_and_review[5] = format_check("amount_tier", False, "48,000.00 is too large")
    refused(
        "a block and a review together, reported as the review",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "requires_manual_review",
                "reason": "amount_never_auto",
                "checks_performed": block_and_review,
            }
        ),
    )


def test_trail_tampering() -> None:
    section("3. Trail tampering — a short-circuited evaluation must not be expressible")

    refused(
        "an empty trail",
        lambda: PolicyVerdict(**{**BASE, "checks_performed": []}),
    )
    refused(
        "a trail stopping at the first failure (1 entry)",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "verdict": "blocked",
                "reason": "customer_opted_out",
                "checks_performed": [
                    format_check("customer_opt_out", False, "on the list")
                ],
            }
        ),
    )
    refused(
        "a trail missing one check",
        lambda: PolicyVerdict(**{**BASE, "checks_performed": VALID_TRAIL[:-1]}),
    )
    refused(
        "a trail padded with a duplicate to reach the right length",
        lambda: PolicyVerdict(
            **{**BASE, "checks_performed": VALID_TRAIL[:-1] + [VALID_TRAIL[0]]}
        ),
    )
    refused(
        "a trail containing an undeclared check name",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "checks_performed": VALID_TRAIL[:-1]
                + ["vibe_check: PASS (looks fine)"],
            }
        ),
    )
    refused(
        "a free-text trail entry that is not in the canonical format",
        lambda: PolicyVerdict(
            **{
                **BASE,
                "checks_performed": VALID_TRAIL[:-1] + ["everything looked fine to me"],
            }
        ),
    )
    refused(
        "a trail entry with no detail",
        lambda: PolicyVerdict(
            **{**BASE, "checks_performed": VALID_TRAIL[:-1] + ["amount_tier: PASS ()"]}
        ),
    )
    refused(
        "format_check refusing to mint an undeclared check",
        lambda: format_check("vibe_check", True, "looks fine"),
    )
    refused(
        "format_check refusing to mint an empty detail",
        lambda: format_check("amount_tier", True, "   "),
    )

    section("3b. Reference shape")
    refused(
        "a decision_id that is not an ObjectId",
        lambda: PolicyVerdict(**{**BASE, "decision_id": "not-an-object-id"}),
    )
    refused(
        "decision_version 0",
        lambda: PolicyVerdict(**{**BASE, "decision_version": 0}),
    )
    refused(
        "a negative decision_version",
        lambda: PolicyVerdict(**{**BASE, "decision_version": -1}),
    )
    refused(
        "a blank event_id",
        lambda: PolicyVerdict(**{**BASE, "event_id": ""}),
    )


def test_context_invariants() -> None:
    section("4. PolicyContext — the facts handed to the engine must be coherent")

    # Required rather than defaulted to None on purpose: a caller who forgets the
    # timestamp would otherwise be silently telling policy "never contacted", which
    # is the permissive direction. Omission has to be an error.
    refused(
        "omitting last_authorized_contact_at entirely",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=0,
        ),
    )
    refused(
        "a negative prior-contact count",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=-1,
            last_authorized_contact_at=None,
        ),
    )
    refused(
        "a naive (timezone-less) clock",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=0,
            last_authorized_contact_at=None,
            now=datetime(2026, 1, 1),
        ),
    )
    refused(
        "a naive last-contact timestamp",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=1,
            last_authorized_contact_at=datetime(2026, 1, 1),
        ),
    )
    refused(
        "contacts counted but no timestamp for the latest",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=2,
            last_authorized_contact_at=None,
        ),
    )
    refused(
        "a last-contact timestamp with a zero count",
        lambda: PolicyContext(
            customer_ref="cust_x",
            customer_opted_out=False,
            prior_authorized_contacts=0,
            last_authorized_contact_at=datetime.now(timezone.utc),
        ),
    )


def test_parameter_guard() -> None:
    section("5. Policy parameters — an incoherent configuration must not load")

    #: Each mutation is applied to the live module, validated, then reverted, so a
    #: failure in the middle cannot leave the process running on a bad policy.
    MUTATIONS = [
        ("the never-auto ceiling below the auto limit", "NEVER_AUTO_AT_OR_ABOVE", 1_000.0),
        ("the two amount thresholds equal", "NEVER_AUTO_AT_OR_ABOVE", 5_000.0),
        ("an auto limit of 0", "AUTO_AUTHORIZE_BELOW", 0.0),
        ("a contact cap of 0", "MAX_CONTACTS_PER_EVENT", 0),
        ("a negative ERV floor", "MINIMUM_ERV", -1.0),
        ("a negative cooldown", "COOLDOWN_HOURS", -1),
        (
            "a contact set naming an intervention that does not exist",
            "CONTACT_INTERVENTIONS",
            frozenset({"send_a_goon"}),
        ),
        (
            "a contact set including a no-action variant",
            "CONTACT_INTERVENTIONS",
            frozenset({"reminder", "no_action"}),
        ),
        ("an empty contact set", "CONTACT_INTERVENTIONS", frozenset()),
    ]

    for label, name, value in MUTATIONS:
        original = getattr(rules, name)
        try:
            setattr(rules, name, value)
            refused(f"rejects {label}", rules._validate_parameters)
        finally:
            setattr(rules, name, original)

    rules._validate_parameters()
    check("the real configuration still validates after every mutation", True)

    section("5b. Tier boundaries fall to the cautious side")
    check(
        f"exactly {rules.AUTO_AUTHORIZE_BELOW:,.2f} is NOT autonomous",
        rules.tier_for(rules.AUTO_AUTHORIZE_BELOW) == "approval_required",
        rules.tier_for(rules.AUTO_AUTHORIZE_BELOW),
    )
    check(
        f"a hair under {rules.AUTO_AUTHORIZE_BELOW:,.2f} is autonomous",
        rules.tier_for(rules.AUTO_AUTHORIZE_BELOW - 0.01) == "auto",
    )
    check(
        f"exactly {rules.NEVER_AUTO_AT_OR_ABOVE:,.2f} is never-auto",
        rules.tier_for(rules.NEVER_AUTO_AT_OR_ABOVE) == "never_auto",
    )
    check(
        f"a hair under {rules.NEVER_AUTO_AT_OR_ABOVE:,.2f} only needs approval",
        rules.tier_for(rules.NEVER_AUTO_AT_OR_ABOVE - 0.01) == "approval_required",
    )
    check(
        "zero is autonomous, not an edge case",
        rules.tier_for(0.0) == "auto",
    )


def test_import_boundary() -> None:
    section("6. Import boundary — no execution or LLM machinery inside app/policy/")

    banned = re.compile(
        r"^\s*(?:from|import)\s+.*"
        r"(razorpay|google\.generativeai|google\.genai|openai|anthropic|httpx|requests|aiohttp)",
        re.IGNORECASE | re.MULTILINE,
    )
    for path in sorted((ROOT / "app" / "policy").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        hits = banned.findall(source)
        check(f"{path.name} imports nothing that could act", not hits, f"found {hits}")

    engine = (ROOT / "app" / "policy" / "engine.py").read_text(encoding="utf-8")
    check(
        "engine.py holds no database handle either (purity, not just no-LLM)",
        "get_database" not in engine and "motor" not in engine,
    )
    check(
        "engine.py contains no 'await' — nothing in it can do I/O",
        "await " not in engine,
    )

    verdict_model = (ROOT / "app" / "models" / "policy.py").read_text(encoding="utf-8")
    check(
        "the policy model module imports no execution client",
        not banned.findall(verdict_model),
    )

    routes = (ROOT / "app" / "routes" / "policy.py").read_text(encoding="utf-8")
    check(
        "the policy routes expose no execute endpoint",
        "/execute" not in routes and "razorpay" not in routes.lower(),
    )


async def test_referential_attacks() -> None:
    section("7. Referential attacks against the write boundary")

    await connect_to_mongo()
    database = get_database()
    verdicts_before = await database["policy_verdicts"].count_documents({})

    document = await latest_decision("dec_S3_TINYINV")
    decision = DecisionRecord.from_document(document)
    context = PolicyContext(
        customer_ref="cust_dec_s3_tinyinv",
        customer_opted_out=False,
        prior_authorized_contacts=0,
        last_authorized_contact_at=None,
        # Well clear of the cooldown, so the honest verdict is `authorized` and
        # any refusal below is the referential guard, not a policy failure.
        now=datetime.now(timezone.utc) + timedelta(days=30),
    )
    honest = evaluate(decision=decision, context=context)
    check(
        "the control verdict is authorized (so refusals below are referential)",
        honest.verdict == "authorized",
        f"{honest.verdict}/{honest.reason}",
    )

    async def expect(name: str, mutation: dict, error: type[Exception]) -> None:
        candidate = honest.model_copy(update=mutation)
        try:
            await append_verdict(candidate)
        except error as exc:
            check(name, True)
            print(f"        refused: {str(exc)[:150]}")
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"raised {type(exc).__name__} instead of {error.__name__}: {exc}")
        else:
            check(name, False, "the write was accepted")

    await expect(
        "a decision_id that exists nowhere",
        {"decision_id": "0" * 24},
        DanglingDecisionReference,
    )

    other = await latest_decision("dec_S3_GHOST")
    await expect(
        "another event's decision",
        {"decision_id": str(other["_id"])},
        DanglingDecisionReference,
    )

    await expect(
        "the right decision at the wrong version",
        {"decision_version": decision.version + 7},
        DanglingDecisionReference,
    )

    # A superseded decision: pin the event's v1 while a higher version exists.
    versions = (
        await database["decisions"]
        .find({"event_id": "pol_S4_CAP"}, {"version": 1})
        .to_list(length=None)
    )
    highest = max(int(v["version"]) for v in versions)
    if highest > 1:
        superseded = await database["decisions"].find_one(
            {"event_id": "pol_S4_CAP", "version": 1}
        )
        stale_decision = DecisionRecord.from_document(superseded)
        stale_verdict = evaluate(
            decision=stale_decision,
            context=PolicyContext(
                customer_ref="cust_pol_S4_cap",
                customer_opted_out=False,
                prior_authorized_contacts=0,
                last_authorized_contact_at=None,
                now=datetime.now(timezone.utc) + timedelta(days=30),
            ),
        )
        try:
            await append_verdict(stale_verdict)
        except StaleDecisionReference as exc:
            check(
                f"a superseded decision (v1, superseded by v{highest})", True
            )
            print(f"        refused: {str(exc)[:170]}")
        except Exception as exc:  # noqa: BLE001
            check("a superseded decision", False, f"raised {type(exc).__name__}: {exc}")
        else:
            check("a superseded decision", False, "the write was accepted")
    else:
        failed.append("pol_S4_CAP has only one decision version; staleness untested")
        print("  SKIP  a superseded decision — no second version exists to supersede v1")

    verdicts_after = await database["policy_verdicts"].count_documents({})
    check(
        "not one refused write left a document behind",
        verdicts_after == verdicts_before,
        f"policy_verdicts went from {verdicts_before} to {verdicts_after}",
    )
    print(f"        policy_verdicts holds {verdicts_after} documents, unchanged")

    await close_mongo_connection()


async def main() -> None:
    print("Stage 4 adversarial tests — every case here is an attack that must fail")

    test_baseline()
    test_execution_smuggling()
    test_contradictions()
    test_trail_tampering()
    test_context_invariants()
    test_parameter_guard()
    test_import_boundary()
    await test_referential_attacks()

    print("\n" + "=" * 78)
    print(f"{passed} attacks refused, {len(failed)} got through")
    if failed:
        for problem in failed:
            print(f"  - {problem}")
        sys.exit(1)
    print("the policy layer held on every front")


if __name__ == "__main__":
    asyncio.run(main())
