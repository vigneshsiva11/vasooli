"""Stage 3 — exercise the matrix-driven no-action branch.

Gate 3 (the intervention matrix mapping a root cause to no action) is the one
branch live data cannot currently reach: every `unknown` diagnosis in the
database is also below the confidence floor, so gate 2 fires first and gate 3 is
never consulted. The ratified handling of `unknown` — always no_action — is
therefore untested by the main verification run.

This inserts ONE synthetic diagnosis to close that gap: `unknown` at confidence
0.95, which the rules classifier never emits and the LLM has never emitted. It is
written through the real diagnosis store, so it is a genuine stored record, and it
is labelled in its evidence so nobody later mistakes it for observed output.

Run:  python scripts/s3_unknown.py http://127.0.0.1:8123
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo
from app.diagnosis import append as append_diagnosis
from app.ingestion import upsert_event
from app.models import Diagnosis, RevenueEvent

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

EVENT_ID = "dec_S3_UNKNOWN_HIGHCONF"


def post(path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{BASE}{path}", data=b"", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def seed() -> None:
    await connect_to_mongo()

    event = RevenueEvent(
        event_id=EVENT_ID,
        surface="payment",
        amount=75_000.00,
        currency="INR",
        customer_ref="cust_unknown_highconf",
        raw_failure_reason="ERR_7734",
    )
    document_id, created = await upsert_event(event)
    print(f"event {EVENT_ID}: id={document_id} created={created} amount=75,000.00")

    diagnosis = Diagnosis(
        event_id=EVENT_ID,
        surface="payment",
        root_cause="unknown",
        confidence=0.95,
        recoverable=True,
        evidence=[
            "SYNTHETIC TEST RECORD, not observed pipeline output",
            "written by scripts/s3_unknown.py to reach the matrix no-action branch",
            "the rules classifier cannot emit 'unknown'; the LLM has only ever "
            "emitted it at 0.10-0.20 confidence",
        ],
    )
    diagnosis_id, version = await append_diagnosis(diagnosis, method="rules")
    print(
        f"diagnosis: id={diagnosis_id} v{version} payment/unknown conf=0.95 "
        f"recoverable=True  (synthetic)"
    )

    await close_mongo_connection()


def main() -> None:
    asyncio.run(seed())

    status, decision = post(f"/decide/{EVENT_ID}")
    if status >= 400:
        print(f"decide failed: {status} {decision}")
        sys.exit(1)

    print(
        f"\nRECOMMENDED: {decision['recommended_intervention']}   "
        f"cost {decision['estimated_cost']:,.2f}   "
        f"p {decision['recovery_probability']:.2f}   "
        f"ERV {decision['expected_recovery_value']:,.2f}"
    )
    print(f"reasoning: {decision['reasoning']}")

    expected = "no_action"
    actual = decision["recommended_intervention"]
    if actual != expected:
        print(f"\nFAIL: expected {expected}, got {actual}")
        sys.exit(1)
    print(
        "\nOK: 75,000.00 at risk, confidence 0.95, recoverable=True, and the "
        "recommendation is still no_action — the matrix, not a confidence gate, "
        "is what blocked it."
    )


if __name__ == "__main__":
    main()
