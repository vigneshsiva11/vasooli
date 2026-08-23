"""Stage 3 verification — seed events that reach the uncovered matrix cells.

Every root cause below is reachable through the deterministic rules classifier, so
this seeds real pipeline output rather than hand-written diagnoses: no Gemini call,
no quota spend. The one exception is flagged explicitly in the output.

Run against a live server:  python scripts/s3_seed.py http://127.0.0.1:8123
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

# (event_id, surface, amount, raw_failure_reason, why this case is here)
SEEDS: list[tuple[str, str, float, str, str]] = [
    (
        "dec_S3_TEMP",
        "payment",
        6500.00,
        "gateway_timeout",
        "temporary_processing_error: two free retries, immediate should win on p alone",
    ),
    (
        "dec_S3_DUNNING",
        "subscription",
        150.00,
        "retries_exhausted",
        "dunning_exhausted on a small subscription: 150 x 0.25 - 50 = -12.50, negative ERV",
    ),
    (
        "dec_S3_TINYINV",
        "receivable",
        120.00,
        "Customer replied that they will pay next week, cash flow is tight",
        "genuine_delay below the 170 crossover: cheap reminder should beat the sequence",
    ),
    (
        "dec_S3_GHOST",
        "receivable",
        90000.00,
        "no_response",
        "non_responsive, large: manual escalation should beat the sequence",
    ),
    (
        "dec_S3_GHOST_SMALL",
        "receivable",
        100.00,
        "unreachable",
        "non_responsive, small: same pair, opposite winner - the crossover is real",
    ),
    (
        "dec_S3_CART",
        "checkout",
        2400.00,
        "session_timeout",
        "checkout technical_error: single mapped candidate",
    ),
    (
        "dec_S3_BROWSE",
        "checkout",
        80.00,
        "just browsing, comparing prices",
        "low_purchase_intent on a tiny cart: 80 x 0.05 - 5 = -1.00, negative ERV",
    ),
]


def post(path: str, payload: dict | None = None) -> tuple[int, dict]:
    """POST JSON and return (status, body)."""
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode(errors="replace")}


def main() -> None:
    print(f"seeding {len(SEEDS)} events against {BASE}\n")

    for event_id, surface, amount, reason, why in SEEDS:
        payload = {
            "event_id": event_id,
            "surface": surface,
            "amount": amount,
            "currency": "INR",
            "customer_ref": f"cust_{event_id.lower()}",
            "raw_failure_reason": reason,
        }
        status, body = post("/events", payload)
        ingested = body.get("event_id", body)

        dx_status, dx = post(f"/diagnose/{event_id}")
        if dx_status >= 400:
            print(f"  {event_id:<20} INGEST {status}  DIAGNOSE FAILED {dx_status}: {dx}")
            continue

        print(
            f"  {event_id:<20} ingest={status} "
            f"{dx['surface']}/{dx['root_cause']} conf={dx['confidence']:.2f} "
            f"method={dx['method']} recoverable={dx['recoverable']} v{dx['version']}"
        )
        print(f"  {'':<20} why: {why}")

    print("\nseeding done")


if __name__ == "__main__":
    main()
