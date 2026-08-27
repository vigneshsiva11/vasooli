"""Stage 8 — test the revised ambiguous strings against the real Gemini path.

Ratification support, not pipeline work. Before re-posting revised `raw_failure_reason`
text into the database, this asks the actual model the actual question the pipeline
would ask: `gemini.propose_diagnosis` with the pipeline's own prompt builder, model,
temperature and response schema. Nothing is written anywhere — no database handle is
opened, no event is ingested, no diagnosis record is stored.

Two things are being checked, and only one of them is a pass/fail:

  * PASS/FAIL — an entry the dataset marks answerable must come back above the 0.5
    decision confidence floor, otherwise it becomes `no_action_low_confidence` and the
    LLM path contributes nothing to the demo. An entry marked unanswerable must come
    back BELOW the floor, because that is the guardrail it exists to show.
  * REPORTED, NOT ASSERTED — whether the root cause matches the one a human analyst
    would reach. A disagreement is printed as a disagreement. The model's
    classification is the model's; overruling it here would be scoring my own guess.

`prior_event_count` is 0, matching the demo batch where nearly every customer
reference is distinct.

Usage:
    .venv/Scripts/python.exe scripts/s8_ambiguity_probe.py
    .venv/Scripts/python.exe scripts/s8_ambiguity_probe.py --only payment
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

from app.diagnosis import gemini  # noqa: E402
from app.models.decision import CONFIDENCE_FLOOR  # noqa: E402

#: Answers already measured against the live model, recorded so a re-run to review
#: them costs no quota. Every one of these was produced by the same call this script
#: makes — `gemini.propose_diagnosis` at temperature 0.0 — on the dates noted below.
#: An entry here is replayed, not re-asked, and is labelled as such in the output.
#:
#: The two receivable/payment entries marked "checkpoint 1" came out of the live
#: checkpoint-1 ingest. The rest were measured by this script on 2026-08-26, at which
#: point I truncated my own capture of the output and lost two payment results — hence
#: this table, so the two could be re-asked without re-asking the eleven.
ALREADY_MEASURED = {
    # checkpoint 1, via the real ingest path (demo_009_pay)
    "Customer says the bank sent them a message about the transaction but the money "
    "never left their account.": ("unknown", 0.35),
    # checkpoint 1, probed directly
    "Their accounts team says the purchase order number on our side does not match "
    "what procurement raised, so it is sitting with someone for sign-off.": (
        "genuine_delay",
        0.90,
    ),
    # measured by this script, 2026-08-26
    "The issuing side sent back a permanent instruction not to attempt this "
    "instrument again until the cardholder contacts them.": ("issuer_declined", 0.92),
    "Our processor was mid-deployment when this went through and the acknowledgement "
    "never came back; the next attempt on the same rail worked.": (
        "temporary_processing_error",
        0.95,
    ),
    "Repeat failure on the same instrument, no code returned by the processor.": (
        "unknown",
        0.40,
    ),
    "The amount was held against their balance and then released without settling; "
    "they said the account is close to its limit this week.": (
        "insufficient_funds",
        0.88,
    ),
    "They filled in delivery details three times because the page kept resetting the "
    "state field, then gave up.": ("checkout_friction", 0.90),
    "On mobile the confirm button did nothing on two separate visits; the same cart "
    "completed fine on desktop.": ("technical_error", 0.90),
    "They wanted to pay by bank transfer from their corporate account and we only "
    "offer cards at this checkout.": ("payment_method_unavailable", 0.95),
    "The standing instruction we hold for this customer reached its end date before "
    "this cycle was raised.": ("mandate_expired", 0.95),
    "Third cycle in a row where their account did not hold the amount on the debit "
    "date; each earlier cycle cleared a day or two afterwards.": (
        "insufficient_funds",
        0.92,
    ),
    "Vendor portal shows the invoice as received but our contact changed roles, "
    "nobody has been assigned to it since, and four messages to the shared AP inbox "
    "have gone unanswered.": ("non_responsive", 0.90),
    "Their finance controller signed it off but the payment run only executes on the "
    "25th, so it sits until then.": ("genuine_delay", 0.92),
    # measured 2026-08-26 on gemini-3.5-flash-lite, NOT gemini-3.6-flash. The
    # 3.6-flash free-tier daily bucket was exhausted (20 requests/day/model) before
    # these two were reached, and the 429 quota id is per-model, so a sibling model
    # had an untouched bucket. Recorded with the model named because a measurement
    # from a different model is a different measurement, even when the answer agrees.
    "Their account balance was short of the transaction value when we attempted it, "
    "and they mentioned salary credits land on the 3rd.": ("insufficient_funds", 0.95),
    "The saved instrument on file reached the end of its validity last month and they "
    "have not added a new one.": ("card_expired", 0.95),
}

#: A representative amount per surface. The prompt includes the amount, so it cannot
#: be omitted; these are mid-band figures, not the amounts any specific event carries.
PROBE_AMOUNT = {
    "payment": 2450.75,
    "checkout": 1899.50,
    "subscription": 799.25,
    "receivable": 48250.40,
}

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


async def run(only: str | None) -> int:
    surfaces = [only] if only else ["payment", "checkout", "subscription", "receivable"]

    print("=" * 78)
    print("REVISED AMBIGUOUS STRINGS — asked of the real model, nothing written")
    print("=" * 78)
    print(f"  model {gemini.get_settings().gemini_model}   "
          f"timeout {gemini.get_settings().gemini_timeout_seconds}s   "
          f"temperature 0.0   decision confidence floor {CONFIDENCE_FLOOR}")

    calls = 0
    failures: list[str] = []
    call_failures: list[str] = []
    disagreements: list[str] = []
    results: list[tuple[str, str | None, str | None, str, float, list[str], str]] = []

    for surface in surfaces:
        for text, expected in ds.AMBIGUOUS[surface]:
            if text is None:
                print()
                print("-" * 78)
                print(f"  {surface}   (no failure text supplied)")
                print("-" * 78)
                print(
                    "  CORRECTION to what this script said in its first version: I\n"
                    "  wrote that the pipeline sends no prompt for an event with no\n"
                    "  reason text. It does. `service.py:135` calls propose_diagnosis\n"
                    "  unconditionally once the rules path declines, and\n"
                    "  `build_prompt` renders the reason block as '(none supplied)'\n"
                    "  (gemini.py:224). So this event spends a Gemini call like any\n"
                    "  other LLM-path event, and is asked below like the rest."
                )

            if text in ALREADY_MEASURED:
                cause, confidence = ALREADY_MEASURED[text]
                source = "recorded measurement, not re-asked"
                evidence = ["(evidence printed when this string was first measured)"]
            else:
                started = time.monotonic()
                try:
                    proposal, _raw = await gemini.propose_diagnosis(
                        surface=surface,
                        amount=PROBE_AMOUNT[surface],
                        currency="INR",
                        raw_failure_reason=text,
                        prior_event_count=0,
                    )
                except gemini.GeminiUnavailable as exc:
                    calls += 1
                    call_failures.append(f"{surface}: {exc}")
                    print()
                    print("-" * 78)
                    print(f"  {surface}   [CALL FAILED after "
                          f"{time.monotonic() - started:.1f}s]")
                    print("-" * 78)
                    print(f"  {text!r}" if text is not None else "  (no failure text)")
                    print(f"  -> {exc}")
                    results.append((surface, text, expected, "?", -1.0, [], "failed"))
                    continue
                calls += 1
                cause = proposal.root_cause
                confidence = proposal.confidence
                evidence = list(proposal.evidence)
                source = f"live call, {time.monotonic() - started:.1f}s"

            answerable = expected is not None
            clears = confidence >= CONFIDENCE_FLOOR
            print()
            print("-" * 78)
            print(
                f"  {surface}   "
                f"{'ANSWERABLE' if answerable else 'UNANSWERABLE ON PURPOSE'}   "
                f"({source})"
            )
            print("-" * 78)
            print(f"  {text!r}" if text is not None else "  (no failure text)")
            print(f"  -> root_cause  {cause}"
                  f"   (a human analyst would say: {expected or 'nothing defensible'})")
            print(f"     confidence  {confidence}"
                  f"   {'clears' if clears else 'BELOW'} the {CONFIDENCE_FLOOR} floor")
            for item in evidence:
                print(f"       - {item}")
            results.append(
                (surface, text, expected, cause, confidence, evidence, source)
            )

            if answerable and not clears:
                failures.append(
                    f"{surface}: marked answerable but returned {confidence} "
                    f"(<{CONFIDENCE_FLOOR}) — would become no_action_low_confidence"
                )
            if not answerable and clears:
                failures.append(
                    f"{surface}: marked unanswerable but returned {cause} at "
                    f"{confidence} — the guardrail it exists to show would not fire"
                )
            if answerable and cause != expected:
                disagreements.append(
                    f"{surface}: analyst said {expected}, model said {cause} "
                    f"@ {confidence}"
                )

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    answerable_total = sum(1 for _s, _t, e, *_rest in results if e is not None)
    unanswerable_total = sum(1 for _s, _t, e, *_rest in results if e is None)
    print(f"  {calls} live Gemini calls spent")
    print(f"  {answerable_total} entries marked answerable, "
          f"{unanswerable_total} deliberately unanswerable "
          f"(one of them carries no failure text at all)")
    check(
        "every answerable string clears the decision confidence floor",
        not any("marked answerable" in f for f in failures),
        "\n           ".join(f for f in failures if "marked answerable" in f),
    )
    check(
        "every deliberately-unanswerable string stays below the floor",
        not any("marked unanswerable" in f for f in failures),
        "\n           ".join(f for f in failures if "marked unanswerable" in f),
    )
    check(
        "no call failed",
        not call_failures,
        "\n           ".join(call_failures) if call_failures
        else f"{calls} calls, all answered",
    )

    print()
    if disagreements:
        print("  REPORTED, NOT FAILED — the model reached a different cause than I")
        print("  would have. Judge these on the evidence it gave, above:")
        for line in disagreements:
            print(f"    - {line}")
    else:
        print("  The model reached the same root cause as a human analyst on every")
        print("  answerable string. Not asserted — it simply happened to agree.")

    print()
    print(f"  {PASSED} passed, {FAILED} failed")
    print("  Nothing was written. No event was ingested, no diagnosis stored.")
    return 0 if FAILED == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, choices=list(PROBE_AMOUNT))
    args = parser.parse_args()
    return asyncio.run(run(args.only))


if __name__ == "__main__":
    raise SystemExit(main())
