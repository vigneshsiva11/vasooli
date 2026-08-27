"""Stage 8 Part B.2 (closure) — prove there are no outstanding diagnosis gaps.

Answers one question for all 200 demo events, not just the ones this session
touched: does every event have a current diagnosis, and is that diagnosis the one
the routing gate predicted?

Deliberately reads the data twice, by two different routes, and compares:

* raw MongoDB via pymongo — what is actually stored;
* the live HTTP API via `GET /diagnoses?event_id=...` — what the app will serve.

If the app and the database disagree about an event's current diagnosis, that is a
finding, reported per-event rather than summarised away. Nothing from `app.metrics`
is imported; the only app import is the dataset's own prediction of what each event
should resolve to, which is the thing under test, not a source of answers.

Usage:
    .venv/Scripts/python.exe scripts/s8_diagnosis_gaps.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

BASE = "http://127.0.0.1:8123"

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


async def main() -> int:
    specs = {s["event_id"]: s for s in ds.generate()}
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db_name]
    http = httpx.Client(base_url=BASE, timeout=60.0)

    # ------------------------------------------------------------------
    heading("EVENTS — are all 200 demo events present?")
    # ------------------------------------------------------------------
    stored_events = await db.events.find(
        {"event_id": {"$regex": "^demo_"}}, {"event_id": 1, "amount": 1, "status": 1}
    ).to_list(None)
    stored_ids = {d["event_id"] for d in stored_events}
    missing_events = sorted(set(specs) - stored_ids)
    check(
        "every generated demo event is in the database",
        not missing_events,
        f"{len(stored_ids)} of {len(specs)}"
        + (f"; missing {missing_events}" if missing_events else ""),
    )
    check(
        "no unexpected demo events exist",
        not (stored_ids - set(specs)),
        f"extras: {sorted(stored_ids - set(specs)) or 'none'}",
    )

    # ------------------------------------------------------------------
    heading("DIAGNOSES — latest version per demo event, straight from MongoDB")
    # ------------------------------------------------------------------
    latest: dict[str, dict] = {}
    cursor = db.diagnoses.find({"event_id": {"$regex": "^demo_"}}).sort(
        [("event_id", 1), ("version", 1)]
    )
    version_counts: Counter[str] = Counter()
    async for doc in cursor:
        latest[doc["event_id"]] = doc  # ascending sort => last write wins
        version_counts[doc["event_id"]] += 1

    undiagnosed = sorted(set(specs) - set(latest))
    check(
        "every demo event has at least one diagnosis",
        not undiagnosed,
        f"{len(latest)} of {len(specs)} diagnosed"
        + (f"; UNDIAGNOSED: {undiagnosed}" if undiagnosed else ""),
    )

    methods = Counter(d["method"] for d in latest.values())
    print(f"\n  latest-version method distribution: {dict(methods)}")
    predicted_llm = {eid for eid, s in specs.items() if s["_expects_llm"]}
    actual_llm = {eid for eid, d in latest.items() if d["method"] in ("llm", "fallback")}
    check(
        "the events whose current diagnosis came from a model are exactly the predicted set",
        actual_llm == predicted_llm,
        f"predicted {len(predicted_llm)}, actual {len(actual_llm)}"
        + (
            f"; unexpected {sorted(actual_llm - predicted_llm)}, "
            f"missing {sorted(predicted_llm - actual_llm)}"
            if actual_llm != predicted_llm
            else ""
        ),
    )
    stale_fallbacks = sorted(
        eid for eid, d in latest.items() if d["method"] == "fallback"
    )
    check(
        "no demo event's current diagnosis is a fallback",
        not stale_fallbacks,
        f"fallbacks: {stale_fallbacks}" if stale_fallbacks else "0 fallbacks",
    )

    # ------------------------------------------------------------------
    heading("ROOT CAUSES — does each current diagnosis match what was predicted?")
    # ------------------------------------------------------------------
    rules_mismatch: list[str] = []
    llm_disagree: list[str] = []
    for eid, spec in specs.items():
        doc = latest.get(eid)
        if doc is None:
            continue
        if not spec["_expects_llm"]:
            if doc["root_cause"] != spec["_intended_root_cause"]:
                rules_mismatch.append(
                    f"{eid}: predicted {spec['_intended_root_cause']}, "
                    f"stored {doc['root_cause']}"
                )
        else:
            expected = spec["_llm_expected_cause"]
            target = expected if expected is not None else "unknown"
            if doc["root_cause"] != target:
                llm_disagree.append(
                    f"{eid}: analyst read {target}, model answered {doc['root_cause']}"
                )
    check(
        "every rules-path event's current root cause matches the offline prediction",
        not rules_mismatch,
        "\n           ".join(rules_mismatch)
        if rules_mismatch
        else f"{len(specs) - len(predicted_llm)} events checked",
    )
    # REPORTED, not a pass/fail on the model's judgement.
    print(
        f"\n  LLM-path agreement with the analyst's reading: "
        f"{len(predicted_llm) - len(llm_disagree)}/{len(predicted_llm)}"
        + (f"\n    divergences: {llm_disagree}" if llm_disagree else "")
    )

    # ------------------------------------------------------------------
    heading("PROVENANCE — which model, and how is it recorded?")
    # ------------------------------------------------------------------
    key_absent = sorted(eid for eid, d in latest.items() if "llm_model" not in d)
    key_null = sorted(
        eid for eid, d in latest.items() if "llm_model" in d and d["llm_model"] is None
    )
    named = {eid: d["llm_model"] for eid, d in latest.items() if d.get("llm_model")}
    print(f"  key absent (written before the field existed) : {len(key_absent)}")
    print(f"  key present, null (no model was called)       : {len(key_null)} {key_null}")
    print(f"  key present, model named                      : {len(named)}")
    print(f"  distinct models named                         : {sorted(set(named.values()))}")
    check(
        "every model-path diagnosis names the model that produced it",
        set(named) == predicted_llm,
        f"named {len(named)}, expected {len(predicted_llm)}"
        + (f"; unnamed: {sorted(predicted_llm - set(named))}" if predicted_llm - set(named) else ""),
    )
    check(
        "no rules-path diagnosis falsely names a model",
        not any(latest[eid].get("llm_model") for eid in set(specs) - predicted_llm if eid in latest),
        "a rules record naming a model would be a false provenance claim",
    )

    # ------------------------------------------------------------------
    heading("CROSS-CHECK — does the HTTP API serve the same current diagnosis?")
    # ------------------------------------------------------------------
    # Sample rather than all 200: 400 HTTP round trips to re-confirm a field-level
    # equality the previous section already established from storage would be slow
    # without being stronger. The sample is chosen to cover every interesting case
    # instead of the first N.
    sample = sorted(predicted_llm) + [
        eid for eid in sorted(set(specs) - predicted_llm)[:10]
    ] + key_null
    sample = list(dict.fromkeys(sample))
    disagreements: list[str] = []
    for eid in sample:
        versions = http.get("/diagnoses", params={"event_id": eid}).json()
        if not versions:
            disagreements.append(f"{eid}: API returned no diagnoses")
            continue
        api = max(versions, key=lambda d: d["version"])
        mongo = latest[eid]
        for field in ("version", "method", "root_cause", "confidence", "recoverable"):
            if api[field] != mongo[field]:
                disagreements.append(
                    f"{eid}.{field}: API {api[field]!r} vs Mongo {mongo[field]!r}"
                )
        if api.get("llm_model") != mongo.get("llm_model"):
            disagreements.append(
                f"{eid}.llm_model: API {api.get('llm_model')!r} vs "
                f"Mongo {mongo.get('llm_model')!r}"
            )
    check(
        f"the API and the database agree on all {len(sample)} sampled events",
        not disagreements,
        "\n           ".join(disagreements)
        if disagreements
        else f"{len(sample)} events x 6 fields compared",
    )

    # ------------------------------------------------------------------
    heading("SUMMARY")
    # ------------------------------------------------------------------
    total_versions = sum(version_counts.values())
    print(f"  demo events               : {len(stored_ids)}")
    print(f"  demo events diagnosed     : {len(latest)}")
    print(f"  outstanding gaps          : {len(undiagnosed)} {undiagnosed or ''}")
    print(f"  diagnosis versions stored : {total_versions} across {len(version_counts)} events")
    print(f"  current: {methods.get('rules', 0)} rules / {methods.get('llm', 0)} llm / "
          f"{methods.get('fallback', 0)} fallback")
    at_risk_money = sum(
        d["amount"] for d in stored_events if d["event_id"] in specs
    )
    print(f"  demo money at risk        : {at_risk_money:,.2f}")

    heading(f"CHECKPOINT — {PASSED} passed, {FAILED} failed")
    client.close()
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
