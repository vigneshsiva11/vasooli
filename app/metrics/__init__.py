"""Stage 7 — the dashboard data layer. Reads and aggregates; writes nothing.

The stage's public surface. Four core metrics, one baseline comparison, one audit
trail — every one of them derived from a snapshot taken at the start of the request,
so the figures in a single response cannot disagree with each other.

The read-only property is structural rather than promised:

* `app/metrics/reader.py` is the only module here that touches the database, and the
  only Motor method it calls is `find`;
* `app/metrics/aggregate.py`, `baseline.py` and `audit.py` are pure functions over a
  frozen `Snapshot` — they have no database handle to write with;
* `app/routes/metrics.py` declares `@router.get` and nothing else;
* `app/metrics/verify_readonly.py` asserts both of those mechanically, by walking the
  live app's OpenAPI schema for non-GET methods and this package's source for write
  calls.
"""

from __future__ import annotations

from app.metrics.aggregate import (
    by_intervention,
    by_root_cause,
    promise_metrics,
    summarize,
)
from app.metrics.audit import EventNotFound, assemble
from app.metrics.baseline import compare
from app.metrics.reader import Snapshot, load_snapshot

__all__ = [
    "EventNotFound",
    "Snapshot",
    "assemble",
    "by_intervention",
    "by_root_cause",
    "compare",
    "load_snapshot",
    "promise_metrics",
    "summarize",
]
