"""Stage 5 adversarial tests — try to make the executor act without permission,
or claim more than it knows.

Same posture as the earlier adversarial suites: every case here is an *attack*, and
passing means the attack was refused. Stage 5 is the first stage that can spend
money, so the attacks divide into two families — "act when you were not allowed to"
and "say something you cannot know" — across eight fronts:

1. **Outcome smuggling.** `ExecutionRecord` must have no field capable of saying the
   money came back, and must refuse an extra that tries to add one. `status` is
   about the API call and nothing else.
2. **Non-executable interventions.** The three `no_action` variants have no entry in
   `ACTION_FOR_INTERVENTION`, so a record claiming to have executed one must be
   unconstructable rather than quietly skipped.
3. **Action-type forgery and half-facts.** A reminder recorded as a payment link, a
   completed link with no link, a contact carrying a link id, a failure with no
   reason — each is a record that would read as evidence of something that did not
   happen.
4. **The type-level gate.** A blocked or review-pending verdict must not be
   narrowable to `AuthorizedVerdict`, which is the executor's argument type.
5. **The same thing over HTTP.** A genuinely blocked event and a genuinely
   review-pending one must both come back 409 from `POST /execute`, with nothing
   written.
6. **The write-time referential guard.** Every attack in front 5 bypasses the type
   entirely and writes to the store directly — including one that forges an
   authorization in memory and gets past `require_authorized`. The guard re-reads
   the database, so it refuses anyway. This is the front that shows why a type is
   not enough: it is a claim about code paths, not about rows.
7. **Real failure.** A deliberately wrong Razorpay key, passed explicitly so no
   module global is mutated. The attempt must be recorded as `failed` with a real
   reason, must leak no credential, and must consume neither a contact-cap slot nor
   a cooldown — otherwise a failed send would silence the next real one.
8. **Boundaries.** No LLM anywhere under `app/execution/`, because contact content
   is templated and a generated reminder is a legal artifact nobody reviewed. No
   second caller of `execute`. No `?force=`.

Front 7 makes real HTTP calls to Razorpay: one that is rejected on purpose, and one
that succeeds as the control. Fronts 5 and 6 need a running server and the live
database.

Run:  python scripts/s5_adversarial.py [base_url] [tag]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bson import ObjectId
from pydantic import ValidationError

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.execution import razorpay
from app.execution import service as execution_service
from app.execution import store as execution_store
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    AuthorizedVerdict,
    ExecutionRecord,
    NotAuthorized,
    require_authorized,
)
from app.models.policy import POLICY_CHECKS, format_check
from app.policy import current_fingerprint, rules
from app.policy.store import prior_authorized_contacts

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
TAG = (
    sys.argv[2]
    if len(sys.argv) > 2
    else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
)

#: Two customers: the opt-out is permanent and global per customer, so the blocked
#: fixture cannot share one with the fixtures that need to be authorized.
CUSTOMER = f"cust_s5adv_{TAG}"
CUSTOMER_OPTED = f"cust_s5adv_opt_{TAG}"

#: Deliberately wrong, and passed as an argument rather than patched into settings.
#: `create_payment_link` takes credentials for exactly this reason.
BAD_KEYS = razorpay.RazorpayCredentials(
    key_id="rzp_test_s5advBADKEY",
    key_secret=f"s5adv-wrong-secret-{TAG}",
)

AWARE_NOW = datetime.now(timezone.utc)

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    global passed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")
    return condition


def refused(name: str, build, expect: type[BaseException] | None = None) -> None:
    """Assert that constructing or storing something invalid raises.

    `expect` pins the exception type when the *kind* of refusal is the point — a
    blocked verdict refused as "stale" would be the right answer for the wrong
    reason, and the difference matters when the message ends up in an audit.
    """
    try:
        result = build()
    except (ValidationError, ValueError, TypeError, RuntimeError, LookupError) as exc:
        if expect is not None and not isinstance(exc, expect):
            check(
                name,
                False,
                f"refused with {type(exc).__name__}, expected {expect.__name__}: {exc}",
            )
            return
        lines = [line.strip() for line in str(exc).strip().splitlines()]
        message = next(
            (line for line in lines if "Value error" in line or "Extra" in line),
            lines[0] if lines else "",
        )
        check(name, True)
        print(f"        refused: {message[:160]}")
    else:
        check(name, False, f"accepted it and returned {result!r}"[:200])


async def refused_async(name: str, coroutine_factory, expect: type[BaseException]) -> None:
    """`refused`, for the store's async write boundary."""
    try:
        result = await coroutine_factory()
    except Exception as exc:  # noqa: BLE001 - the type is what is being asserted
        if not isinstance(exc, expect):
            check(
                name,
                False,
                f"refused with {type(exc).__name__}, expected {expect.__name__}: {exc}",
            )
            return
        check(name, True)
        print(f"        refused: {str(exc)[:170]}")
    else:
        check(name, False, f"the write was accepted: {result!r}"[:200])


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
    except urllib.error.URLError as exc:
        # Nothing listening. Reported as 0 so the reachability probe can say so
        # plainly rather than ending the run in a socket traceback.
        return 0, {"detail": str(exc.reason)}


# ---------------------------------------------------------------------------
# Valid records to perturb. Everything in fronts 1-3 is a mutation of one of these.
# ---------------------------------------------------------------------------

CONTACT_RECORD: dict[str, Any] = {
    "event_id": "evt_s5adv",
    "policy_verdict_id": "a" * 24,
    "policy_verdict_version": 1,
    "intervention": "reminder",
    "action_type": "contact_logged",
    "contact_channel": "email",
    "contact_message_summary": "reminder.v1 v1 via email — Payment reminder",
    "executed_at": AWARE_NOW,
    "status": "completed",
}

LINK_RECORD: dict[str, Any] = {
    "event_id": "evt_s5adv",
    "policy_verdict_id": "b" * 24,
    "policy_verdict_version": 1,
    "intervention": "recovery_payment_link",
    "action_type": "payment_link_generated",
    "razorpay_payment_link_id": "plink_S5ADVERSARIAL",
    "razorpay_payment_link_url": "https://rzp.io/i/s5adv",
    "executed_at": AWARE_NOW,
    "status": "completed",
}

FAILED_RECORD: dict[str, Any] = {
    "event_id": "evt_s5adv",
    "policy_verdict_id": "c" * 24,
    "policy_verdict_version": 1,
    "intervention": "recovery_payment_link",
    "action_type": "payment_link_generated",
    "executed_at": AWARE_NOW,
    "status": "failed",
    "failure_reason": "Razorpay returned HTTP 401 — BAD_REQUEST_ERROR",
}


VALID_TRAIL = [
    format_check(
        "decision_is_actionable", True, "payment_method_update_link is a real intervention"
    ),
    format_check("customer_opt_out", True, "customer cust_s5adv has not opted out"),
    format_check("contact_cap", True, "0 of 3 contacts used for event evt_s5adv"),
    format_check("contact_cooldown", True, "no prior authorized contact"),
    format_check("erv_minimum", True, "ERV 800.00 clears the 25.00 minimum"),
    format_check("amount_tier", True, "2,400.00 is below the 5,000.00 limit"),
]


def trail_failing(name: str, detail: str) -> list[str]:
    """The full six-entry trail with one named check recorded as FAIL."""
    entries = list(VALID_TRAIL)
    entries[POLICY_CHECKS.index(name)] = format_check(name, False, detail)
    return entries


def verdict_document(
    *,
    verdict: str,
    reason: str,
    checks: list[str],
    version: int = 1,
) -> dict[str, Any]:
    """A stored-verdict document shaped exactly as MongoDB would return one."""
    return {
        "_id": ObjectId(),
        "event_id": "evt_s5adv",
        "decision_id": "d" * 24,
        "decision_version": 1,
        "verdict": verdict,
        "reason": reason,
        "checks_performed": checks,
        "evaluated_at": AWARE_NOW,
        "rulebook_fingerprint": current_fingerprint(),
        "version": version,
    }


AUTHORIZED_DOC = verdict_document(
    verdict="authorized", reason="ok", checks=list(VALID_TRAIL)
)
BLOCKED_DOC = verdict_document(
    verdict="blocked",
    reason="customer_opted_out",
    checks=trail_failing("customer_opt_out", "customer cust_s5adv has opted out"),
)
REVIEW_DOC = verdict_document(
    verdict="requires_manual_review",
    reason="amount_never_auto",
    checks=trail_failing("amount_tier", "30,000.00 is at or above the 25,000.00 ceiling"),
)


# ---------------------------------------------------------------------------
# 0. Baseline
# ---------------------------------------------------------------------------


def test_baseline() -> None:
    section("0. Baseline — the honest records must still be constructable")

    for label, payload in (
        ("a completed contact", CONTACT_RECORD),
        ("a completed payment link", LINK_RECORD),
        ("a failed attempt", FAILED_RECORD),
    ):
        try:
            record = ExecutionRecord(**payload)
        except ValidationError as exc:
            check(f"{label} validates", False, str(exc)[:200])
        else:
            check(f"{label} validates ({record.action_type}/{record.status})", True)

    check(
        "the type-level gate accepts an authorized verdict",
        require_authorized(AUTHORIZED_DOC).verdict == "authorized",
    )


# ---------------------------------------------------------------------------
# 1. Outcome smuggling
# ---------------------------------------------------------------------------


def test_outcome_smuggling() -> None:
    section("1. Outcome smuggling — the record must not be able to claim success")
    print(
        "   Stage 5 knows one thing: an action was attempted. Whether the customer\n"
        "   paid is Stage 6's subject and needs its own evidence. Any field that\n"
        "   could answer it here would be a boolean set hopefully at send time.\n"
    )

    for field, value in [
        ("money_recovered", True),
        ("customer_paid", True),
        ("amount_received", 2_400.0),
        ("amount_recovered", 2_400.0),
        ("revenue_recovered", 2_400.0),
        ("outcome", "success"),
        ("success", True),
        ("paid", True),
        ("recovered", True),
        ("verified", True),
        ("settled_at", AWARE_NOW),
        ("razorpay_payment_id", "pay_S5ADVERSARIAL"),
        ("link_opened", True),
        # Idempotency's mirror image: a permission is spent once, so there is no
        # version to increment. A record that could carry one could be re-issued.
        ("version", 2),
    ]:
        refused(
            f"extra field {field!r} rejected",
            lambda field=field, value=value: ExecutionRecord(
                **{**LINK_RECORD, field: value}
            ),
        )

    refused(
        "status 'recovered' is not an execution status",
        lambda: ExecutionRecord(**{**LINK_RECORD, "status": "recovered"}),
    )
    refused(
        "status 'paid' is not an execution status",
        lambda: ExecutionRecord(**{**LINK_RECORD, "status": "paid"}),
    )

    surface = set(ExecutionRecord.model_fields)
    print(f"\n        field surface: {sorted(surface)}")
    outcome_words = re.compile(
        r"recover|receiv|collect|settl|refund|\bpaid\b|success|outcome|verifi"
        r"|confirm|reconcil|opened|clicked|responded",
        re.IGNORECASE,
    )
    offenders = {name for name in surface if outcome_words.search(name)}
    check(
        "no declared field name claims an outcome",
        not offenders,
        f"suspicious: {sorted(offenders)}",
    )
    check(
        "there is no version field, so a spent permission cannot be re-issued",
        "version" not in surface,
    )


# ---------------------------------------------------------------------------
# 2. Non-executable interventions
# ---------------------------------------------------------------------------


def test_no_action_is_unconstructable() -> None:
    section("2. The three ways of doing nothing cannot be recorded as done")
    print(
        "   Policy refuses to authorize a no_action recommendation, so one arriving\n"
        "   here is not an edge case to skip quietly — it means the decision, the\n"
        "   verdict and the catalogue disagree about what was permitted.\n"
    )

    for intervention in (
        "no_action",
        "no_action_low_confidence",
        "no_action_negative_erv",
    ):
        check(
            f"{intervention!r} has no entry in ACTION_FOR_INTERVENTION",
            intervention not in ACTION_FOR_INTERVENTION,
            str(ACTION_FOR_INTERVENTION.get(intervention)),
        )
        refused(
            f"an execution record for {intervention!r} is unconstructable",
            lambda intervention=intervention: ExecutionRecord(
                **{**CONTACT_RECORD, "intervention": intervention}
            ),
        )

    refused(
        "an intervention outside the Stage 3 catalogue is refused",
        lambda: ExecutionRecord(
            **{**CONTACT_RECORD, "intervention": "wire_transfer_demand"}
        ),
    )
    check(
        "the executor raises rather than skipping when the table has no action",
        "Policy is supposed to block this" in inspect.getsource(execution_service.execute),
    )


# ---------------------------------------------------------------------------
# 3. Action-type forgery and half-facts
# ---------------------------------------------------------------------------


def test_forgery_and_half_facts() -> None:
    section("3. Action-type forgery and half-facts")
    print(
        "   The action type is derived from the intervention by a declared table, so\n"
        "   it is not a free choice. And each optional field is populated exactly\n"
        "   when its action requires it — a half-fact reads as evidence.\n"
    )

    for intervention, action_type, note in [
        ("reminder", "payment_link_generated", "a reminder recorded as a payment link"),
        ("recovery_payment_link", "contact_logged", "a payment link recorded as a contact"),
        ("immediate_retry", "payment_link_generated", "a retry recorded as a plain link"),
        ("manual_escalation", "retry_simulated", "an escalation recorded as a retry"),
    ]:
        refused(
            note,
            lambda intervention=intervention, action_type=action_type: ExecutionRecord(
                **{
                    **LINK_RECORD,
                    "intervention": intervention,
                    "action_type": action_type,
                }
            ),
        )

    refused(
        "a completed link with no link id",
        lambda: ExecutionRecord(
            **{**LINK_RECORD, "razorpay_payment_link_id": None}
        ),
    )
    refused(
        "a completed link with no URL",
        lambda: ExecutionRecord(
            **{**LINK_RECORD, "razorpay_payment_link_url": None}
        ),
    )
    refused(
        "a contact record carrying a Razorpay link id",
        lambda: ExecutionRecord(
            **{**CONTACT_RECORD, "razorpay_payment_link_id": "plink_S5ADVERSARIAL"}
        ),
    )
    refused(
        "a link record carrying contact fields",
        lambda: ExecutionRecord(**{**LINK_RECORD, "contact_channel": "email"}),
    )
    refused(
        "a completed contact with no channel",
        lambda: ExecutionRecord(**{**CONTACT_RECORD, "contact_channel": None}),
    )
    refused(
        "a completed contact with no message summary",
        lambda: ExecutionRecord(**{**CONTACT_RECORD, "contact_message_summary": None}),
    )
    refused(
        "a FAILED attempt that nonetheless carries a link",
        lambda: ExecutionRecord(
            **{
                **FAILED_RECORD,
                "razorpay_payment_link_id": "plink_S5ADVERSARIAL",
                "razorpay_payment_link_url": "https://rzp.io/i/s5adv",
            }
        ),
    )
    refused(
        "a failure with no reason",
        lambda: ExecutionRecord(**{**FAILED_RECORD, "failure_reason": None}),
    )
    refused(
        "a success carrying a failure reason",
        lambda: ExecutionRecord(
            **{**LINK_RECORD, "failure_reason": "but it worked anyway"}
        ),
    )
    refused(
        "a naive executed_at, which the cooldown would fail to subtract",
        lambda: ExecutionRecord(
            **{**LINK_RECORD, "executed_at": AWARE_NOW.replace(tzinfo=None)}
        ),
    )
    refused(
        "an http:// payment URL",
        lambda: ExecutionRecord(
            **{**LINK_RECORD, "razorpay_payment_link_url": "http://rzp.io/i/s5adv"}
        ),
    )
    refused(
        "an action type outside the declared three",
        lambda: ExecutionRecord(**{**LINK_RECORD, "action_type": "money_moved"}),
    )
    refused(
        "a policy_verdict_id that is not an ObjectId",
        lambda: ExecutionRecord(**{**LINK_RECORD, "policy_verdict_id": "not-an-id"}),
    )


# ---------------------------------------------------------------------------
# 4. The type-level gate
# ---------------------------------------------------------------------------


def test_type_level_gate() -> None:
    section("4. The type-level gate — an unauthorized verdict cannot be narrowed")
    print(
        "   `execute` takes an AuthorizedVerdict, not a verdict. There is no branch\n"
        "   inside it deciding whether execution is permitted, because an instance\n"
        "   that should not proceed cannot be constructed as its argument.\n"
    )

    refused(
        "require_authorized refuses a blocked verdict",
        lambda: require_authorized(BLOCKED_DOC),
        expect=NotAuthorized,
    )
    refused(
        "require_authorized refuses a review-pending verdict",
        lambda: require_authorized(REVIEW_DOC),
        expect=NotAuthorized,
    )
    refused(
        "require_authorized refuses a document with no verdict at all",
        lambda: require_authorized(
            {key: value for key, value in AUTHORIZED_DOC.items() if key != "verdict"}
        ),
        expect=NotAuthorized,
    )
    refused(
        "require_authorized is case-sensitive ('AUTHORIZED' is not 'authorized')",
        lambda: require_authorized({**AUTHORIZED_DOC, "verdict": "AUTHORIZED"}),
        expect=NotAuthorized,
    )

    # Bypassing the helper and hitting the model directly must fail too, otherwise
    # the gate would be a convention rather than a type.
    refused(
        "AuthorizedVerdict itself refuses a blocked document",
        lambda: AuthorizedVerdict.from_document(BLOCKED_DOC),
        expect=ValidationError,
    )
    refused(
        "AuthorizedVerdict itself refuses a review-pending document",
        lambda: AuthorizedVerdict.from_document(REVIEW_DOC),
        expect=ValidationError,
    )
    refused(
        "AuthorizedVerdict refuses 'authorized' paired with a refusing reason",
        lambda: AuthorizedVerdict.from_document(
            {**AUTHORIZED_DOC, "reason": "customer_opted_out"}
        ),
        expect=ValidationError,
    )

    signature = inspect.signature(execution_service.execute)
    annotation = signature.parameters["verdict"].annotation
    check(
        "execute's verdict parameter is annotated AuthorizedVerdict",
        "AuthorizedVerdict" in str(annotation),
        str(annotation),
    )
    body = inspect.getsource(execution_service.execute)
    check(
        "execute's body contains no verdict/permission branch of its own",
        'verdict.verdict' not in body and '== "authorized"' not in body,
    )
    check(
        "execute does accept a credentials seam, so the failure test needs no monkeypatch",
        "credentials" in signature.parameters,
    )


# ---------------------------------------------------------------------------
# 8. Boundaries (source-level; run early because it needs nothing)
# ---------------------------------------------------------------------------


def test_boundaries() -> None:
    section("8. Boundaries — what the execution layer is not allowed to reach for")

    llm = re.compile(
        r"^\s*(?:from|import)\s+.*"
        r"(google\.generativeai|google\.genai|\bgenai\b|openai|anthropic"
        r"|app\.diagnosis\.gemini)",
        re.IGNORECASE | re.MULTILINE,
    )
    http = re.compile(
        r"^\s*(?:from|import)\s+.*(httpx|requests|aiohttp|urllib)",
        re.IGNORECASE | re.MULTILINE,
    )
    for path in sorted((ROOT / "app" / "execution").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        check(
            f"{path.name} imports no LLM client",
            not llm.findall(source),
            f"found {llm.findall(source)}",
        )
        if path.name != "razorpay.py":
            check(
                f"{path.name} reaches the network through razorpay.py or not at all",
                not http.findall(source),
                f"found {http.findall(source)}",
            )

    templates_source = (ROOT / "app" / "execution" / "templates.py").read_text(
        encoding="utf-8"
    )
    check(
        "templates.py builds messages by .format() on declared shells, not generation",
        ".format(**values)" in templates_source,
    )

    razorpay_source = (ROOT / "app" / "execution" / "razorpay.py").read_text(
        encoding="utf-8"
    )
    check(
        "notifications are hard-coded false in the request payload",
        '"notify": {"sms": False, "email": False}' in razorpay_source
        and '"reminder_enable": False' in razorpay_source,
    )
    link_parameters = set(inspect.signature(razorpay.create_payment_link).parameters)
    check(
        "there is no parameter that could switch notifications back on",
        not (link_parameters & {"notify", "notify_sms", "notify_email", "reminder_enable"}),
        str(sorted(link_parameters)),
    )
    check(
        "the failure path redacts before it stores",
        "_redact(" in razorpay_source,
    )

    # Scan the handler's *signature*, not the module text: the docstring says the
    # words "force" and "override" in the course of explaining that neither exists,
    # and a test that failed on that would be reading prose as behaviour.
    from app.routes import executions as executions_route

    handler = inspect.signature(executions_route.execute_event)
    knobs = set(handler.parameters) - {"event_id", "response"}
    check(
        "the execute route takes an event id and nothing else",
        not knobs,
        f"extra parameters: {sorted(knobs)}",
    )
    print(f"        POST /execute/{{event_id}} parameters: {sorted(handler.parameters)}")
    for knob in ("force", "override", "verdict_id", "amount", "intervention"):
        check(
            f"...so there is no {knob!r} knob",
            knob not in handler.parameters,
        )

    # A revocation only means something if there is no way to reach past it. The
    # route reads the *latest* verdict, so a second call site — a batch job, a retry
    # worker — holding an older AuthorizedVerdict would be a hole the type cannot see.
    call = re.compile(r"(?:^|[\s.=(\[])execute\(")
    callers = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in sorted((ROOT / "app").rglob("*.py"))
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if call.search(line) and "def execute" not in line
    ]
    check(
        "exactly one place in app/ calls execute, and it is the route",
        len(callers) == 1 and "routes" in callers[0],
        f"callers: {callers}",
    )
    print(f"        sole caller: {callers[0] if callers else '(none)'}")


# ---------------------------------------------------------------------------
# 5 & 6 & 7 need the live system.
# ---------------------------------------------------------------------------


def seed(event_id: str, *, customer: str, amount: float) -> bool:
    """Ingest, diagnose and decide one card_expired payment failure.

    `card_expired` resolves through the rules path, so no Gemini quota is spent and
    the recommendation — `payment_method_update_link` — is both contact-type and
    link-producing. That combination is what lets front 7 fail a real Razorpay call
    and then ask whether a contact slot was consumed.
    """
    status, body = post(
        "/events",
        {
            "event_id": event_id,
            "surface": "payment",
            "amount": amount,
            "currency": "INR",
            "customer_ref": customer,
            "raw_failure_reason": "card_expired",
        },
    )
    if status >= 400:
        return check(f"ingest {event_id}", False, f"{status} {body}")
    status, diagnosis = post(f"/diagnose/{event_id}")
    if status >= 400:
        return check(f"diagnose {event_id}", False, f"{status} {diagnosis}")
    status, decision = post(f"/decide/{event_id}")
    if status >= 400:
        return check(f"decide {event_id}", False, f"{status} {decision}")
    return check(
        f"{event_id} -> {decision['recommended_intervention']}",
        decision["recommended_intervention"] == "payment_method_update_link",
        decision["recommended_intervention"],
    )


def record_naming(
    verdict_document_: dict[str, Any],
    *,
    event_id: str | None = None,
    version: int | None = None,
    intervention: str = "payment_method_update_link",
    status: str = "failed",
) -> ExecutionRecord:
    """A *valid* ExecutionRecord naming a given verdict.

    Valid on purpose: every refusal in front 6 has to come from the store's guard
    rather than from Pydantic, or the test would prove nothing about the database.
    """
    payload: dict[str, Any] = {
        "event_id": event_id or verdict_document_["event_id"],
        "policy_verdict_id": str(verdict_document_["_id"]),
        "policy_verdict_version": (
            version if version is not None else int(verdict_document_["version"])
        ),
        "intervention": intervention,
        "action_type": ACTION_FOR_INTERVENTION[intervention],
        "executed_at": datetime.now(timezone.utc),
        "status": status,
    }
    if status == "failed":
        payload["failure_reason"] = "an attack, which should never have been recorded"
    else:
        payload["razorpay_payment_link_id"] = "plink_S5ADVERSARIAL"
        payload["razorpay_payment_link_url"] = "https://rzp.io/i/s5adv"
    return ExecutionRecord(**payload)


async def test_live_fronts() -> None:
    database = get_database()
    executions = database["executions"]
    verdicts = database["policy_verdicts"]

    blocked_event = f"exe_S5ADV_{TAG}_BLOCKED"
    review_event = f"exe_S5ADV_{TAG}_REVIEW"
    guard_event = f"exe_S5ADV_{TAG}_GUARD"
    failkey_event = f"exe_S5ADV_{TAG}_FAILKEY"
    honest_event = f"exe_S5ADV_{TAG}_HONEST"

    # -- 5 -------------------------------------------------------------------
    section("5. Over HTTP — a verdict that did not permit anything returns 409")

    status, _ = post(f"/opt-out/{CUSTOMER_OPTED}")
    check(f"opted {CUSTOMER_OPTED} out", status in (200, 201), f"HTTP {status}")

    if not seed(blocked_event, customer=CUSTOMER_OPTED, amount=2_400.00):
        print("\nABORT: the blocked fixture did not resolve as intended.")
        sys.exit(1)
    if not seed(review_event, customer=CUSTOMER, amount=30_000.00):
        print("\nABORT: the review fixture did not resolve as intended.")
        sys.exit(1)

    before = await executions.count_documents({})

    status, blocked_verdict = post(f"/authorize/{blocked_event}")
    check(
        "the opted-out fixture is genuinely blocked",
        blocked_verdict.get("verdict") == "blocked"
        and blocked_verdict.get("reason") == "customer_opted_out",
        f"{blocked_verdict.get('verdict')}/{blocked_verdict.get('reason')}",
    )
    status, review_verdict = post(f"/authorize/{review_event}")
    check(
        "the 30,000 fixture genuinely requires review",
        review_verdict.get("verdict") == "requires_manual_review"
        and review_verdict.get("reason") == "amount_never_auto",
        f"{review_verdict.get('verdict')}/{review_verdict.get('reason')}",
    )

    for label, event_id, expected_reason in (
        ("blocked", blocked_event, "customer_opted_out"),
        ("review-pending", review_event, "amount_never_auto"),
    ):
        status, body = post(f"/execute/{event_id}")
        detail = body.get("detail", "") if isinstance(body, dict) else str(body)
        check(
            f"executing a {label} verdict returns 409",
            status == 409,
            f"HTTP {status}: {detail}",
        )
        check(
            f"the 409 names the real reason rather than paraphrasing it ({label})",
            expected_reason in detail,
            detail[:200],
        )
        print(f"        409: {detail[:150]}")

    # An event that has never been authorized is a 404, not a silent no-op.
    status, body = post(f"/execute/exe_S5ADV_{TAG}_NEVER_INGESTED")
    check("executing an unknown event is 404", status == 404, f"HTTP {status}")

    after = await executions.count_documents({})
    check(
        "not one refused execution wrote a document",
        after == before,
        f"executions went from {before} to {after}",
    )

    # -- 6 -------------------------------------------------------------------
    section("6. The write-time referential guard — bypassing the type entirely")
    print(
        "   Everything below skips require_authorized and writes to the store\n"
        "   directly. The type is a claim about code paths; the guard re-reads the\n"
        "   database, which is a claim about rows. Both are needed.\n"
    )

    if not seed(guard_event, customer=CUSTOMER, amount=2_300.00):
        print("\nABORT: the guard fixture did not resolve as intended.")
        sys.exit(1)
    status, guard_verdict = post(f"/authorize/{guard_event}")
    if not check(
        "the guard fixture is authorized (so refusals below are referential)",
        guard_verdict.get("verdict") == "authorized",
        f"{guard_verdict.get('verdict')}/{guard_verdict.get('reason')}",
    ):
        print("\nABORT: front 6 needs a genuinely authorized verdict.")
        sys.exit(1)

    guard_document = await verdicts.find_one({"_id": ObjectId(guard_verdict["id"])})
    blocked_document = await verdicts.find_one({"_id": ObjectId(blocked_verdict["id"])})
    review_document = await verdicts.find_one({"_id": ObjectId(review_verdict["id"])})

    before = await executions.count_documents({})

    await refused_async(
        "an execution naming a verdict that does not exist",
        lambda: execution_store.insert(
            record_naming(
                {"_id": ObjectId(), "event_id": guard_event, "version": 1},
            )
        ),
        execution_store.DanglingVerdictReference,
    )
    await refused_async(
        "an execution naming a verdict that belongs to another event",
        lambda: execution_store.insert(
            record_naming(guard_document, event_id=review_event)
        ),
        execution_store.DanglingVerdictReference,
    )
    await refused_async(
        "an execution claiming the wrong verdict version",
        lambda: execution_store.insert(record_naming(guard_document, version=2)),
        execution_store.DanglingVerdictReference,
    )
    await refused_async(
        "an execution of a BLOCKED verdict — the loudest error in the stage",
        lambda: execution_store.insert(record_naming(blocked_document)),
        execution_store.UnauthorizedVerdictReference,
    )
    await refused_async(
        "an execution of a REVIEW-PENDING verdict",
        lambda: execution_store.insert(record_naming(review_document)),
        execution_store.UnauthorizedVerdictReference,
    )

    # The headline attack: forge the authorization in memory. The type gate passes,
    # because a type can only inspect what it was handed. The database still says
    # blocked, so the row is refused.
    forged = {
        **blocked_document,
        "verdict": "authorized",
        "reason": "ok",
        "checks_performed": list(VALID_TRAIL),
    }
    narrowed = require_authorized(forged)
    check(
        "a forged document DOES get past the type gate (a type only sees its input)",
        narrowed.verdict == "authorized",
    )
    await refused_async(
        "...and the store refuses it anyway, because it re-reads the verdict",
        lambda: execution_store.insert(
            record_naming({**blocked_document, "version": blocked_document["version"]})
        ),
        execution_store.UnauthorizedVerdictReference,
    )

    await refused_async(
        "an execution recording an intervention that was not the one authorized",
        lambda: execution_store.insert(
            record_naming(guard_document, intervention="reminder")
        ),
        execution_store.InterventionMismatch,
    )

    # Staleness last: it needs a newer verdict, which retires the guard fixture.
    status, guard_v2 = post(f"/authorize/{guard_event}")
    check(
        f"re-authorizing the guard fixture produced v{guard_v2.get('version')}",
        int(guard_v2.get("version", 0)) > int(guard_document["version"]),
        str(guard_v2.get("version")),
    )
    await refused_async(
        "an execution of a permission that a later verdict has superseded",
        lambda: execution_store.insert(record_naming(guard_document)),
        execution_store.StaleVerdictReference,
    )

    after = await executions.count_documents({})
    check(
        "not one attack against the guard left a document behind",
        after == before,
        f"executions went from {before} to {after}",
    )
    print(f"        executions holds {after} documents, unchanged")

    # -- 7 -------------------------------------------------------------------
    section("7. Real failure — a deliberately wrong Razorpay key")
    print(
        "   The key is passed as an argument, not patched into settings, so nothing\n"
        "   global is mutated and every other reader still sees the real pair.\n"
        f"   Attacking with key_id {BAD_KEYS.key_id!r}.\n"
    )

    if not seed(failkey_event, customer=CUSTOMER, amount=2_600.00):
        print("\nABORT: the failure fixture did not resolve as intended.")
        sys.exit(1)
    status, failkey_verdict = post(f"/authorize/{failkey_event}")
    if not check(
        "the failure fixture is authorized before anything is attempted",
        failkey_verdict.get("verdict") == "authorized",
        f"{failkey_verdict.get('verdict')}/{failkey_verdict.get('reason')}",
    ):
        print("\nABORT: front 7 needs a genuinely authorized verdict.")
        sys.exit(1)

    failkey_document = await verdicts.find_one({"_id": ObjectId(failkey_verdict["id"])})
    authorized = require_authorized(failkey_document)
    outcome = await execution_service.execute(authorized, credentials=BAD_KEYS)
    record = outcome.record

    print(f"        status         {record.status}")
    print(f"        executed_at    {record.executed_at.isoformat()}")
    print(f"        failure_reason {record.failure_reason}")

    check("the attempt was recorded, not raised", outcome.created)
    check(
        "status is 'failed'",
        record.status == "failed",
        record.status,
    )
    check(
        "the reason is Razorpay's own, not a paraphrase",
        bool(record.failure_reason) and "Razorpay returned HTTP" in (record.failure_reason or ""),
        str(record.failure_reason),
    )
    check(
        "the failure was an authentication rejection (HTTP 401)",
        "401" in (record.failure_reason or ""),
        str(record.failure_reason),
    )
    check(
        "no artifact is claimed, because none was created",
        record.razorpay_payment_link_id is None
        and record.razorpay_payment_link_url is None,
        f"{record.razorpay_payment_link_id} / {record.razorpay_payment_link_url}",
    )
    check(
        "the bad secret does not appear in the stored reason",
        BAD_KEYS.key_secret not in (record.failure_reason or "")
        and BAD_KEYS.key_id not in (record.failure_reason or ""),
        "a credential reached the record",
    )

    # A second record for the same permission must hit the unique index, not the
    # service's check-first path — this call bypasses the service entirely.
    await refused_async(
        "a second record for the same permission is refused by the unique index",
        lambda: execution_store.insert(
            record_naming(failkey_document, status="failed")
        ),
        execution_store.DuplicateExecution,
    )

    # The whole point of §6: a failure must cost the customer nothing.
    count, last_contact = await prior_authorized_contacts(failkey_event)
    print(f"\n        prior_authorized_contacts({failkey_event}) = ({count}, {last_contact})")
    check(
        "a FAILED execution consumes no contact-cap slot",
        count == 0,
        f"count is {count}",
    )
    check(
        "a FAILED execution starts no cooldown",
        last_contact is None,
        f"anchor is {last_contact}",
    )

    status, recovery = post(f"/authorize/{failkey_event}")
    print(
        f"        re-authorize -> v{recovery.get('version')} "
        f"{recovery.get('verdict')}/{recovery.get('reason')}"
    )
    check(
        "re-authorization is therefore a usable recovery path after a failed send",
        recovery.get("verdict") == "authorized",
        f"{recovery.get('verdict')}/{recovery.get('reason')}",
    )

    # The control: a COMPLETED execution must do the opposite of all of the above.
    print(
        "\n   Control — the same fixture shape with the real key, so the contrast is\n"
        "   between two records written by one script in one minute under one rulebook.\n"
    )
    if not seed(honest_event, customer=CUSTOMER, amount=2_200.00):
        print("\nABORT: the control fixture did not resolve as intended.")
        sys.exit(1)
    status, honest_verdict = post(f"/authorize/{honest_event}")
    check(
        "the control fixture is authorized",
        honest_verdict.get("verdict") == "authorized",
        f"{honest_verdict.get('verdict')}/{honest_verdict.get('reason')}",
    )
    status, honest_record = post(f"/execute/{honest_event}")
    check(
        "the control executed for real (201)",
        status == 201 and honest_record.get("status") == "completed",
        f"HTTP {status}: {honest_record}",
    )
    if honest_record.get("razorpay_payment_link_url"):
        print(f"        LIVE URL       {honest_record['razorpay_payment_link_url']}")

    honest_count, honest_anchor = await prior_authorized_contacts(honest_event)
    print(
        f"        prior_authorized_contacts({honest_event}) = "
        f"({honest_count}, {honest_anchor})"
    )
    check(
        "a COMPLETED execution does consume a contact-cap slot",
        honest_count == 1,
        f"count is {honest_count}",
    )
    check(
        "and anchors the cooldown at its real send time",
        honest_anchor is not None
        and honest_anchor.isoformat().startswith(
            honest_record["executed_at"].replace("Z", "").replace("+00:00", "")[:19]
        ),
        f"{honest_anchor} vs executed_at {honest_record.get('executed_at')}",
    )

    status, failed_list = get("/executions?status=failed&history=true")
    check(
        "the failed attempt is visible in the record, not swallowed",
        status == 200
        and any(item["event_id"] == failkey_event for item in failed_list),
        f"HTTP {status}, {len(failed_list) if isinstance(failed_list, list) else '?'} failed record(s)",
    )

    schema_status, schema = get("/openapi.json")
    execute_spec = (
        schema.get("paths", {}).get("/execute/{event_id}", {}).get("post", {})
        if schema_status == 200
        else {}
    )
    query_parameters = [
        parameter["name"]
        for parameter in execute_spec.get("parameters", [])
        if parameter.get("in") == "query"
    ]
    check(
        "the execute endpoint declares no query parameters and no request body",
        not query_parameters and "requestBody" not in execute_spec,
        f"query={query_parameters}, body={'requestBody' in execute_spec}",
    )


async def main() -> None:
    print("Stage 5 adversarial tests — every case here is an attack that must fail")
    print(f"target {BASE}, run tag {TAG}")
    print(f"rulebook in force: {current_fingerprint()}")
    print(f"cooldown measured from: {rules.COOLDOWN_MEASURED_FROM}")

    test_baseline()
    test_outcome_smuggling()
    test_no_action_is_unconstructable()
    test_forgery_and_half_facts()
    test_type_level_gate()
    test_boundaries()

    reachable, _ = get("/executions")
    if reachable != 200:
        print(
            f"\nABORT: {BASE} is not answering (HTTP {reachable}). Fronts 5-7 need "
            "the server and the live database."
        )
        sys.exit(1)

    await connect_to_mongo()
    try:
        await test_live_fronts()
    finally:
        await close_mongo_connection()

    print("\n" + "=" * 78)
    print(f"{passed} attacks refused, {len(failed)} got through")
    if failed:
        for problem in failed:
            print(f"  - {problem}")
        sys.exit(1)
    print(
        "nothing executed without permission, no record claimed an outcome it cannot\n"
        "know, and a failed send cost the customer neither a contact nor a cooldown"
    )


if __name__ == "__main__":
    asyncio.run(main())
