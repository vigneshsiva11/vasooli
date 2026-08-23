"""Stage 3 verification — decide every diagnosed event and expose the ERV math.

For each event this prints the full candidate comparison with the arithmetic
spelled out, recomputed *independently* of the server: the candidate table comes
from importing `app.decision.evaluate` in this process, while the recommendation
comes back over HTTP from the running app. If the two ever disagree the script
says so, which is what makes the printed numbers worth hand-checking.

Run:  python scripts/s3_verify.py http://127.0.0.1:8123
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reasoning strings contain U+00D7 and U+2212, which a cp1252 console cannot
# encode. The stored text is correct as-is; it is this terminal that is narrow.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.decision import evaluate
from app.models import (
    ALLOWED_INTERVENTIONS,
    CONFIDENCE_FLOOR,
    NO_ACTION_INTERVENTIONS,
    expected_recovery_value,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"


def request(path: str, method: str = "GET", payload: dict | None = None):
    """Issue a JSON request and return (status, body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode(errors="replace")}


def rule(char: str = "-", width: int = 100) -> None:
    print(char * width)


def main() -> None:
    _, events = request("/events")
    _, diagnoses = request("/diagnoses")

    amounts = {event["event_id"]: event["amount"] for event in events}
    currencies = {event["event_id"]: event["currency"] for event in events}

    # /diagnoses returns every version; decide from the latest, which is what the
    # route itself does, so the printed basis matches the stored basis.
    latest: dict[str, dict] = {}
    for diagnosis in diagnoses:
        current = latest.get(diagnosis["event_id"])
        if current is None or diagnosis["version"] > current["version"]:
            latest[diagnosis["event_id"]] = diagnosis

    print(f"{len(events)} events, {len(diagnoses)} diagnoses, "
          f"{len(latest)} events with a latest diagnosis")
    print(f"confidence floor = {CONFIDENCE_FLOOR}\n")

    # Order the report so the interesting categories are adjacent.
    def sort_key(item: tuple[str, dict]) -> tuple:
        event_id, dx = item
        if not dx["recoverable"]:
            bucket = 2
        elif dx["confidence"] < CONFIDENCE_FLOOR:
            bucket = 1
        else:
            bucket = 0
        return (bucket, dx["surface"], dx["root_cause"], event_id)

    results: list[tuple[str, dict, dict]] = []
    by_intervention: dict[str, list[str]] = defaultdict(list)
    mismatches: list[str] = []

    for event_id, dx in sorted(latest.items(), key=sort_key):
        amount = amounts[event_id]
        currency = currencies[event_id]

        status_code, decision = request(f"/decide/{event_id}", method="POST")
        if status_code >= 400:
            print(f"FAILED {event_id}: {status_code} {decision}")
            continue

        results.append((event_id, dx, decision))
        by_intervention[decision["recommended_intervention"]].append(event_id)

        rule()
        gate = (
            "recoverable=False -> hard block"
            if not dx["recoverable"]
            else f"confidence {dx['confidence']:.2f} < floor {CONFIDENCE_FLOOR} -> blocked"
            if dx["confidence"] < CONFIDENCE_FLOOR
            else "passed both gates -> ERV compared"
        )
        print(
            f"{event_id}   {dx['surface']}/{dx['root_cause']}   "
            f"{currency} {amount:,.2f}   conf {dx['confidence']:.2f}   "
            f"recoverable={dx['recoverable']}   dx v{dx['version']}"
        )
        print(f"  gate: {gate}")

        # Independently recomputed candidate table. Skipped when a gate fired
        # before the matrix was reached, because in that case no candidate was
        # scored at all and printing a table would misrepresent the decision.
        gated = (
            not dx["recoverable"] or dx["confidence"] < CONFIDENCE_FLOOR
        )
        if not gated:
            scored = evaluate(dx["surface"], dx["root_cause"], amount)
            print(f"  candidates scored ({len(scored)}), best first:")
            for index, (candidate, cost, erv) in enumerate(scored):
                manual = amount * candidate.recovery_probability - cost
                marker = "<= chosen" if index == 0 else ""
                print(
                    f"    {candidate.intervention:<30} "
                    f"{amount:>12,.2f} x {candidate.recovery_probability:.2f} "
                    f"- {cost:>5,.2f} = {erv:>13,.2f}   "
                    f"(unrounded {manual:,.4f}) {marker}"
                )

        print(
            f"  RECOMMENDED: {decision['recommended_intervention']}   "
            f"cost {decision['estimated_cost']:,.2f}   "
            f"p {decision['recovery_probability']:.2f}   "
            f"ERV {decision['expected_recovery_value']:,.2f}   "
            f"decision v{decision['version']}"
        )

        # Cross-check the server's stored winner against this process's own
        # arithmetic, so the table above is not simply the same code agreeing
        # with itself over HTTP.
        recomputed = expected_recovery_value(
            decision["revenue_at_risk"],
            decision["recovery_probability"],
            decision["estimated_cost"],
        )
        if abs(recomputed - decision["expected_recovery_value"]) > 0.01:
            mismatches.append(f"{event_id}: stored ERV != recomputed {recomputed}")

        if not gated:
            best = evaluate(dx["surface"], dx["root_cause"], amount)[0]
            expected_name = best[0].intervention
            if best[2] < 0 and expected_name not in NO_ACTION_INTERVENTIONS:
                expected_name = "no_action_negative_erv"
            if decision["recommended_intervention"] != expected_name:
                mismatches.append(
                    f"{event_id}: server chose "
                    f"{decision['recommended_intervention']}, local scoring says "
                    f"{expected_name}"
                )

        print(f"  reasoning: {decision['reasoning']}")

    rule("=")
    print(f"\ndecisions made: {len(results)}")
    print("\nrecommendation distribution:")
    for intervention in sorted(by_intervention, key=lambda name: -len(by_intervention[name])):
        ids = by_intervention[intervention]
        print(f"  {intervention:<30} {len(ids):>2}  {', '.join(sorted(ids))}")

    unknown = set(by_intervention) - ALLOWED_INTERVENTIONS
    print(f"\ninterventions outside the fixed catalogue: {sorted(unknown) or 'none'}")
    print(f"server/local scoring disagreements: {mismatches or 'none'}")


if __name__ == "__main__":
    main()
