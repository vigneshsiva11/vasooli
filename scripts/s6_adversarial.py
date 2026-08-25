"""Stage 6 adversarial suite — twenty ways this stage could be wrong.

Deliberately proportionate. Part A already has 63 live assertions in
`scripts/s6_verify.py` and Part B has 103 in `scripts/s6b_verify.py`; both of those
prove the happy paths and the guardrail paths work. This file only attacks the
things whose failure would be *silent* — the type that is supposed to be
unforgeable, the model that is supposed to reject impossible records, the state
machine that is supposed to refuse illegal moves, and the absence of any ungated
way to send a message.

THIS SCRIPT IS THE ATTACKER, AND IT CHEATS ON PURPOSE
----------------------------------------------------
Cases 9-11 import `app.ptp.safety._MINTED_BY_THE_CHECK` — the private sentinel that
makes `UnpaidConfirmation` unforgeable — and use it to build confirmations that
`confirm_still_unpaid` would never produce: one backdated ten minutes, one bound to
the wrong event. That is not a hole in the design being exploited quietly; it is the
point being demonstrated. Forging one requires reaching into a private module
attribute, which is a deliberate act rather than an oversight, and case 6 greps the
whole of `app/` to prove no production module does it. Then cases 10 and 11 show
that even a successfully forged token is refused by the freshness and event-binding
checks before the policy gate is reached.

WHAT NEEDS TO BE RUNNING
------------------------
MongoDB, for cases 10-12 and 19, which write and read real documents. The FastAPI
server, for the two events seeded over HTTP. Cases 1-8 and 13-18 and 20 are pure
in-process checks and would pass with nothing running at all.

Nothing here sends a contact. Case 12 calls the policy gate directly and lets it
write a verdict — that is the positive control which makes the two refusals above
it mean something — but `execute_event` is never called, so no message and no
payment link is produced by this script.
"""

from __future__ import annotations

import asyncio
import ast
import copy
import dataclasses
import inspect
import pickle
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from pydantic import ValidationError

import app.ptp as ptp_package
from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models.promise import (
    ALLOWED_PROMISE_STATES,
    ALLOWED_PROMISE_TRANSITIONS,
    INITIAL_PROMISE_STATE,
    OPEN_PROMISE_STATE,
    TERMINAL_PROMISE_STATES,
    PromiseRequest,
    PromiseToPay,
    promise_transition_allowed,
    states_that_may_become,
)
from app.policy import store as policy_store
from app.ptp import store
from app.ptp.safety import (
    MAX_CONFIRMATION_AGE_SECONDS,
    MismatchedConfirmation,
    StaleConfirmation,
    UnmintedConfirmation,
    UnpaidConfirmation,
    _MINTED_BY_THE_CHECK,  # THE CHEAT. See the module docstring.
    confirm_still_unpaid,
)
from app.ptp.service import send_follow_up
from app.routes.policy import authorize_event

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
TAG = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

passes: list[str] = []
failures: list[str] = []
touched: list[str] = []

#: Set on the two events this script seeds, so nothing it writes can be mistaken
#: for pipeline output.
SEEDED = [f"adv_{TAG}_X", f"adv_{TAG}_Y"]


def case(number: int, title: str) -> None:
    print(f"\n-- CASE {number:>2} — {title}")


def check(label: str, ok: bool, detail: str = "") -> bool:
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


def refuses(label: str, expected: type[BaseException], call) -> bool:
    """Assert a call raises, and raises the RIGHT thing.

    `expected` matters as much as the raising does: a `KeyError` where an
    `UnmintedConfirmation` was intended would still 'fail closed' today and stop
    doing so the moment the code around it changed.
    """
    try:
        result = call()
    except expected as exc:
        return check(label, True, str(exc)[:120])
    except BaseException as exc:  # noqa: BLE001 - reporting the wrong-reason case
        return check(
            label,
            False,
            f"raised {type(exc).__name__} rather than {expected.__name__}: {exc}"[:200],
        )
    return check(label, False, f"did not raise at all; returned {result!r}"[:200])


def python_sources() -> list[Path]:
    """Every production source file. Excludes `scripts/`, which is the attacker."""
    return sorted(
        path
        for path in (ROOT / "app").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def grep(needle: str) -> list[str]:
    """Every `path:line` in `app/` containing `needle`."""
    hits: list[str] = []
    for path in python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in line:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    return hits


def minted(event_id: str = "adv_event", *, age_seconds: float = 0.0) -> UnpaidConfirmation:
    """Forge a confirmation, using the private sentinel. THE CHEAT, deliberately."""
    return UnpaidConfirmation(
        event_id=event_id,
        checked_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        verifications_examined=0,
        _mint=_MINTED_BY_THE_CHECK,
    )


# ---------------------------------------------------------------------------
# Group 1 — the token that is supposed to be unforgeable.
# ---------------------------------------------------------------------------


def group_the_token() -> None:
    heading("THE UNFORGEABLE TOKEN — can a follow-up be reached without the check?")

    case(1, "an UnpaidConfirmation cannot be constructed directly")
    refuses(
        "constructing one with no mint raises UnmintedConfirmation",
        UnmintedConfirmation,
        lambda: UnpaidConfirmation(
            event_id="adv",
            checked_at=datetime.now(timezone.utc),
            verifications_examined=0,
        ),
    )

    case(2, "a forged sentinel is not the sentinel")
    refuses(
        "a look-alike object() is refused",
        UnmintedConfirmation,
        lambda: UnpaidConfirmation(
            event_id="adv",
            checked_at=datetime.now(timezone.utc),
            verifications_examined=0,
            _mint=object(),
        ),
    )
    refuses(
        "and so is a truthy stand-in like True",
        UnmintedConfirmation,
        lambda: UnpaidConfirmation(
            event_id="adv",
            checked_at=datetime.now(timezone.utc),
            verifications_examined=0,
            _mint=True,
        ),
    )
    note("identity, not truthiness: `_mint is not _MINTED_BY_THE_CHECK`")

    case(3, "a real confirmation cannot be re-targeted, and copying it launders nothing")
    real = minted("adv_event_one")
    refuses(
        "dataclasses.replace cannot rebind it to another event",
        UnmintedConfirmation,
        lambda: dataclasses.replace(real, event_id="adv_event_two"),
    )
    # copy/deepcopy/pickle DO succeed — they bypass __init__ — and that is reported
    # rather than hidden, because it is not a hole: a copy carries the original's
    # event id and timestamp, so it permits exactly what the original permitted.
    # The two properties that gate a send both survive the round trip.
    clones = {
        "copy.copy": copy.copy(real),
        "copy.deepcopy": copy.deepcopy(real),
        "pickle round trip": pickle.loads(pickle.dumps(real)),
    }
    check(
        "copy, deepcopy and pickle all preserve the event id and the timestamp",
        all(
            clone.event_id == real.event_id and clone.checked_at == real.checked_at
            for clone in clones.values()
        ),
        f"{[(name, c.event_id, c.checked_at.isoformat()) for name, c in clones.items()]}",
    )
    note(
        "they DO succeed — they bypass __init__ — but a clone is bound to the same "
        "event and dated the same moment, so it can neither be re-aimed nor refreshed"
    )

    case(4, "there is no second constructor")
    check(
        "not a Pydantic model, so no model_validate to take a dict",
        not hasattr(UnpaidConfirmation, "model_validate")
        and not hasattr(UnpaidConfirmation, "model_construct"),
        f"model_validate={hasattr(UnpaidConfirmation, 'model_validate')} "
        f"model_construct={hasattr(UnpaidConfirmation, 'model_construct')}",
    )
    check(
        "and no from_dict / parse_obj style classmethod either",
        not any(
            hasattr(UnpaidConfirmation, name)
            for name in ("from_dict", "parse_obj", "from_document", "construct")
        ),
    )

    case(5, "frozen, and the mint is not left lying around on the instance")
    def mutate() -> None:
        real.event_id = "somebody_else"  # type: ignore[misc]

    refuses(
        "assigning to a field raises FrozenInstanceError",
        dataclasses.FrozenInstanceError,
        mutate,
    )
    field_names = [f.name for f in dataclasses.fields(real)]
    check(
        "_mint is an InitVar, so it is not a field and is not stored on the instance",
        "_mint" not in field_names and "_mint" not in real.__dict__,
        f"fields={field_names} instance __dict__ keys={sorted(real.__dict__)}",
    )
    # `hasattr(real, "_mint")` is True, and that is worth stating rather than
    # asserting away: declaring the InitVar with a default leaves `_mint = None` as a
    # CLASS attribute, so the name resolves. What matters is what it resolves TO. A
    # holder of a legitimate confirmation cannot read the sentinel back off it and
    # use it to mint more — the attribute is the default None, not the capability.
    check(
        "and reading `_mint` off a real confirmation yields None, not the sentinel — "
        "holding one does not let you mint another",
        getattr(real, "_mint", None) is not _MINTED_BY_THE_CHECK
        and getattr(real, "_mint", "absent") is None,
        f"getattr(confirmation, '_mint') = {getattr(real, '_mint', 'ABSENT')!r}",
    )
    check(
        "and it is not in the repr, so logging one cannot leak the capability",
        "_mint" not in repr(real),
        repr(real)[:160],
    )

    case(6, "nothing in app/ mints a confirmation outside app/ptp/safety.py")
    sentinel_hits = grep("_MINTED_BY_THE_CHECK")
    stray_sentinel = [
        hit
        for hit in sentinel_hits
        if not hit.startswith("app/ptp/safety.py")
        and not hit.startswith("app/ptp/__init__.py")
    ]
    check(
        "the sentinel appears only in safety.py (and __init__'s note about it)",
        not stray_sentinel,
        f"stray: {stray_sentinel}",
    )
    note(f"sentinel referenced at: {', '.join(sentinel_hits)}")
    construction_hits = grep("UnpaidConfirmation(")
    check(
        "and 'UnpaidConfirmation(' appears at exactly one construction site",
        construction_hits == ["app/ptp/safety.py:215"]
        or (
            len(construction_hits) == 1
            and construction_hits[0].startswith("app/ptp/safety.py")
        ),
        f"sites: {construction_hits}",
    )
    note(f"constructed at: {', '.join(construction_hits)}")

    case(7, "nothing in app/ constructs a VerifiedWebhook outside the verifier")
    # The grep `app/webhooks/signature.py`'s own docstring promises. A second
    # construction site would be a route into reconciliation that skipped the HMAC
    # check while still satisfying the type that is supposed to prove it happened.
    webhook_hits = grep("VerifiedWebhook(")
    check(
        "'VerifiedWebhook(' appears at exactly one construction site",
        len(webhook_hits) == 1
        and webhook_hits[0].startswith("app/webhooks/signature.py"),
        f"sites: {webhook_hits}",
    )
    note(f"constructed at: {', '.join(webhook_hits)}")

    case(8, "the sentinel is not re-exported from the package")
    check(
        "app.ptp does not expose _MINTED_BY_THE_CHECK",
        not hasattr(ptp_package, "_MINTED_BY_THE_CHECK"),
    )
    check(
        "and it is not in app.ptp.__all__",
        "_MINTED_BY_THE_CHECK" not in ptp_package.__all__,
    )
    note(
        "app.ptp.safety._MINTED_BY_THE_CHECK is still reachable, as this script "
        "proves; the claim is that reaching it is a private-attribute access a grep "
        "finds, not that Python can forbid it"
    )


# ---------------------------------------------------------------------------
# Group 2 — freshness and binding, against the real sender.
# ---------------------------------------------------------------------------


async def group_the_sender() -> None:
    heading("THE SENDER — is a forged or expired token enough to reach the gate?")

    event_x, event_y = SEEDED

    case(9, "a confirmation expires, and expiry cannot be laundered")
    fresh = minted(event_x, age_seconds=0.0)
    fresh.assert_fresh()
    check("a just-minted confirmation is fresh", True, "assert_fresh() returned")
    stale = minted(event_x, age_seconds=MAX_CONFIRMATION_AGE_SECONDS + 600)
    refuses(
        f"one {MAX_CONFIRMATION_AGE_SECONDS + 600:.0f}s old is refused by assert_fresh",
        StaleConfirmation,
        stale.assert_fresh,
    )
    check(
        "its reported age is real, not nominal",
        stale.age_seconds > MAX_CONFIRMATION_AGE_SECONDS,
        f"age_seconds={stale.age_seconds:.1f}, limit={MAX_CONFIRMATION_AGE_SECONDS}",
    )
    refuses(
        "and a clone of it is just as stale — copying does not refresh the clock",
        StaleConfirmation,
        copy.deepcopy(stale).assert_fresh,
    )

    verdicts_before = await policy_store.collection().count_documents(
        {"event_id": event_y}
    )
    executions_before = await get_database()["executions"].count_documents(
        {"event_id": event_y}
    )

    case(10, "send_follow_up refuses a stale confirmation BEFORE the policy gate runs")
    await refuses_async(
        "a stale confirmation cannot send",
        StaleConfirmation,
        lambda: send_follow_up(minted(event_y, age_seconds=600), event_id=event_y),
    )
    check(
        "and no policy verdict was written — the gate was never reached",
        await policy_store.collection().count_documents({"event_id": event_y})
        == verdicts_before,
        f"verdicts for {event_y}: {verdicts_before} -> "
        f"{await policy_store.collection().count_documents({'event_id': event_y})}",
    )

    case(11, "send_follow_up refuses a confirmation minted for another event")
    await refuses_async(
        "a confirmation for X cannot authorise a follow-up to Y",
        MismatchedConfirmation,
        lambda: send_follow_up(minted(event_x), event_id=event_y),
    )
    check(
        "again no verdict, so the mismatch was caught before the gate",
        await policy_store.collection().count_documents({"event_id": event_y})
        == verdicts_before,
        f"verdicts for {event_y}: {verdicts_before} -> "
        f"{await policy_store.collection().count_documents({'event_id': event_y})}",
    )
    check(
        "and nothing executed on either refusal",
        await get_database()["executions"].count_documents({"event_id": event_y})
        == executions_before,
        f"executions for {event_y}: {executions_before} -> "
        f"{await get_database()['executions'].count_documents({'event_id': event_y})}",
    )

    case(12, "POSITIVE CONTROL — the gate those two refusals stopped short of is live")
    # Without this, cases 10 and 11 prove only that nothing happened, which an
    # unreachable gate would also satisfy. Calling it directly writes a verdict and
    # nothing else: `authorize_event` never executes anything, so this control
    # cannot send a message.
    verdict = await authorize_event(event_y)
    verdicts_after = await policy_store.collection().count_documents(
        {"event_id": event_y}
    )
    check(
        "authorizing the same event DOES write a verdict",
        verdicts_after == verdicts_before + 1,
        f"{verdicts_before} -> {verdicts_after}",
    )
    note(f"verdict v{verdict.version}: {verdict.verdict}/{verdict.reason}")
    check(
        "so the two refusals above stopped a gate that was genuinely reachable",
        verdicts_after > verdicts_before,
    )
    check(
        "and even the control executed nothing — authorize is not execute",
        await get_database()["executions"].count_documents({"event_id": event_y})
        == executions_before,
    )
    touched.append(f"policy_verdicts: v{verdict.version} for {event_y} (case 12 control)")

    case(13, "send_follow_up cannot be called without a confirmation")
    signature = inspect.signature(send_follow_up)
    parameters = list(signature.parameters.values())
    first = parameters[0]
    check(
        "its first parameter is positional",
        first.kind
        in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD),
        f"{first.name} is {first.kind}",
    )
    check(
        "it has no default, so it cannot be omitted",
        first.default is inspect.Parameter.empty,
        f"default={first.default!r}",
    )
    check(
        "and it is annotated UnpaidConfirmation",
        "UnpaidConfirmation" in str(first.annotation),
        f"annotation={first.annotation!r}",
    )
    check(
        "every other parameter is keyword-only, so nothing can be passed by mistake "
        "in the confirmation's place",
        all(p.kind is p.KEYWORD_ONLY for p in parameters[1:]),
        f"{[(p.name, str(p.kind)) for p in parameters[1:]]}",
    )
    await refuses_async(
        "calling it with no confirmation is a TypeError, not a send",
        TypeError,
        lambda: send_follow_up(event_id=event_y),  # type: ignore[call-arg]
    )

    case(14, "the follow-up path reuses the policy gate rather than reassembling it")
    source = (ROOT / "app" / "ptp" / "service.py").read_text(encoding="utf-8")
    check(
        "app/ptp/service.py calls authorize_event and execute_event",
        "authorize_event(" in source and "execute_event(" in source,
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    reassembly = sorted(
        module
        for module in imported
        if module.startswith("app.policy") or module.startswith("app.execution")
    )
    check(
        "and imports nothing from app.policy or app.execution directly",
        not reassembly,
        f"direct imports that would allow a second assembly of the gate: {reassembly}",
    )
    note(f"its route into those stages: {sorted(m for m in imported if 'routes' in m)}")


async def refuses_async(label: str, expected: type[BaseException], call) -> bool:
    """`refuses`, for an awaitable."""
    try:
        result = await call()
    except expected as exc:
        return check(label, True, str(exc)[:120])
    except BaseException as exc:  # noqa: BLE001
        return check(
            label,
            False,
            f"raised {type(exc).__name__} rather than {expected.__name__}: {exc}"[:200],
        )
    return check(label, False, f"did not raise at all; returned {result!r}"[:200])


# ---------------------------------------------------------------------------
# Group 3 — the model, and states it must refuse to represent.
# ---------------------------------------------------------------------------


def group_the_model() -> None:
    heading("THE MODEL — can an impossible promise be constructed at all?")

    now = datetime.now(timezone.utc)
    valid = {
        "event_id": "adv",
        "promised_amount": 100.0,
        "promised_date": date(2026, 9, 1),
    }

    case(15, "a promise cannot be constructed in a state that contradicts itself")
    refuses(
        "follow_up_sent=True while still 'promised' is refused",
        ValidationError,
        lambda: PromiseToPay(**valid, follow_up_sent=True),
    )
    refuses(
        "resolved_at set while still 'promised' is refused",
        ValidationError,
        lambda: PromiseToPay(**valid, resolved_at=now),
    )
    refuses(
        "'honored' with no resolved_at is refused",
        ValidationError,
        lambda: PromiseToPay(**valid, state="honored"),
    )
    refuses(
        "'reevaluating' with follow_up_sent=False is refused",
        ValidationError,
        lambda: PromiseToPay(
            **valid, state="reevaluating", resolved_at=now, follow_up_sent=False
        ),
    )
    check(
        "while the legitimate combination — honored after being chased — is allowed",
        PromiseToPay(
            **valid, state="honored", resolved_at=now, follow_up_sent=True
        ).state
        == "honored",
    )
    note("a customer who paid after a reminder is a real record and must be storable")

    case(16, "execution-shaped fields cannot be smuggled onto a promise")
    for field, value in [
        ("razorpay_payment_link_id", "plink_forged"),
        ("razorpay_payment_link_url", "https://rzp.io/rzp/forged"),
        ("intervention", "reminder"),
        ("action_type", "contact_logged"),
        ("message", "please pay"),
        ("policy_verdict_id", "deadbeef"),
    ]:
        refuses(
            f"extra='forbid' rejects {field!r}",
            ValidationError,
            lambda field=field, value=value: PromiseToPay(**valid, **{field: value}),
        )
    note(
        "a promise is a statement of intent; what was DONE about one is an "
        "ExecutionRecord, written under a verdict — merging them would create a "
        "place to record a message as sent with nothing having authorized it"
    )

    case(17, "the scalars are constrained too")
    refuses(
        "a zero amount is refused",
        ValidationError,
        lambda: PromiseToPay(**{**valid, "promised_amount": 0.0}),
    )
    refuses(
        "a negative amount is refused",
        ValidationError,
        lambda: PromiseToPay(**{**valid, "promised_amount": -1.0}),
    )
    refuses(
        "an empty event_id is refused",
        ValidationError,
        lambda: PromiseToPay(**{**valid, "event_id": ""}),
    )
    refuses(
        "a state outside the vocabulary is refused",
        ValidationError,
        lambda: PromiseToPay(**valid, state="pending"),
    )
    refuses(
        "a naive created_at is refused",
        ValidationError,
        lambda: PromiseToPay(**valid, created_at=datetime(2026, 8, 1)),
    )
    check(
        "and the amount is rounded rather than stored as arithmetic produced it",
        PromiseToPay(**{**valid, "promised_amount": 1200.0000000000002}).promised_amount
        == 1200.0,
    )

    case(18, "the transition table is closed, and honored is the end of it")
    check(
        "every state in the table is a declared PromiseState",
        set(ALLOWED_PROMISE_TRANSITIONS) == ALLOWED_PROMISE_STATES,
        f"{sorted(ALLOWED_PROMISE_TRANSITIONS)} vs {sorted(ALLOWED_PROMISE_STATES)}",
    )
    check(
        "'honored' is terminal and is the only terminal state",
        TERMINAL_PROMISE_STATES == frozenset({"honored"}),
        f"{sorted(TERMINAL_PROMISE_STATES)}",
    )
    check(
        "nothing may move back to 'promised' — a date does not move, a new promise "
        "is a new document",
        states_that_may_become(INITIAL_PROMISE_STATE) == frozenset(),
        f"states that may become 'promised': {sorted(states_that_may_become(INITIAL_PROMISE_STATE))}",
    )
    check(
        "no state may transition to itself",
        not any(
            promise_transition_allowed(state, state) for state in ALLOWED_PROMISE_STATES
        ),
    )
    check(
        "an unknown stored state may move nowhere, rather than raising",
        promise_transition_allowed("legacy_value", "honored") is False,
    )
    # The inversion the guarded query relies on, checked against the table itself.
    inverted_ok = all(
        (current in states_that_may_become(target))
        == promise_transition_allowed(current, target)
        for current in ALLOWED_PROMISE_STATES | {"legacy_value"}
        for target in ALLOWED_PROMISE_STATES
    )
    check(
        "states_that_may_become inverts the table exactly, for every pair",
        inverted_ok,
        "the Mongo filter is built from this inversion, so a disagreement here is a "
        "guard that permits an illegal move",
    )
    illegal = sorted(
        (current, target)
        for current in ALLOWED_PROMISE_STATES
        for target in ALLOWED_PROMISE_STATES
        if not promise_transition_allowed(current, target)
    )
    note(f"{len(illegal)} of 16 ordered pairs are illegal, including {illegal[:4]}")


# ---------------------------------------------------------------------------
# Group 4 — the live enforcement, and the absence of a back door.
# ---------------------------------------------------------------------------


async def group_enforcement() -> None:
    heading("LIVE ENFORCEMENT — does the database refuse what the table forbids?")

    event_x = SEEDED[0]
    now = datetime.now(timezone.utc)

    case(19, "an illegal transition matches zero documents rather than being corrected")
    promise = PromiseToPay(
        event_id=event_x,
        promised_amount=250.0,
        promised_date=date(2026, 12, 31),
    )
    promise_id = await store.insert(promise)
    touched.append(f"promises: {promise_id} ({event_x}, case 19)")
    check("a promise was inserted in the initial state", True, f"id={promise_id}")

    # promised -> reevaluating is not in the table. `broken` is the only route there.
    refused = await store.apply_transition(
        promise_id=promise_id, target="reevaluating", resolved_at=now, follow_up_sent=True
    )
    check(
        "'promised' -> 'reevaluating' writes nothing",
        not refused.changed and refused.refused,
        f"changed={refused.changed} refused={refused.refused} current={refused.current!r}",
    )
    note(refused.detail)
    stored = await store.find_by_id(promise_id)
    assert stored is not None
    check(
        "and the document is untouched — not partially updated",
        stored["state"] == OPEN_PROMISE_STATE
        and stored["resolved_at"] is None
        and stored["follow_up_sent"] is False,
        f"state={stored['state']!r} resolved_at={stored['resolved_at']!r} "
        f"follow_up_sent={stored['follow_up_sent']!r}",
    )

    legal = await store.apply_transition(
        promise_id=promise_id, target="honored", resolved_at=now
    )
    check(
        "the legal move on the same document DOES apply",
        legal.changed,
        f"changed={legal.changed} current={legal.current!r}",
    )

    from_terminal = await store.apply_transition(
        promise_id=promise_id, target="broken", resolved_at=now
    )
    check(
        "and nothing moves out of 'honored' afterwards",
        not from_terminal.changed and from_terminal.refused,
        f"changed={from_terminal.changed} current={from_terminal.current!r}",
    )
    note(from_terminal.detail)

    self_move = await store.apply_transition(
        promise_id=promise_id, target="honored", resolved_at=now
    )
    check(
        "a self-transition is reported as no change, not as a success",
        not self_move.changed and not self_move.refused,
        f"changed={self_move.changed} refused={self_move.refused}",
    )
    note(self_move.detail)

    final = await store.find_by_id(promise_id)
    assert final is not None
    check(
        "the promise ends 'honored' — one legal move applied out of four attempts",
        final["state"] == "honored",
        f"state={final['state']!r}",
    )

    case(20, "there is no ungated way to send, and no way to set a state by hand")
    from app.main import app as fastapi_app

    paths = fastapi_app.openapi()["paths"]
    promise_paths = {path: sorted(methods) for path, methods in paths.items() if "promise" in path}
    check(
        "no follow-up endpoint exists",
        not any(
            "follow" in path.lower() or "remind" in path.lower() or "contact" in path.lower()
            for path in paths
        ),
        f"suspicious paths: {[p for p in paths if 'follow' in p.lower()]}",
    )
    check(
        "no PATCH or PUT on any promise path, so no state can be edited",
        not any(
            method in methods
            for methods in promise_paths.values()
            for method in ("patch", "put", "delete")
        ),
        f"{promise_paths}",
    )
    check(
        "PromiseRequest has no state field, so a promise cannot be born honored",
        "state" not in PromiseRequest.model_fields,
        f"request fields: {sorted(PromiseRequest.model_fields)}",
    )
    check(
        "nor a follow_up_sent or resolved_at field",
        not {"follow_up_sent", "resolved_at"} & set(PromiseRequest.model_fields),
    )
    note(f"the whole promise surface: {promise_paths}")


# ---------------------------------------------------------------------------
# Setup and entry point.
# ---------------------------------------------------------------------------


async def seed(client: httpx.AsyncClient, event_id: str) -> bool:
    """Ingest -> diagnose -> decide, so the policy gate has something to evaluate.

    A contact-type fixture: `receivable/genuine_delay` at 900 resolves to
    `escalating_reminder_sequence`, which is in CONTACT_INTERVENTIONS. Case 12's
    control needs a decision that policy will actually authorize, otherwise the
    control would prove nothing about reachability.
    """
    response = await client.post(
        f"{API}/events",
        json={
            "event_id": event_id,
            "surface": "receivable",
            "amount": 900.00,
            "currency": "INR",
            "customer_ref": f"cust_{event_id}",
            "raw_failure_reason": "cash flow delay, will pay next week",
        },
        timeout=60,
    )
    if response.status_code >= 400:
        return check(f"seed {event_id}", False, f"{response.status_code} {response.text[:200]}")
    for step in ("diagnose", "decide"):
        stepped = await client.post(f"{API}/{step}/{event_id}", timeout=120)
        if stepped.status_code >= 400:
            return check(
                f"seed {event_id}", False, f"{step} {stepped.status_code} {stepped.text[:200]}"
            )
    touched.append(f"events: {event_id} (seeded, at_risk)")
    return True


async def main() -> int:
    print(f"Stage 6 adversarial suite — against {API}")
    print(f"run tag {TAG}")
    print(
        "\nThis script imports the private mint sentinel on purpose and forges\n"
        "confirmations with it. That is the demonstration, not a bypass: case 6\n"
        "proves no production module does the same, and cases 10-11 show a forged\n"
        "token is still refused before the policy gate."
    )

    # Cases 1-8 need nothing running at all.
    group_the_token()

    await connect_to_mongo()
    try:
        async with httpx.AsyncClient() as client:
            heading("SETUP — two events, seeded to a decision")
            try:
                ok = all([await seed(client, SEEDED[0]), await seed(client, SEEDED[1])])
            except httpx.HTTPError as exc:
                # Reported as a failed check rather than a traceback: a dead server is
                # a reason the live cases could not run, and it must not be mistaken
                # for a safety property that did not hold.
                ok = check(
                    "the API is reachable for setup",
                    False,
                    f"{type(exc).__name__}: {exc}. Start the server, then re-run — "
                    "cases 9-14 and 19-20 need it.",
                )
            check("both events seeded to a decision", ok)

        if ok:
            await group_the_sender()

        # Cases 15-18 are pure in-process model checks, run whether or not the live
        # setup worked, so a dead server still gets a full model report.
        group_the_model()

        if ok:
            await group_enforcement()
        else:
            heading("SKIPPED — cases 9-14 and 19-20")
            print(
                "  The live cases did not run because setup failed above.\n"
                "  They are NOT passes and are not counted as any."
            )
    finally:
        await close_mongo_connection()

    heading("SUMMARY")
    print(f"  {len(passes)} passed, {len(failures)} failed")
    for failure in failures:
        print(f"    FAILED: {failure}")

    print("\n  Real state this run changed:")
    for item in touched:
        print(f"    - {item}")
    print(
        "\n  Nothing was sent. No contact was logged and no payment link was created:\n"
        "  the only write outside the two seeded events and one promise is case 12's\n"
        "  policy verdict, and evaluating policy is not executing anything."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
