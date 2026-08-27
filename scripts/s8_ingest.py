"""Stage 8 Part B.1-B.2 — ingest and diagnose a slice of the demo batch.

Runs against the live server exactly as a client would: `POST /events` per event,
then `POST /diagnose/{event_id}` per event. No store module is imported, no database
handle is opened here. If the pipeline has a bug, this script has no way to route
around it.

The dry run predicted, offline, which events resolve on the rules path and which fall
through to Gemini, and what root cause the rules should return. This script checks the
live server against that prediction per event. A rules-path event that comes back with
a different root cause, or an event that reaches Gemini when the rules should have
caught it, is reported as a mismatch — not smoothed over.

Idempotency is a feature of the endpoints, not something this script arranges: a
re-run re-posts the same events and gets 200 instead of 201, and re-diagnoses them
into a new version. Re-running is therefore safe but not free — it adds diagnosis
versions and spends Gemini calls.

Usage:
    .venv/Scripts/python.exe scripts/s8_ingest.py --start 1 --count 50
    .venv/Scripts/python.exe scripts/s8_ingest.py --start 51 --count 150 --samples 0
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

PASSED = 0
FAILED = 0


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument("--start", type=int, default=1, help="1-based, inclusive")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="how many full diagnoses to print for inspection",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help=(
            "ingest every event in the slice but diagnose only the rules-path ones, "
            "deferring the LLM-path events. Use when Gemini quota is unavailable. The "
            "deferred event ids are printed in full and the run FAILS its completeness "
            "check, so a deferral can never be mistaken for a finished slice."
        ),
    )
    args = parser.parse_args()

    specs = ds.generate()
    slice_ = specs[args.start - 1 : args.start - 1 + args.count]
    if not slice_:
        print("empty slice — nothing to do")
        return 1

    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=120.0)
    health = http.get("/").json()
    print(
        f"server {args.base}   database={health.get('database')}   "
        f"gemini={health.get('gemini')}"
    )
    print(
        f"slice: events {args.start}..{args.start + len(slice_) - 1} "
        f"({slice_[0]['event_id']} .. {slice_[-1]['event_id']})"
    )
    expected_llm = [s["event_id"] for s in slice_ if s["_expects_llm"]]
    print(
        f"  {len(slice_)} events, {money(sum(s['amount'] for s in slice_))} at risk\n"
        f"  {len(expected_llm)} expected to reach Gemini: {expected_llm}"
    )

    # =====================================================================
    heading("INGEST — POST /events")
    # =====================================================================
    created = 0
    existing = 0
    ingest_errors: list[str] = []
    for spec in slice_:
        body = ds.api_body(spec)
        response = http.post("/events", json=body)
        if response.status_code == 201:
            created += 1
        elif response.status_code == 200:
            existing += 1
        else:
            ingest_errors.append(
                f"{spec['event_id']}: {response.status_code} {response.text[:200]}"
            )
    print(f"  201 created: {created}    200 already existed: {existing}")
    check(
        "every event was accepted",
        not ingest_errors,
        "\n           ".join(ingest_errors) if ingest_errors else "",
    )
    check(
        "ingestion accounted for the whole slice",
        created + existing == len(slice_),
        f"{created + existing} of {len(slice_)}",
    )

    # Read one back and confirm the server stored what was sent, including the status
    # it set itself rather than one we supplied.
    probe = slice_[0]
    stored = next(
        (
            doc
            for doc in http.get("/events").json()
            if doc["event_id"] == probe["event_id"]
        ),
        None,
    )
    check(f"{probe['event_id']} is readable from GET /events", stored is not None)
    if stored:
        check(
            "amount round-tripped exactly",
            stored["amount"] == probe["amount"],
            f"sent {probe['amount']}, stored {stored['amount']}",
        )
        check(
            "the server assigned the initial status itself",
            stored["status"] == "at_risk",
            f"status={stored['status']}, and no status was sent in the body",
        )

    # =====================================================================
    heading("DIAGNOSE — POST /diagnose/{event_id}")
    # =====================================================================
    results: list[tuple[dict, dict]] = []
    diagnose_errors: list[str] = []
    deferred: list[str] = []
    to_diagnose = [s for s in slice_ if not (args.skip_llm and s["_expects_llm"])]
    if args.skip_llm:
        deferred = [s["event_id"] for s in slice_ if s["_expects_llm"]]
        print(
            f"  --skip-llm: diagnosing {len(to_diagnose)} rules-path events, "
            f"DEFERRING {len(deferred)} LLM-path events"
        )
        for event_id in deferred:
            print(f"    deferred: {event_id}")
    started = time.monotonic()
    for index, spec in enumerate(to_diagnose, start=1):
        response = http.post(f"/diagnose/{spec['event_id']}")
        if response.status_code != 201:
            diagnose_errors.append(
                f"{spec['event_id']}: {response.status_code} {response.text[:200]}"
            )
            continue
        results.append((spec, response.json()))
        if index % 10 == 0 or index == len(to_diagnose):
            print(
                f"  {index}/{len(to_diagnose)} diagnosed "
                f"({time.monotonic() - started:.1f}s elapsed)"
            )
    check(
        "every event was diagnosed",
        not diagnose_errors,
        "\n           ".join(diagnose_errors) if diagnose_errors else "",
    )
    if deferred:
        # Deliberately a FAILURE, not a note. The slice is not finished, and a green
        # run here would let a half-diagnosed batch flow into the decision stage.
        check(
            "the slice is fully diagnosed",
            False,
            f"{len(deferred)} LLM-path events still undiagnosed: {deferred}. "
            "Re-run without --skip-llm once Gemini quota is available.",
        )

    methods = Counter(record["method"] for _spec, record in results)
    print(f"\n  method: {dict(methods)}")
    actual_llm = sorted(
        spec["event_id"]
        for spec, record in results
        if record["method"] in ("llm", "fallback")
    )
    # With --skip-llm no LLM-path event was attempted, so the prediction under test is
    # that NONE of the rules-path events reached Gemini. That is the whole claim the
    # routing gate makes about them, and it is still worth checking.
    attempted_llm = [] if args.skip_llm else sorted(expected_llm)
    check(
        "the events that reached Gemini are exactly the ones predicted offline",
        actual_llm == attempted_llm,
        f"predicted {attempted_llm}, actual {actual_llm}"
        + (" (LLM-path events deferred)" if args.skip_llm else ""),
    )
    check(
        "no diagnosis fell back — every Gemini call returned a usable answer",
        methods.get("fallback", 0) == 0,
        f"{methods.get('fallback', 0)} fell back",
    )

    # Rules path: the root cause is deterministic, so it is checkable per event.
    mismatches: list[str] = []
    for spec, record in results:
        if spec["_expects_llm"]:
            continue
        if record["root_cause"] != spec["_intended_root_cause"]:
            mismatches.append(
                f"{spec['event_id']}: expected {spec['_intended_root_cause']}, "
                f"got {record['root_cause']} "
                f"(reason={spec['raw_failure_reason']!r})"
            )
    check(
        "every rules-path diagnosis returned the predicted root cause",
        not mismatches,
        "\n           ".join(mismatches) if mismatches else
        f"{sum(1 for s, _r in results if not s['_expects_llm'])} events checked",
    )

    print("\n  root cause distribution as diagnosed")
    causes = Counter(record["root_cause"] for _spec, record in results)
    for cause, n in causes.most_common():
        flags = {
            record["recoverable"]
            for _spec, record in results
            if record["root_cause"] == cause
        }
        if flags == {True}:
            label = "yes"
        elif flags == {False}:
            label = "no"
        else:
            label = f"MIXED {flags}"
        print(f"    {cause:<28} {n:>3}   recoverable={label}")
    check(
        "recoverability is consistent for every root cause",
        all(
            len(
                {
                    record["recoverable"]
                    for _spec, record in results
                    if record["root_cause"] == cause
                }
            )
            == 1
            for cause in causes
        ),
    )

    confidences = [record["confidence"] for _spec, record in results]
    rules_conf = [
        record["confidence"] for spec, record in results if not spec["_expects_llm"]
    ]
    llm_conf = [
        record["confidence"] for spec, record in results if spec["_expects_llm"]
    ]
    print(
        "\n  confidence: rules path "
        + (
            f"{min(rules_conf):.2f}..{max(rules_conf):.2f}"
            if rules_conf
            else "none in this slice"
        )
        + (
            f"   LLM path {min(llm_conf):.2f}..{max(llm_conf):.2f}"
            if llm_conf
            else "   LLM path: none in this slice"
        )
    )
    check(
        "every diagnosis carries evidence",
        all(record["evidence"] for _spec, record in results),
    )
    check(
        "no confidence exceeds 1.0 or falls below 0.0",
        all(0.0 <= value <= 1.0 for value in confidences),
    )

    # =====================================================================
    if args.samples:
        heading(f"SAMPLE DIAGNOSES — {args.samples} for inspection")
        # Deliberately weighted: every LLM-path diagnosis first, because those are the
        # ones no rule table constrains and the only ones where the model's judgement
        # is on show. The rest spread across distinct root causes rather than taking
        # the first N, which would be almost all insufficient_funds.
        chosen: list[tuple[dict, dict]] = [
            pair for pair in results if pair[0]["_expects_llm"]
        ]
        seen_causes = {record["root_cause"] for _spec, record in chosen}
        for spec, record in results:
            if len(chosen) >= args.samples:
                break
            if spec["_expects_llm"]:
                continue
            if record["root_cause"] in seen_causes:
                continue
            seen_causes.add(record["root_cause"])
            chosen.append((spec, record))
        for spec, record in results:
            if len(chosen) >= args.samples:
                break
            if any(spec["event_id"] == s["event_id"] for s, _r in chosen):
                continue
            chosen.append((spec, record))

        for spec, record in chosen[: args.samples]:
            route = "GEMINI" if spec["_expects_llm"] else "RULES"
            print()
            print("-" * 78)
            print(
                f"  {record['event_id']}   {record['surface']}   "
                f"{money(spec['amount'])} {spec['currency']}   [{route}]"
            )
            print("-" * 78)
            reason = spec["raw_failure_reason"]
            print(
                f"  raw_failure_reason: "
                f"{'(none supplied)' if reason is None else repr(reason)}"
            )
            print(f"  customer_ref:       {spec['customer_ref']}")
            print(
                f"  -> root_cause  {record['root_cause']}"
                f"   (intended: {spec['_intended_root_cause'] or 'left to the model'})"
            )
            print(
                f"     method      {record['method']}"
                f"     confidence  {record['confidence']}"
                f"     recoverable {record['recoverable']}"
            )
            print(f"     version     {record['version']}   id {record['id']}")
            print("     evidence:")
            for item in record["evidence"]:
                print(f"       - {item}")

    heading(f"CHECKPOINT — {PASSED} passed, {FAILED} failed")
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
