"""Stage 8 Part B.2 (completion) — diagnose the LLM-path demo events.

The rules-path events were diagnosed with `--skip-llm` and are done. This script
finishes the batch: it runs the events the routing gate deliberately could not
classify through the real `POST /diagnose/{event_id}` endpoint, so Gemini answers
them via the pipeline's own prompt, model and schema. No store module is imported
for writing and no diagnosis is constructed here — if the route is broken, this
script has no way around it.

Three things make this safe to run against a near-exhausted free-tier quota:

* The event list is DERIVED from the dataset's `_expects_llm` flag, then
  cross-checked against the list the user named. A hand-typed list that drifted
  from the data would be caught before a single call is spent.
* Calls are strictly sequential and the run ABORTS on the first `fallback`
  method. A fallback means the call itself failed (quota, timeout, transport),
  and continuing would write a stack of junk "unknown" records over good events.
* Every result is printed as it arrives, so a mid-run abort still leaves a full
  record of what was spent and what came back.

The expected root cause is REPORTED, never asserted. `_llm_expected_cause` is what
a careful analyst reads out of the ambiguous string; the model's classification is
the model's. Only two things here are pass/fail, and neither is agreement:

* an answerable string must clear the decision stage's `CONFIDENCE_FLOOR`, or the
  event is dropped as `no_action_low_confidence` no matter how right the cause is;
* a deliberately unanswerable string must stay BELOW that floor and come back
  `unknown` — the guardrail case, where being confidently wrong is the failure.

Usage:
    .venv/Scripts/python.exe scripts/s8_llm_diagnose.py
    .venv/Scripts/python.exe scripts/s8_llm_diagnose.py --only demo_029_pay
    .venv/Scripts/python.exe scripts/s8_llm_diagnose.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.models import ALLOWED_ROOT_CAUSES  # noqa: E402
from app.models.decision import CONFIDENCE_FLOOR  # noqa: E402

#: The events the user named, for cross-checking the derived list. Written out
#: rather than computed so that a drift between "what the data says needs an LLM"
#: and "what was authorised to be spent on" fails loudly instead of silently
#: spending quota on an event nobody approved.
USER_NAMED = [
    "demo_009_pay",  # re-diagnosed so its record names a model, not just `llm`
    "demo_029_pay",  # text was revised after its v2 stub; that stub is stale
    "demo_062_pay",  # measured in the probe; re-confirmed through the real route
    "demo_072_pay",
    "demo_073_pay",
    "demo_083_pay",
    "demo_087_pay",
    "demo_105_chk",
    "demo_109_chk",
    "demo_116_chk",
    "demo_118_chk",
    "demo_163_sub",
    "demo_167_sub",
    "demo_178_rcv",
    "demo_180_rcv",
    "demo_190_rcv",
]

PASSED = 0
FAILED = 0


def heading(text: str) -> None:
    print()
    print("=" * 100)
    print(text)
    print("=" * 100)


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="restrict to specific event ids (repeatable). Spends one call each.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print the call list, spend nothing",
    )
    args = parser.parse_args()

    specs = {s["event_id"]: s for s in ds.generate()}
    derived = [eid for eid, s in specs.items() if s["_expects_llm"]]

    heading("CALL LIST — derived from the dataset, cross-checked against the named list")
    print(f"  events flagged `_expects_llm` in the dataset : {len(derived)}")
    print(f"  events named by the user                    : {len(USER_NAMED)}")
    only_derived = sorted(set(derived) - set(USER_NAMED))
    only_named = sorted(set(USER_NAMED) - set(derived))
    check(
        "the derived LLM-path set and the named set are identical",
        not only_derived and not only_named,
        f"only in dataset: {only_derived or 'none'};  only in named list: {only_named or 'none'}",
    )
    if only_derived or only_named:
        print("\n  ABORTING before any call. The list under test does not match the data.")
        return 1

    targets = derived if not args.only else [e for e in derived if e in set(args.only)]
    if args.only:
        unknown = sorted(set(args.only) - set(derived))
        if unknown:
            print(f"\n  ABORTING: --only named non-LLM-path events: {unknown}")
            return 1
    print(f"\n  {len(targets)} calls will be spent, sequentially:")
    for eid in targets:
        s = specs[eid]
        expect = s["_llm_expected_cause"] or "unanswerable -> expect `unknown`"
        print(f"    {eid:<14} {s['surface']:<13} {expect}")

    if args.dry_run:
        print("\n  --dry-run: nothing spent.")
        return 0

    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=180.0)
    health = http.get("/").json()
    print(
        f"\n  server {args.base}   database={health.get('database')}   "
        f"gemini={health.get('gemini')}"
    )
    print(f"  decision-stage CONFIDENCE_FLOOR = {CONFIDENCE_FLOOR}")

    # =====================================================================
    heading("DIAGNOSE — POST /diagnose/{event_id}, one at a time")
    # =====================================================================
    results: list[tuple[dict, dict]] = []
    aborted_at: str | None = None
    http_errors: list[str] = []

    for index, eid in enumerate(targets, start=1):
        spec = specs[eid]
        started = time.monotonic()
        response = http.post(f"/diagnose/{eid}")
        elapsed = time.monotonic() - started

        if response.status_code != 201:
            http_errors.append(f"{eid}: {response.status_code} {response.text[:200]}")
            print(f"  {index:>2}/{len(targets)}  {eid:<14} HTTP {response.status_code} "
                  f"({elapsed:.1f}s) — ABORTING")
            aborted_at = eid
            break

        record = response.json()
        results.append((spec, record))
        print(
            f"  {index:>2}/{len(targets)}  {eid:<14} v{record['version']:<2} "
            f"{record['method']:<8} {record['root_cause']:<28} "
            f"conf={record['confidence']:<5} model={record['llm_model']}  ({elapsed:.1f}s)"
        )

        if record["method"] == "fallback":
            # A fallback here is not a diagnosis, it is a failed call wearing one.
            # Stop: the remaining events are better left undiagnosed than
            # overwritten with `unknown` records that look like real answers.
            print(
                f"\n  ABORTING at {eid}: method came back `fallback`, which means the "
                f"Gemini call itself failed.\n"
                f"  evidence: {record['evidence']}\n"
                f"  {len(targets) - index} events left untouched, deliberately."
            )
            aborted_at = eid
            break

    check(
        "every call returned HTTP 201",
        not http_errors,
        "\n           ".join(http_errors) if http_errors else f"{len(results)} calls",
    )
    check(
        "the run completed without aborting",
        aborted_at is None,
        f"aborted at {aborted_at}" if aborted_at else f"all {len(targets)} calls landed",
    )

    if not results:
        heading(f"CHECKPOINT — {PASSED} passed, {FAILED} failed")
        http.close()
        return 1

    # =====================================================================
    heading("RESULTS — event, surface, expected cause, model's answer, confidence, model")
    # =====================================================================
    print(
        f"  {'event':<14} {'surface':<13} {'analyst expected':<28} "
        f"{'model answered':<28} {'conf':>5}  {'agree':<6} model"
    )
    print("  " + "-" * 118)
    agreements = 0
    answerable = 0
    for spec, record in results:
        expected = spec["_llm_expected_cause"]
        expected_label = expected or "(unanswerable)"
        if expected is None:
            agree = "n/a"
        else:
            answerable += 1
            same = record["root_cause"] == expected
            agree = "yes" if same else "NO"
            agreements += int(same)
        print(
            f"  {record['event_id']:<14} {record['surface']:<13} {expected_label:<28} "
            f"{record['root_cause']:<28} {record['confidence']:>5}  {agree:<6} "
            f"{record['llm_model']}"
        )
    print(
        f"\n  root-cause agreement (REPORTED, not asserted): "
        f"{agreements}/{answerable} answerable strings matched the analyst's reading"
    )

    # =====================================================================
    heading("CHECKS")
    # =====================================================================
    check(
        "every record names the model that produced it",
        all(r["llm_model"] for _s, r in results),
        f"models seen: {sorted({r['llm_model'] for _s, r in results})}",
    )
    methods = Counter(r["method"] for _s, r in results)
    check(
        "no diagnosis fell back — every call returned a usable answer",
        methods.get("fallback", 0) == 0,
        f"methods: {dict(methods)}",
    )

    # Answerable strings must survive the decision stage's confidence floor.
    too_weak = [
        f"{r['event_id']} {r['root_cause']} @ {r['confidence']}"
        for s, r in results
        if s["_llm_expected_cause"] is not None and r["confidence"] < CONFIDENCE_FLOOR
    ]
    check(
        f"every answerable string cleared CONFIDENCE_FLOOR ({CONFIDENCE_FLOOR})",
        not too_weak,
        "\n           ".join(too_weak) if too_weak else f"{answerable} events checked",
    )

    # Unanswerable strings must NOT be answered confidently. This is the guardrail:
    # a model that invents a cause here is worse than one that says it cannot tell.
    guard = [(s, r) for s, r in results if s["_llm_expected_cause"] is None]
    bad_guard = [
        f"{r['event_id']} answered {r['root_cause']} @ {r['confidence']}"
        for _s, r in guard
        if r["root_cause"] != "unknown" or r["confidence"] >= CONFIDENCE_FLOOR
    ]
    if guard:
        check(
            "every deliberately-unanswerable string came back `unknown` below the floor",
            not bad_guard,
            "\n           ".join(bad_guard)
            if bad_guard
            else ", ".join(f"{r['event_id']}={r['root_cause']}@{r['confidence']}" for _s, r in guard),
        )

    illegal = [
        f"{r['event_id']}: {r['root_cause']} not allowed for {r['surface']}"
        for _s, r in results
        if r["root_cause"] not in ALLOWED_ROOT_CAUSES[r["surface"]]
    ]
    check(
        "every root cause is legal for its surface",
        not illegal,
        "\n           ".join(illegal)
        if illegal
        else "checked against ALLOWED_ROOT_CAUSES, the same closed set the server enforces",
    )
    check(
        "every diagnosis carries evidence",
        all(r["evidence"] for _s, r in results),
    )

    heading(f"CHECKPOINT — {PASSED} passed, {FAILED} failed")
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
