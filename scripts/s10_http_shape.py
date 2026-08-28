"""Stage 10 HTTP shape cases — ZERO Gemini calls, and it proves they were zero.

Two kinds of case here:

* bodies FastAPI refuses before the handler runs (naive/future `received_at`,
  empty or over-long `raw_text`, an extra field). These never reach any code of
  mine, which is the point of putting those invariants on the request model.
* bodies that reach the handler but are refused before the API call — an unknown
  event, and an event already recovered. The promisability check is deliberately
  ordered *ahead* of the model call so a doomed request costs no quota.

The second group is the interesting one, and asserting a 404 does not prove the
ordering. So the run counts `promise_extractions` documents before and after: a
call that had been spent would have written an audit record, whatever the status
code said. The count staying flat is the evidence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models.promise_extraction import MAX_RAW_TEXT_CHARS

BASE = "http://127.0.0.1:8123"
SETTLED_EVENT = "exe_S5_20260824T164458_REMIND"  # status 'recovered'
LIVE_EVENT = "demo_186_rcv"
MESSAGE = "I'll pay by Friday, just had a cash flow issue."

PASS = 0
FAIL = 0


def check(label: str, expected, actual, extra: str = "") -> None:
    global PASS, FAIL
    ok = expected == actual
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"         expected={expected!r}  actual={actual!r}")
    if extra:
        print(f"         {extra}")


async def main() -> int:
    await connect_to_mongo()
    db = get_database()
    before = await db.promise_extractions.count_documents({})

    print("=" * 78)
    print("STAGE 10 — HTTP SHAPE CASES (zero Gemini calls)")
    print(f"promise_extractions documents before: {before}")
    print("=" * 78)

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        print("\n[D] REFUSED BY THE REQUEST MODEL — never reaches the handler")

        r = await client.post("/promises/from-text", json={"raw_text": MESSAGE})
        check("D1  missing event_id -> 422", 422, r.status_code,
              str(r.json()["detail"][0]["loc"]) + " " + r.json()["detail"][0]["type"])

        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT, "raw_text": "   \t  "})
        check("D2  whitespace-only raw_text -> 422", 422, r.status_code,
              r.json()["detail"][0]["msg"])

        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT,
                                    "raw_text": "x" * (MAX_RAW_TEXT_CHARS + 1)})
        check("D3  raw_text over the bound -> 422", 422, r.status_code,
              r.json()["detail"][0]["type"])

        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT, "raw_text": MESSAGE,
                                    "received_at": "2026-08-20T10:30:00"})
        check("D4  naive received_at -> 422 (not 500)", 422, r.status_code,
              r.json()["detail"][0]["msg"])

        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT, "raw_text": MESSAGE,
                                    "received_at": future})
        check("D5  future received_at -> 422 (not 500)", 422, r.status_code,
              r.json()["detail"][0]["msg"])

        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT, "raw_text": MESSAGE,
                                    "promised_date": "2026-08-30"})
        check("D6  caller cannot supply promised_date directly -> 422",
              422, r.status_code,
              f"{r.json()['detail'][0]['loc']} {r.json()['detail'][0]['type']} — "
              "no bypassing the extractor to set the date by hand")

        r = await client.post("/promises/from-text",
                              json={"event_id": LIVE_EVENT, "raw_text": MESSAGE,
                                    "confidence": 0.99})
        check("D7  caller cannot supply confidence -> 422", 422, r.status_code,
              f"{r.json()['detail'][0]['loc']} {r.json()['detail'][0]['type']} — "
              "no supplying your own way past the floor")

        print("\n[E] REFUSED IN THE HANDLER, BEFORE THE MODEL CALL")

        r = await client.post("/promises/from-text",
                              json={"event_id": "no_such_event_at_all",
                                    "raw_text": MESSAGE})
        check("E1  unknown event -> 404", 404, r.status_code, r.json()["detail"])

        r = await client.post("/promises/from-text",
                              json={"event_id": SETTLED_EVENT, "raw_text": MESSAGE})
        check("E2  already-recovered event -> 422", 422, r.status_code,
              r.json()["detail"])

    after = await db.promise_extractions.count_documents({})
    print("\n" + "-" * 78)
    print(f"promise_extractions documents after:  {after}")
    check("F1  NO extraction record written -> no Gemini call was spent",
          before, after,
          "this is the proof the promisability check runs ahead of the model call, "
          "not merely the claim that it does")

    print("\n" + "=" * 78)
    print(f"RESULT — {PASS} passed, {FAIL} failed   (Gemini calls used: 0)")
    print("=" * 78)
    await close_mongo_connection()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
