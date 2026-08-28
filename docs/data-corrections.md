# Data corrections

`policy_verdicts` is an append-only authorization log. Nothing in the running
system deletes from it or edits a stored verdict, and no code path exists that
can. Every correction is therefore a manual, out-of-band act, and every one of
them is recorded here in full — what changed, why it could not be left alone,
when, and what replaced it. A verdict log with an unexplained hole in it is worth
less than one with no hole and no explanation, because the hole is the part you
cannot check.

Two kinds of correction appear below, and they are not equally serious. Deleting
a verdict destroys a record. Correcting a *provenance label* leaves the verdict
untouched and revises a claim the project made about how well it knows where that
verdict came from. The second is nearly routine — a provenance claim can go stale
without anything being wrong — but it is recorded to the same standard, because a
field that quietly self-corrects is not evidence of anything.

---

## 2026-08-25 — `pol_S4_MULTI` v6, v7, v8, v9 removed

Four verdict documents deleted from `policy_verdicts`. All four were fixture
residue: their trails describe a sequence of contacts that never happened.

### What they were

| version | `_id` | verdict | decision | `evaluated_at` | fingerprint |
|---|---|---|---|---|---|
| 6 | `6a8c6f69474e2d8946197893` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-22T04:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 7 | `6a8c6f69474e2d8946197894` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-23T10:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 8 | `6a8c6f69474e2d8946197895` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-24T14:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 9 | `6a8c6f6bf81f8f6f93d0e6c8` | `blocked` / `customer_opted_out` | `6a8c6f6af81f8f6f93d0e6c7` v4 | 2026-08-24T16:20:59.230Z | `rb1_aba19a5e5ee8124e` `evaluated` |

All four named event `pol_S4_MULTI`, customer `cust_pol_S4_multi`, intervention
`manual_escalation`. **No execution referenced any of them** — checked against
`executions.policy_verdict_id` before deleting, and the deletion would have
aborted if one had.

The four documents are preserved verbatim, exactly as stored, in
[`docs/data-corrections/2026-08-25-pol_S4_MULTI-v6-v9.json`](data-corrections/2026-08-25-pol_S4_MULTI-v6-v9.json).
The summary below is a reading of them, not a substitute for them.

v6, v7 and v8 each carried these two trail entries:

```
contact_cap:      PASS (0 of 3 contacts used for event pol_S4_MULTI)      # v6
contact_cap:      PASS (1 of 3 contacts used for event pol_S4_MULTI)      # v7
contact_cap:      PASS (2 of 3 contacts used for event pol_S4_MULTI)      # v8
customer_opt_out: PASS (customer cust_pol_S4_multi has not opted out)     # all three
```

Both statements were false when they were written. By that point the event
already carried three authorized contacts (v1, v2, v3) and the customer had
already withdrawn consent — v4 and v5 both blocked on `customer_opted_out`,
and the opt-out record is dated 2026-08-24T04:18:36.154Z, hours before v6.
So these three documents recorded an empty contact history and a live consent
on an event that had neither.

### Why

Not a policy-engine bug. The engine judged exactly what it was given; it was
given fiction. The cause was in the fixture, `scripts/s4_multi.py`.

That script demonstrates the maximal simultaneous-failure case: a receivable
chased three times over several days, then a withdrawn consent, then a balance
grown past the never-auto ceiling, so that four protections fail at once. To lay
a three-day timeline out in a few seconds it builds a `PolicyContext` by hand
with a back-dated `now`, rather than gathering context from the database.

That is sound on an event with no history — the injected counts and anchors are
chosen to equal exactly what `gather_context` would have returned from an empty
event, so the verdicts it writes still re-derive from the record. It stopped
being sound the moment the script ran a second time against the same event id,
which it did because `EVENT_ID` and `CUSTOMER` were fixed constants. The second
run injected `prior_authorized_contacts=0` and `customer_opted_out=False` over
an event that already had three contacts and a withdrawn consent, and wrote the
result into an append-only log, permanently.

### Why v9 as well

v9 was a genuine verdict — produced through `POST /authorize/{event_id}` from
real gathered context, not injected — and it re-derived correctly *while the
fiction was present*. But its trail quotes that fiction:

```
contact_cap:      FAIL (6 contact(s) already authorized for event pol_S4_MULTI, ...)
contact_cooldown: FAIL (last authorized contact was 2.0h ago at 2026-08-24T14:20:57.289000+00:00, ...)
```

The `6` is v1–v3 plus v6–v8. The cooldown anchor `14:20:57.289` is v8's
back-dated `evaluated_at`. With v6–v8 gone the true count is 3 and the true
anchor is v3's timestamp, so v9's stored prose becomes unre-derivable — and v9
carries an `evaluated` fingerprint, which is a claim of direct evidence and is
held to exact reproduction with no trail tolerance. v9 could not be kept
truthfully once its premises were removed. It is residue of the same second run
by the same mechanism, one step removed.

Deleting v6–v9 rather than v6–v8 also leaves the version sequence contiguous at
`[1, 2, 3, 4, 5]`. That was not the reason for the decision, but it means
`check_uniqueness()` in `scripts/s4_audit.py` needed no exemption for a
documented gap — the gap check still means what it always meant, that a hole in
an append-only sequence is a lost record.

### What was not deleted

v1–v5 stayed. v1, v2 and v3 are the first run's three chases: injected context,
but on a genuinely empty event, so what they assert was true and they re-derive.
v4 and v5 are real blocks on the withdrawn consent. v5 in particular was written
by the second run *before* the injection, so it never quoted the fiction — its
trail reads `3 contact(s) already authorized` with v3's anchor, which is correct
both before and after this deletion.

### What replaced it

Deleting v9 left v5 as the event's current verdict while the pipeline's current
recommendation had moved on to decision v4 — a live authorization resting on a
superseded recommendation, which `scripts/s4_audit.py` §6 reports as a finding
and should. Rather than widen that check, the event was re-authorized through
the ordinary route:

```
POST /authorize/pol_S4_MULTI  ->  v6  blocked/customer_opted_out  on decision v4
```

evaluated live against the real record, fingerprint `rb1_aba19a5e5ee8124e`,
source `evaluated`. Its trail:

```
decision_is_actionable: PASS (manual_escalation is a real intervention)
customer_opt_out:       FAIL (customer cust_pol_S4_multi is on the do-not-contact list ...)
contact_cap:            FAIL (3 contact(s) already authorized for event pol_S4_MULTI, cap is 3 ...)
contact_cooldown:       PASS (last authorized contact was 26.4h ago, outside the 24h cooldown)
erv_minimum:            PASS (ERV 32,950.00 clears the 25.00 minimum at a cost of 50.00)
amount_tier:            FAIL (60,000.00 is at or above the 25,000.00 never-auto ceiling ...)
```

Three failures where the deleted v9 claimed four. The difference is the
cooldown, and it is the honest number: the real last contact is v3's, 26.4 hours
before this verdict, so the cooldown has genuinely lapsed. The fiction's `2.0h
ago` came from v8's back-dated timestamp. The four-simultaneous-failure case the
fixture exists to demonstrate is still demonstrated, re-derivably, by the
run-tagged event `pol_S4_MULTI_20260825T042142`.

### How this is prevented from recurring

`scripts/s4_multi.py` was fixed before the deletion, in two ways:

* `EVENT_ID` and `CUSTOMER` are now run-tagged with a UTC timestamp, so each run
  gets a fresh event and a fresh customer and the injected context is always
  describing the empty event it claims to describe.
* `three_prior_chases()` now refuses structurally rather than trusting the tag.
  Before injecting anything it counts existing verdicts for the event and checks
  the opt-out list, and aborts if either says the event has a history. An
  injected context written over a real history is a stored verdict describing a
  world the database contradicts, and nothing downstream can tell the difference
  after the fact — so the check belongs before the write, not in the audit.

### Verification

`scripts/s4_audit.py` after the deletion and re-authorization: **exit 0**, 189
verdicts re-derived under the rulebook each one names, 0 mismatches, 0 findings,
85 events with no gaps in any version sequence. `pol_S4_MULTI` v1–v6 all report
`match`, with v6 `attested` against `rb1_aba19a5e5ee8124e`.

One advisory remains open and is unrelated to this correction: 6 verdicts are
labelled `reconstructed` but are now reproduced exactly by two rulebooks each,
so their source should be weakened to `backfilled`. They re-derive correctly;
the label overstates how uniquely it was identified.

*(Resolved the same day — see the entry below.)*

---

## 2026-08-25 — six `reconstructed` labels weakened to `backfilled`

Six verdicts had their `rulebook_fingerprint_source` changed from
`reconstructed` to `backfilled`. No verdict, trail, reason or fingerprint value
was altered. Ratified before the change.

| event | version | fingerprint (unchanged) |
|---|---|---|
| `pol_S4_20260824T045335_RPL_FRESH` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T045335_RPL_PRE` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T045335_RPL_PRE` | 2 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_FRESH` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_PRE` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_PRE` | 2 | `rb1_3ecc9dde2839f090` |

### Why

`reconstructed` means one rulebook reproduces the verdict exactly and no other
does. That is not a property of the verdict alone — it is a property of the
verdict *and the registry it was compared against*. Adding a rulebook to
`app/policy/rulebook.py` can therefore falsify a `reconstructed` label that was
accurate when it was written, without anything about the verdict changing.

`rb1_aba19a5e5ee8124e` — the amendment that moved the cooldown anchor from
`verdict.evaluated_at` to `execution.executed_at` — did exactly that. On an event
with no executions the two anchors coincide, so verdicts on such events re-derive
identically under it and under its predecessor. Six verdicts that were uniquely
identified when `scripts/s4_fingerprint_backfill.py` ran became ambiguous the
moment that rulebook entered the registry, and their labels went on claiming a
uniqueness that no longer held.

Nothing was wrong with the verdicts. `scripts/s4_audit.py` had been reporting
this as `weakened` — explicitly not a re-derivation failure, since each still
re-derives byte-exactly under the rulebook it names — and it was the last open
advisory on the Stage 4 log.

### How

Via `scripts/s4_fingerprint_reconcile.py`, added for this correction.

It does not restate the labelling rule. `decide_fingerprint()` in
`scripts/s4_fingerprint_backfill.py` already encodes it — one exact match →
`reconstructed`, several → `backfilled` — so the reconcile script imports that
function and asks what it would say today. The corrected labels are therefore
produced by the same code that produced every other label in the collection,
rather than by a second implementation that could drift from it, or by hand.

The migration itself could not be reused: it skips any verdict that already
carries a fingerprint, and its write filter requires the field to be absent. That
refusal is deliberate and was left intact.

Bounds on what the reconcile script will write:

* the only field it writes is `rulebook_fingerprint_source`, and only in the
  direction `reconstructed` → `backfilled`;
* it writes only when the fingerprint already on the record is still among the
  exact matches. The guard is *not* "the fingerprint did not move" — see the
  tiebreak-drift finding below. Weakening a label is safe while the stored
  fingerprint still fits and unsafe if it does not, because that would trade an
  overstated claim for a false one;
* a *strengthening* — `backfilled` → `reconstructed` — is refused
  unconditionally. A shrinking registry can make an identification look unique
  again, and writing that would manufacture evidence out of the archive being
  incomplete, which is the inversion of what the field is for;
* verdicts labelled `evaluated` are never recomputed at all. That label records
  the rulebook that actually ran, at the moment it ran. It is evidence rather than
  identification, and `decide_fingerprint` has neither the vocabulary to express
  it nor any standing to contradict it. (The first draft of the script did
  recompute them, and every one of the 135 registered as a spurious divergence —
  which is what surfaced the distinction.)
* dry-run by default, and each write re-asserts `_id` + fingerprint + the
  expected prior source in its filter, so a concurrent change matches nothing
  rather than being overwritten.

### An unrelated finding this surfaced: tiebreak drift

Recomputing every migrated label turned up **44 further verdicts** that diverge,
all already labelled `backfilled` and all left alone. Their stored fingerprint is
`rb1_3ecc9dde2839f090`; today's recomputation would nominate
`rb1_aba19a5e5ee8124e` instead, because all four known rulebooks now reproduce
them exactly and the ambiguous-case tiebreak takes the newest that fits. The
registry has grown since the migration ran, so "the newest that fits" no longer
resolves to the value stored.

Every stored fingerprint here is still an exact match, so no record is wrong and
the audit re-derives all of them. But it does falsify one inference the migration
documents (`scripts/s4_fingerprint_backfill.py`, lines 40–41 and 147–148): that a
`backfilled` verdict naming a *superseded* rulebook is one today's policy could
not have produced. Today's rulebook reproduces these 44 exactly.

That inference was therefore already unreliable for 44 stored verdicts before
this correction touched anything — which is also why the six weakenings were not
blocked on it. The two issues are independent: one is a label overstating
uniqueness, the other is a tiebreak target going stale.

Restoring the signal would mean overwriting 44 (now 50) fingerprint values that
were each correct when chosen, for no gain in re-derivability. **Not done, not
applied, and reported rather than acted on.** The reconcile script prints the full
list on every run.

**Ratified 2026-08-25: leave all 44 untouched.** Every one still re-derives
correctly under the fingerprint it names, and rewriting 50 stored values to settle
a question of docstring precision is not worth a further data mutation. The
imprecise claim was in the prose, so the prose was corrected instead of the data.

What changed, in `scripts/s4_fingerprint_backfill.py` — comments and printed
output only, no behaviour:

* the docstring passage that read "a backfilled verdict naming a superseded
  rulebook is one that today's policy demonstrably could not have produced" now
  says what the field actually records: the newest rulebook that fit the verdict
  among those known *when the label was written* — the tightest consistent account
  available at that moment, not a claim that no other rulebook fits, and not a
  claim about what today's policy could have produced;
* the same claim appeared a second time as an inline comment inside
  `decide_fingerprint()`, the function that implements the tiebreak. Softened
  there too, since leaving it would have left the overclaim in the code that
  causes it. It now states explicitly that the earlier version claimed this and
  was wrong to;
* `summarise()` printed it as well — "name a SUPERSEDED rulebook, meaning today's
  policy cannot have produced them". Reworded to "today's rules do not reproduce
  them, so the tightest fit is an archived one".

One nuance the rewrite pins down, because it explains why the claim was ever
made. At the instant the migration runs the claim is *true by construction*: the
tiebreak takes the newest rulebook that fits, so if the stored fingerprint is a
superseded one, the current rulebook provably did not reproduce that verdict. What
the original prose missed is that this is a statement about the registry at write
time, and the registry grows. A rulebook added later can fit a verdict exactly
without anything about the verdict changing, which makes the stored value stale as
a *tiebreak* while leaving it perfectly valid as a *fit*. The claim did not become
wrong; it decayed, silently, and nothing recomputed it.

That decay is now detected rather than assumed away:
`scripts/s4_fingerprint_reconcile.py` re-checks every migrated label against the
current registry on each run and reports the drift, without writing. Both softened
passages point at it and at this file.

### Verification

* `scripts/s4_fingerprint_reconcile.py` — dry run showed exactly the 6 the audit
  had named, then `--apply` wrote 6 of 6; all 189 verdicts read back valid
  through `PolicyVerdictRecord`. Labels now: 135 `evaluated`, 51 `backfilled`
  (was 45), 3 `reconstructed` (was 9).
* re-run of the same script: **0 in scope**, so the correction is idempotent.
* `scripts/s4_fingerprint_backfill.py` dry run: still plans 0 writes, unaffected.
* `scripts/s4_audit.py` — **exit 0**, and the `weakened` advisory is gone
  entirely. The six now report `match ... stand-in`, and none of them appears
  under `reclassified`, so weakening the label did not start relying on the trail
  tolerance that `backfilled` grants: they still re-derive byte-exactly.
* `scripts/s4_adversarial.py` **exit 0** (137 refused), `scripts/s5_audit.py`
  **exit 0**, `scripts/s5_adversarial.py` **exit 0** (120 refused).

---

## 2026-08-25 — open finding, NOT a correction: no receivable path can be verified

Nothing was changed for this entry. No document was written, edited or deleted, and
no code was altered. It is recorded here because it was found while building Stage 6
and the stage's scope boundary forbade acting on it, and a finding held only in a
closing report is a finding that stops existing when the session does.

### The gap

Three facts that are each individually reasonable and jointly leave a hole:

1. `app/decision/matrix.py`, receivable block — **every** candidate intervention for
   every receivable root cause is contact-type. `reminder`,
   `escalating_reminder_sequence`, `manual_escalation`, `payment_plan_offer`,
   `recovery_payment_link` (reclassified as contact-type in Stage 4 hardening). None
   of them causes a Razorpay payment link to be created.
2. Stage 6 Part A writes a `VerificationRecord` only in response to a Razorpay
   `payment_link.paid` webhook — that is, only for an event whose recovery actually
   created a link.
3. Therefore **no receivable event can ever be verified as recovered by this
   system.** Money can come back; the webhook that would tell us never fires,
   because we never gave Razorpay a link to fire it about.

### Why it mattered to Stage 6 rather than Stage 4

The first draft of `app/ptp/store.py` restricted promises to the receivable surface,
on the reasoning that a promise to pay is a receivables concept. Combined with the
above, that made `promised -> honored` **unreachable in production**: a receivable
promise could be broken, chased and re-chased, but the one transition the entire
safety property is built around could only ever have been exercised by a test.

The restriction was dropped rather than the transition, and replaced with a
terminal-status guard (`NON_PROMISABLE_STATUSES = TERMINAL_EVENT_STATUSES`). A
promise against a failed card payment that somebody has said they will settle is a
real thing to record, and it is verifiable, because that surface does produce links.
The comment at `app/ptp/store.py:176` states this in full at the point of the
decision.

### Not fixed here, deliberately

The fix would be a change to the decision matrix — some receivable root cause
resolving to a link-producing intervention — and that is a Stage 3/4 economic
judgement about what the right recovery action for an unpaid invoice actually is, not
a Stage 6 plumbing detail. Stage 6 was given an explicit scope boundary against
re-opening the decision engine, so this is reported and left. It is the first thing
to look at if receivable recovery is ever expected to close the loop.


---

## 2026-08-26 — open finding, NOT a correction: eleven recovered verification records describe four payments

> **Annotation added 2026-08-27, after checkpoint 7. Nothing below is rewritten.**
> The finding still stands; its counts have grown with the dataset. At 305-event
> scale it is **24 recovered records describing 17 distinct payments**, 7 ignored as
> duplicates, a literal sum of 44,605.14 against a deduped **29,605.14**. See the
> final entry in this file.

Nothing was changed for this entry. No document was written, edited or deleted, and
no code in Stages 1-6 was altered. Stage 7 was given an explicit rule — "if you find
a data quality issue while building aggregations, note it in docs/data-corrections.md
and tell me — do not silently exclude records or fix underlying data without asking"
— and this is the entry that rule asked for.

### What was found

`GET /metrics/summary` was specified as summing `amount_recovered` across every
`VerificationRecord` with `outcome="recovered"`. Taken literally, that sum is
**22,500.00 INR**. The eleven records it sums describe **four** payments:

| event | payment link | recovered records | amount each | literal sum | actually paid |
|---|---|---|---|---|---|
| `exe_S5ADV_20260825T045458_HONEST` | `plink_TTsV8YH18jku14` | 6 | 2,200.00 | 13,200.00 | 2,200.00 |
| `exe_S5_20260825T042248_DRETRY` | `plink_TTrxBuptDYfom8` | 3 | 2,000.00 | 6,000.00 | 2,000.00 |
| `ptp_20260825T111455_C` | `plink_TTyyRX5VdP3kKj` | 1 | 1,650.00 | 1,650.00 | 1,650.00 |
| `ptp_20260825T112307_C` | `plink_TTz7C9begJ4nhD` | 1 | 1,650.00 | 1,650.00 | 1,650.00 |
| | | **11** | | **22,500.00** | **7,500.00** |

The literal reading reports **15,000.00 INR of money that never existed** — twice the
amount that did.

### Why the duplicates exist, and why they are not corrupt

They are correct records of what actually arrived. Stage 6's Part A verification
harness is re-runnable, and each run mints a fresh `x-razorpay-event-id`
(`s6a_paid_01`, `s6a_20260825T075440_paid_01`, `s6a_20260825T161504_paid_01`, …).
`razorpay_event_id` carries a unique index, which is what makes webhook delivery
idempotent — so a *different* event id is by design a *different* event, and each
one legitimately produced its own append-only record. The dedup logic did exactly
what it was built to do.

What no layer was ever asked to know is that a payment link is **paid once**. Three
harness runs against one link produce three honest records of three distinct Razorpay
events describing one payment. `GET /audit-trail/exe_S5ADV_20260825T045458_HONEST`
shows this plainly: nine verification records, six of them `recovered`, one execution,
one link.

### What Stage 7 does about it

It counts each recovered payment **once per execution**, taking the latest record by
`verified_at`, and it says so in the response rather than in a comment:

* `total_revenue_recovered: 7500.0` — the deduped figure
* `recovered_verification_records: 11` — the raw count, unchanged
* `distinct_recoveries_counted: 4`
* `duplicate_verification_records_ignored: 7`
* `methodology` — states the rule and the reason in the payload itself

"Ignored" means ignored *by this sum*. The seven records remain exactly where they
are, because in the `verifications` collection they are an accurate record of what
Razorpay sent, and deleting them would destroy evidence to make a total look tidy.
The dedup happens at read time, every request, and is visible in the response.

This was implemented as a unilateral call, under an instruction to proceed rather than
open a ratification round, and **ratified on 2026-08-26**: the deduped figure with the
raw/deduped breakdown exposed. It is reversible in one place — `distinct_recoveries()`
in `app/metrics/reader.py` — and both figures are on the wire, so nothing was hidden by
making it before it was ratified.

### Not fixed here, deliberately

Two candidate fixes exist and both are out of Stage 7's read-only scope:

1. **Constrain at write time** — a unique index on `(execution_id, outcome)` in
   `app/webhooks/store.py`, so a second `paid` for one execution is rejected rather
   than stored. This would trade idempotency-by-event-id for
   idempotency-by-outcome, and it changes Stage 6 behaviour.
2. **Make the harness non-re-runnable** — have Stage 6's Part A fixtures create a
   fresh event and link per run instead of reusing `exe_S5ADV_..._HONEST`. Cleaner,
   but it means the harness stops testing repeat delivery against a link that has
   already been paid, which is a real production case worth keeping.

Neither is a Stage 7 decision. The read-time dedup makes the reported number correct
today without touching either.

### One more thing worth knowing about the same event

`exe_S5_20260825T042248_DRETRY`'s three records carry `amount_mismatch=True`: Razorpay
reported **2,000.00** against an `amount_expected` of **2,050.00** — a 50.00 shortfall.
That flag is Stage 6 working correctly — it is recorded, not reconciled — and Stage 7
reports the amount Razorpay stated (2,000.00), not the amount that was expected. So
2,000.00 of the headline 7,500.00 is a short payment that no layer has reconciled.
Flagged here because an `amount_mismatch=True` recovery sitting inside a headline total
is worth a human look, and Stage 7 was not authorized to resolve it.

---

## 2026-08-26 — ratified decision, NOT a correction: `total_revenue_at_risk` excludes nothing

Nothing was changed for this entry either. It records a decision about **scope** rather
than a correction to data, and it is here because the decision is invisible in the code
— non-exclusion leaves no filter behind to read — and because it is the one figure most
likely to be quoted out of this system.

Stage 7 was asked to sum `amount` across all `RevenueEvent`s "excluding any events you
can identify as synthetic test fixtures created purely for adversarial testing during
Stages 2-6". Stage 7 excludes **nothing** and reports all **105 events / 744,127.75
INR**. Ratified 2026-08-26.

### Why exclusion was not possible to do honestly

**Every event in this dataset is synthetic.** There is no production traffic. So the
instruction cannot mean "keep the real ones" — it can only mean "draw a line through a
uniformly synthetic population", and there is no principled place to draw it. The
population, grouped by the id prefix each stage's harness used:

| prefix family | n | at risk | % of total | what it is |
|---|---|---|---|---|
| `pol_S4_` | 38 | 195,600.00 | 26.3% | Stage 4 policy harness |
| `dec_S3_` | 9 | 175,349.00 | 23.6% | Stage 3 decision harness |
| `rcv_S2_` | 2 | 173,000.00 | 23.2% | Stage 2 seed, plausible business event |
| `exe_S5ADV_` | 10 | 79,000.00 | 10.6% | Stage 5 adversarial harness |
| `exe_S5_` | 21 | 56,010.00 | 7.5% | Stage 5 execution harness |
| `pay_S2_` | 4 | 33,999.00 | 4.6% | Stage 2 seed, plausible business event |
| `ptp_` | 12 | 13,800.00 | 1.9% | Stage 6 PTP harness |
| `evt_test_` | 1 | 4,999.00 | 0.7% | smoke test |
| `chk_S2_` | 1 | 3,499.00 | 0.5% | Stage 2 seed, plausible business event |
| (other) | 1 | 2,499.50 | 0.3% | plausible business event |
| `pay_DUP_TEST` | 1 | 1,875.25 | 0.3% | idempotency test |
| `adv_` | 2 | 1,800.00 | 0.2% | Stage 6 adversarial harness |
| `sub_S2_` | 2 | 1,798.00 | 0.2% | Stage 2 seed, plausible business event |
| `pay_RACE_` | 1 | 899.00 | 0.1% | race test |
| **total** | **105** | **744,127.75** | **100.0%** | |

### The sensitivity is the whole argument

Two criteria, each defensible, and the headline moves by more than 3×:

* **Narrow** — adversarial suites and explicitly-named test fixtures only
  (`exe_S5ADV_`, `adv_`, `evt_test_`, `pay_DUP_TEST`, `pay_RACE_`, `pay_S2_INJECT`):
  **16 events / 89,572.25 / 12.0%** excluded → would report **654,555.50**.
* **Broad** — every per-stage harness cohort, i.e. everything that exists because a
  stage needed a fixture: **96 events / 530,331.25 / 71.3%** excluded → would report
  **213,796.50**.

Only about 10 of the 105 events (the `*_S2_*` seeds plus one unprefixed event,
214,795.50) look like plausible business events at all. A headline that can be
654,555.50 or 213,796.50 depending on an unratified judgement is not a measurement, and
encoding either one would have buried that judgement in a filter nobody would think to
question later.

An earlier draft of this finding quoted a third, intermediate criterion (22 events /
375,671.25 / 50.5%). That figure was one arbitrary line among many rather than *the*
cohort size, which is precisely the point; the spread above supersedes it.

### What is reported instead

The sum over everything, plus the split that lets a reader take the rate either way:

* `total_revenue_at_risk: 744127.75` — all 105 events, nothing excluded
* `non_recoverable_at_risk: 148299.0` — the portion the system deliberately declined to
  chase, derived from each event's own latest `Diagnosis.recoverable` flag rather than
  from a hardcoded list (dispute 125,000 + fraud 22,000 + churn 1,299)
* `total_events: 105` and `events_without_decision: 1`
* `methodology` — states the non-exclusion and its reason in the payload itself

`GET /metrics/baseline-comparison` narrows separately and on a stated rule — latest
diagnosis `recoverable=True` — giving `eligible_events: 101` and
`eligible_revenue_at_risk: 594829.75`, with `excluded_non_recoverable: 3` and
`excluded_undiagnosed: 1` disclosed on the response.

### How to reverse this

One filter in `load_snapshot()` in `app/metrics/reader.py`, given a ratified list of
event ids or prefixes. Nothing else in Stage 7 assumes the full population — every
figure derives from the snapshot it is handed. If a cohort is ever excluded, the
excluded count and amount should be reported on `/metrics/summary` alongside the total,
for the same reason the dedup exposes its raw count: a subtraction the reader cannot see
is a subtraction the reader cannot check.

---

## 2026-08-26 — provenance disclosure: 16 demo diagnoses came from `gemini-3.5-flash-lite`, not `gemini-3.6-flash`

No stored document was deleted or edited for this entry. Sixteen diagnoses were
**appended** through the ordinary route, and a field was added so that they can say
which model produced them. It is recorded here because the Stage 8 demo dataset is
the thing a reader will judge the system's diagnosis quality by, and "which model
answered" is not derivable from the data afterwards unless it was written down at
the time.

### What happened

The demo batch is 200 events. 184 resolve on the deterministic rules path and never
reach a model. The remaining **16** carry deliberately ambiguous free text that no
regex in `app/diagnosis/rules.py` matches, so `classify()` returns `None` and the
pipeline consults Gemini — that is the whole point of those 16, and it is verified
offline before any call is spent.

Those 16 were diagnosed on **`gemini-3.5-flash-lite`**. The configured model for
Stages 1–7 was `gemini-3.6-flash`, and every other Gemini-produced record in this
database came from it.

### Why

The Gemini free tier allows **20 `generateContent` requests per day, per model, per
project** — `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`. The
`gemini-3.6-flash` bucket was exhausted during dataset generation: 8 calls at
checkpoint 1 (4 of them latency probing) and 13 on the ambiguity probe that
established these 16 strings actually reach the model and are answerable, totalling
21 attempts against a ceiling of 20.

The quota id is **per model**, which is the load-bearing detail. A sibling model has
its own untouched bucket, so switching models made 16 further calls possible the same
day without waiting for the 00:00 Pacific reset. Ratified explicitly: *"staying on
the free tier, no billing enabled … use `gemini-3.5-flash-lite` instead of waiting
for the `gemini-3.6-flash` daily quota reset."*

The switch is one line in `.env` (`GEMINI_MODEL`). No code selects a model; the
pipeline reads one from config, which is why this is a configuration disclosure and
not a code change.

### The 16 events

Diagnosed via `POST /diagnose/{event_id}` — the real route, real prompt builder,
`temperature=0.0`, real response schema. Script: `scripts/s8_llm_diagnose.py`.

| event | surface | what a careful analyst reads | model answered | stored confidence | agrees |
|---|---|---|---|---|---|
| `demo_009_pay` | payment | *(unanswerable)* | `unknown` | 0.20 | n/a |
| `demo_029_pay` | payment | `insufficient_funds` | `insufficient_funds` | 0.90 | yes |
| `demo_062_pay` | payment | `card_expired` | `card_expired` | 0.90 | yes |
| `demo_072_pay` | payment | `issuer_declined` | `issuer_declined` | 0.90 | yes |
| `demo_073_pay` | payment | `temporary_processing_error` | `temporary_processing_error` | 0.90 | yes |
| `demo_083_pay` | payment | *(unanswerable)* | `unknown` | 0.20 | n/a |
| `demo_087_pay` | payment | `insufficient_funds` | `insufficient_funds` | 0.90 | yes |
| `demo_105_chk` | checkout | *(unanswerable — no failure text at all)* | `unknown` | 0.10 | n/a |
| `demo_109_chk` | checkout | `checkout_friction` | `checkout_friction` | 0.90 | yes |
| `demo_116_chk` | checkout | `technical_error` | `technical_error` | 0.90 | yes |
| `demo_118_chk` | checkout | `payment_method_unavailable` | `payment_method_unavailable` | 0.90 | yes |
| `demo_163_sub` | subscription | `mandate_expired` | `mandate_expired` | 0.90 | yes |
| `demo_167_sub` | subscription | `insufficient_funds` | `insufficient_funds` | 0.90 | yes |
| `demo_178_rcv` | receivable | `genuine_delay` | `genuine_delay` | 0.90 | yes |
| `demo_180_rcv` | receivable | `non_responsive` | `non_responsive` | 0.90 | yes |
| `demo_190_rcv` | receivable | `genuine_delay` | `genuine_delay` | 0.90 | yes |

**13 of 13** answerable strings matched the analyst's reading and cleared the
decision stage's `CONFIDENCE_FLOOR` of 0.5. **3 of 3** deliberately-unanswerable
strings came back `unknown` *below* the floor, so `app/decision/engine.py` routes
them to `no_action_low_confidence` — the guardrail case, where a confident wrong
answer would be the failure and an admission of ignorance is the correct output.

Agreement is **reported, not asserted**. `scripts/s8_llm_diagnose.py` makes only the
confidence-floor behaviour pass/fail; the classification is the model's, and a
divergence would have been printed as a divergence rather than as a test failure.

### Two things the stored numbers do not say

**The 0.90s are at a ceiling, not a measurement.** `LLM_CONFIDENCE_CEILING = 0.90`
in `app/diagnosis/service.py` clamps whatever a model states. The earlier probe
called `gemini.propose_diagnosis` directly and saw raw values of 0.88–0.95 for the
same strings. So every stored `0.9` above means "the model said 0.90 or more" and
cannot be read as "the model said exactly 0.90". The unanswerable rows are below the
ceiling and are therefore the model's own unmodified numbers.

**The two models are not equally cautious about ignorance.** On `gemini-3.6-flash`
the probe measured `demo_009_pay`'s string at `unknown`/**0.35** and
`demo_083_pay`'s at `unknown`/**0.40**. On `gemini-3.5-flash-lite` through the
pipeline they are **0.20** and **0.20**. Both models land on the same side of the
0.5 floor, so the guardrail holds either way, but the margin differs by roughly half
and that difference is a property of the model, not of the event.

### Prior art for the same substitution

`demo_029_pay` and `demo_062_pay` were already measured on `gemini-3.5-flash-lite`
during the ambiguity probe, for the same quota reason, and `scripts/s8_ambiguity_probe.py`
records that at the two affected entries in `ALREADY_MEASURED` rather than letting them
sit unlabelled among the `3.6-flash` measurements. Both were re-diagnosed through the
real pipeline here, so their stored records are pipeline output rather than probe output.
Their answers agreed across both readings (`insufficient_funds` 0.95 → 0.90 clamped;
`card_expired` 0.95 → 0.90 clamped).

### The scope waiver this required

Stage 8's standing rule was *"do not modify diagnosis, decision, policy, execution,
verification, or PTP logic in this stage."* Recording the model **had** to be a code
change, because nothing persisted it: `DiagnosisMethod` says whether an LLM was
involved, never which one. The waiver was requested before any call was spent and
ratified for this single field.

Four files, no behavioural change to any classification:

* `app/models/diagnosis.py` — `llm_model: str | None` on `DiagnosisRecord` (the
  stored form), **not** on `Diagnosis`. The domain contract stays "an explanation,
  and nothing else"; which model produced it is a fact about how the record was made.
  A `_llm_model_only_when_a_model_answered` validator rejects `method="rules"` with a
  named model, since a rules record naming a model is a false provenance claim.
* `app/diagnosis/service.py` — `diagnose()` now returns a 3-tuple. The model name is
  read once from `get_settings()`, which is `@lru_cache`d, so it is provably the same
  value `propose_diagnosis` used rather than a second guess at it. It is recorded on
  the failure paths too: a `GeminiUnavailable` fallback names the model that was
  unreachable, and a rejected out-of-vocabulary root cause names the model that
  answered badly.
* `app/diagnosis/store.py` — writes the field on **every** document, including as an
  explicit `null` on rules-path records.
* `app/routes/diagnoses.py` — the only caller; unpacks the tuple and returns the field.

The route was verified on a rules-path event (`demo_001_pay`, zero Gemini cost) and
the validator on all six `method`/`llm_model` combinations before the 16 calls were
spent. The quota allowed one attempt, so the field had to exist first, not after —
adding it afterwards would have meant either 16 more calls or a backfill, and a
backfilled provenance label is the same weak claim this project already had to
correct once for rulebook fingerprints.

### Three states, deliberately distinguishable

`append()` writes an explicit `null` rather than omitting the field, so the
collection now holds three genuinely different facts about its 253 demo diagnosis
versions, and no reader has to guess which:

| state in MongoDB | means | current demo diagnoses |
|---|---|---|
| key absent | written before provenance was recorded | 183 |
| key present, `null` | no model was called | 1 (`demo_001_pay`) |
| key present, named | that model produced it | 16 |

### What was NOT done, and how to reverse it

**The 183 were not re-diagnosed.** Their current diagnosis predates the field, so
the key is simply absent. Re-running them is free of Gemini cost — the rules path
never calls a model — but it would append 183 diagnosis versions to an append-only
collection purely so a field reads `null` instead of being missing, and
`method="rules"` already states unambiguously that no model was involved. The
asymmetry is disclosed here rather than papered over.

To make the batch uniform: re-`POST /diagnose/{event_id}` for the 183, which is
deterministic and adds one version each. Worth doing only before any decision pins
a diagnosis id — after that it puts every pinned decision one version behind, which
`scripts/s4_audit.py` §6 reports as a finding, correctly.

**No claim of reproducibility is being made.** A recorded model name is provenance:
it says which model answered. It is not a guarantee that asking again returns the
same thing. `temperature=0.0` narrows variation but does not eliminate it, and a
provider can change behaviour behind a stable model name. This is the same
distinction the rulebook-fingerprint entry above had to be corrected for — a claim
that is true when written and decays quietly afterwards — so it is stated as the
weaker thing from the start.

### Verification

* `scripts/s8_llm_diagnose.py` — **9 passed, 0 failed**. 16/16 HTTP 201, all
  `method="llm"`, zero fallbacks, all naming `gemini-3.5-flash-lite`, every root
  cause legal for its surface.
* `scripts/s8_diagnosis_gaps.py` — **9 passed, 0 failed**, read from raw MongoDB and
  cross-checked over HTTP. 200/200 demo events diagnosed, **0 outstanding gaps**,
  current methods 184 rules / 16 llm / **0 fallback**, 184/184 rules-path root causes
  matching the offline prediction, and the set of model-diagnosed events exactly
  equal to the set predicted offline.
* Demo money at risk unchanged at **1,443,090.27** — diagnosis does not touch
  amounts, and a change here would have meant something else went wrong.


---

## 2026-08-27 — 65 execution records superseded, in two passes, and a wrong diagnosis corrected

`executions` is not append-only in the sense `policy_verdicts` is, but it is the
record of side effects that reached a payment gateway, so deleting from it is held
to the same standard. Two deletions happened on this date, for two different
reasons, and the second one exists because the reason given for the first was
partly wrong.

### Pass 1 — 62 records, gateway-refused against an exhausted account

The first checkpoint 4 run attempted 62 executions needing 59 real Razorpay
test-mode payment links. 8 completed, 54 failed. Every failure stored the same
verbatim gateway text:

```
Razorpay returned HTTP 429 — BAD_REQUEST_ERROR: Too many requests
```

whose unmasked body was `RATE_LIMIT_EXCEEDED — "test mode limit of 30 reached for
payment_link"`. That account held 30 links and Razorpay test mode allows 30 per
account for the account's lifetime; cancelling one does not return its slot. The
54 were therefore not retryable on those credentials, and the 5 links the run did
create sat on an account the demo was moving off, where nothing downstream could
verify them.

* **archive**: `.s8_archive/executions_superseded_20260827T085506Z.json`, 62
  documents, 38,103 bytes, written and read back before anything was deleted
* **verified after**: 0 demo executions remained; all 31 fixture executions
  survived, matched by document `_id` rather than by count, because a count match
  alone would not rule out a swap
* **not deleted anywhere**: the 5 links created on the old account remain there as
  orphans — `plink_TUMOKvoDvR8vPU`, `plink_TUMOMhtZUiVM2Y`, `plink_TUMONz21okUsEO`,
  `plink_TUMOPFVOVnvtvY`, `plink_TUMOQfDTPUN9n8`. Along with 5 `vslprobe_`
  throwaways, they are why that account reads 30/30.

### The wrong part: there are TWO limits, not one

The re-run on a fresh account with **0 of 30 slots used** accepted 5 creates in
about 16 seconds and then refused the 6th, 7th and 8th — with **25 slots still
free**. 87 seconds later a single create was accepted again. So:

| limit | scope | what it does | costs a slot when refused? |
|---|---|---|---|
| lifetime ceiling, 30 links | per merchant account, permanent | refuses forever once reached | no |
| burst limit, ~5 creates / ~60s | per account, transient | refuses until the window clears | no |

Both are real, and the old account had hit both. The first run's 54 failures are
still correctly attributed to the lifetime ceiling — that account genuinely read
30/30 and its error body said so in those words.

**What was concluded too strongly.** Stage 8 tooling recorded that this was "not a
rate limit," on the strength of a probe that made 15 creates at 3-second gaps and
had 0 accepted, with failures persisting 11 hours. That probe ran against the
already-exhausted account, where every create fails at any pace, so it could not
distinguish the two mechanisms — it confirmed the lifetime cap and was read as
excluding a burst limit. The 11-hour persistence is likewise fully explained by
30/30 and says nothing about pacing.

The coincidence that made this convincing is that both accounts accepted **exactly
5** links before refusing. On the old account 5 was its remaining headroom to
30/30. On the new one 5 is the burst allowance. Same number, different mechanism.

Three claims in `scripts/` asserted the stronger version and were corrected in
place: the `--link-budget` help text, the printed budget check ("refusals are not
retryable"), and the archive `reason` for this pass. A spent slot never coming back
is true; a refused create being unretryable is not.

### Pass 2 — 3 records, refused by the burst limit

`scripts/s8_supersede.py --only-failed` cleared the three records the paced re-run
needed to retry (`demo_007_pay`, `demo_008_pay`, `demo_010_pay`), keeping the 5
that had completed. Deletion is keyed on `_id`, not `event_id`, so an event holding
both a completed and a failed record could not lose the completed one.

* **archive**: `.s8_archive/executions_superseded_20260827T090023Z.json`, 3
  documents, 2,231 bytes
* **no Razorpay link was orphaned** — all three were refused before a link existed
* **why they had to go**: `policy_verdict_id` carries a unique index, so an event
  that already holds any execution record replays as HTTP 200 instead of calling
  the gateway. Left in place, the retry would have reported success while doing
  nothing — which is exactly what the 54 stale records did to the first re-run
  attempt.

### What made the difference, and what caught it

Two pieces of Stage 8 tooling, both added on this date:

* a **circuit breaker** (3 consecutive failures) in `scripts/s8_execute.py`. The
  first run wrote 54 dead records because nothing was watching; the re-run stopped
  at 8 of 28. Because a gateway refusal arrives as HTTP **201** with
  `status="failed"` — which is how the first run reported `{201: 62}` while 54 of
  them were dead — the breaker inspects the stored record, not the status code.
* a **`--gap`** applied before link-creating calls only, since contact-type actions
  call no external API. At 15 seconds, 20 consecutive creates were accepted with
  zero refusals.

### Final state

| | value |
|---|---|
| demo executions | **28, all `completed`, 0 failed** |
| real payment links | 25, all with an `https` URL, 25 distinct ids |
| templated contacts | 3, none carrying a link artifact |
| distinct policy verdicts pinned | 28 |
| fixture executions | 31, untouched |
| money chased | 43,644.89 |
| modelled intervention spend | 170.00 |
| new account slots consumed | 26 of 30 — 25 demo + 1 diagnostic probe |

5 of the 25 links were created by the unpaced first attempt on this same account
and were kept rather than re-created, via `--resume`. Re-creating them would have
spent 5 more of a 30-slot lifetime for no gain.

Confirmed by raw `pymongo` against the collection, with no `app/` imports, and
independently by `GET /executions?history=true`. Both agree on every figure above.

### How to reverse this

Both archives hold the deleted documents verbatim, including `_id`, so
`insertMany` restores either pass exactly. What cannot be restored is the gateway
side: the 5 orphaned links on the old account still exist but are unreachable with
the current credentials, and the 54 refused creates never produced a link to
restore.

**4 slots remain on the current account.** Checkpoint 5 cancels and pays existing
links rather than creating new ones, so this is sufficient — but it is a hard
number, and a third account would restart the provenance problem this entry
documents.

---

## 2026-08-27 — Checkpoint 5: the ~35% executed-cohort recovery rate is superseded by 46.4%, and 3 of 25 verification outcomes are a disclosed override

> **Annotation added 2026-08-27, after checkpoint 7. Nothing below is rewritten.**
> Both figures this entry reports have since been superseded. The executed-cohort
> rate reported across the whole dataset is now **39.53%** (17 of 43 link-producing
> executions); the **46.4%** below is retained only as "the ratified demo cohort".
> The **~2.4%** headline named below was a pre-execution forecast that never
> reproduced — the measured headline is **1.35%**. See the final entry in this file.

### What changed

The recovery figure ratified at checkpoint 0 was **~35% of the executed cohort**.
The number now reported is **46.4% (13 of 28 executed)**, and the earlier figure
should not be quoted anywhere alongside it.

The **~2.4% headline recovery rate remains a separate reported number** and is not
reconciled with this one, per the standing instruction. The two answer different
questions: the headline is recovered money over all revenue at risk in the dataset,
the cohort rate is recovered links over links actually executed against.

### Why it moved

Checkpoint 5 assigns each of the 25 link-carrying executions an outcome from the
`recovery_probability` its own decision record already stored, drawn against a
per-event seeded value (`sha256("s8_verify:" + event_id)`). Sum of probabilities
over the 25 links is **8.68** — the matrix's expected recovery count. The seeded
draw realised **10**, which is a normal high-side deviation, not a defect.

Three events were then **forced from not-recovered to recovered**:

| event | p | drawn u | money | role |
|---|---|---|---|---|
| `demo_002_pay` | 0.35 | 0.696 | 674.00 | `ptp_honored` |
| `demo_019_pay` | 0.45 | 0.922 | 1,298.00 | `ptp_honored` |
| `demo_150_sub` | 0.45 | 0.923 | 225.99 | `ptp_honored` |

`app/models/promise.py` permits `promised -> honored` **only when a verification
with outcome `recovered` exists** — a constraint enforced by type, not by comment.
The dataset ratifies 4 honored promises at checkpoint 6. The draw left 3 of those 4
events not-recovered, so without this override 3 of the 4 honored promises would
have been unreachable and checkpoint 6 would have had to either fabricate a
verification or quietly reduce its ratified counts.

This was caught before checkpoint 5 ran and ratified as an explicit override rather
than absorbed silently. **10/28 = 35.7% is the pure draw; 13/28 = 46.4% is what is
reported.** The override is 3 outcomes out of 25 and is listed above by name so the
delta is auditable rather than merely disclosed. Every other outcome is the
untouched draw — including `demo_035_pay` (p=0.65, u=0.676, 3,892.00), a near miss
that was left as cancelled rather than nudged.

### The honesty labelling — what is real and what is not

| tier | count | what is real |
|---|---|---|
| `genuine` | **0** | — |
| `real_state_simulated_delivery` | **8** | the cancellation happened on Razorpay; the webhook entity is the real post-cancel object, fetched back. Only the delivery is ours. |
| `simulated` | **17** | the entity is Razorpay's own, fetched live; `status` and either `amount_paid` or `expired_at` are overridden. |

**No payment in this dataset was genuinely completed.** The attempt was made and is
recorded: `demo_001_pay` → `plink_TUjgTBC2eIZVTa`, whose `short_url` returns
`HTTP 200, text/html, 6,927 bytes` — an interactive browser checkout requiring card
entry. The link's status was re-read after the attempt and was still `created`.
Razorpay exposes no server-side endpoint that pays a payment link. Therefore **all
22,105.14 of reported recovered money is simulated**, and the dashboard figure
should be presented as such.

Delivery is ours in all 25 cases because the tunnel Razorpay would post to is not
running. **If that tunnel is restarted, Razorpay's queued retries for the 8 real
cancellations may deliver a second time**, producing a duplicate row per cancelled
link with the same outcome. Dedup is on `razorpay_event_id`, and Razorpay's real
event ids differ from the synthetic ones used here (`evt_s8cancel_*`,
`evt_s8paid_*`, `evt_s8expired_*`), so dedup will not catch them.

### Final state

| | value |
|---|---|
| verifications total | 42 — 25 demo + 17 Stage 6 fixture, untouched |
| demo outcomes | 13 `recovered`, 8 `cancelled`, 4 `expired` |
| distinct demo events / event ids / link ids | 25 / 25 / 25 |
| amount mismatches | 0 |
| money recovered | **22,105.14** |
| executed-cohort recovery rate | **13/28 = 46.4%** (supersedes ~35%) |
| demo event statuses after reconcile | 13 `recovered`, 12 `recovery_failed` |
| demo executions | 28, all `completed`, unchanged |
| payment links created by checkpoint 5 | **0** — account read 26 before and 26 after |

3 executions (`demo_157_sub`, `demo_172_rcv`, `demo_173_rcv`) are templated contacts
with no payment link and are therefore unverifiable by webhook. They stay outside
the 25 and outside both recovery rates' numerators; they are in the 28 denominator.

Verified by `scripts/s8_verify.py` (15 checks, 0 failures) and independently by raw
`pymongo` with no `app/` imports. Both agree on every figure above.

### How to reverse this

`db.verifications.delete_many({"event_id": {"$regex": "^demo_"}})` removes the 25
records; the Stage 6 fixture 17 do not match that filter. The event `status`
transitions would need reverting separately. What cannot be reversed is the gateway
side: **the 8 cancellations are permanent** — a cancelled Razorpay link cannot be
un-cancelled, and cancelling never returned its lifetime slot either way. Re-running
`scripts/s8_verify.py` after a delete reproduces the same 25 outcomes exactly,
because the draw is seeded by event id and the override list is a literal.

---

## 2026-08-27 — 4 verification records deleted and re-delivered in the correct order, so a promise could precede the payment that settles it

### What was wrong

Checkpoint 5 (verification) ran before checkpoint 6 (promise-to-pay). In reality the
order is the other way round: a customer commits to pay, and *then* the money
arrives. Running them backwards made the four `ptp_honored` events
(`demo_002_pay`, `demo_019_pay`, `demo_108_chk`, `demo_150_sub`) reach status
`recovered` before any promise existed against them.

`app/ptp/store.py:NON_PROMISABLE_STATUSES` is `TERMINAL_EVENT_STATUSES`, so
`POST /promises` refuses a terminal event with 422 `EventSettled` — "you cannot
promise to pay money that has already arrived". That guard is correct. With it in
force and the payments already recorded, **the `honored` state was unreachable for
every event in the dataset**: honoring requires a `recovered` verification, and any
event that has one is terminal.

This is a sequencing error in Stage 8's own checkpoint order, not a pipeline defect.
No `app/` code was changed.

### What was done

For each of the four events, in this order:

1. the verification record was archived verbatim to
   `.s8_archive/verifications_resequenced_20260827T102709Z.json` (4 documents,
   2,302 bytes), read back, and only then deleted;
2. the event's status was returned to `at_risk` by a raw Mongo `$set`;
3. `POST /promises` recorded the commitment, moving the event to
   `awaiting_promise` through the ordinary guarded transition;
4. the **same** paid webhook was re-delivered, reusing the **same
   `razorpay_event_id`**, writing the verification back and moving the event
   `awaiting_promise -> recovered` — a transition the status table already permits;
5. `POST /promises/{event_id}/check` found the payment and honored the promise.

The raw `$set` in step 2 is the only unguarded write, and it is deliberate:
`transition_event_status` cannot move an event out of `recovered` because that state
is terminal by declaration, which is right — money does not un-arrive. What is being
corrected is the order two checkpoints ran in, not a business fact.

### What changed in the data, and what did not

| | before | after |
|---|---|---|
| verifications total | 42 | **42** |
| demo verifications | 25 | **25** |
| demo outcomes | 13 recovered, 8 cancelled, 4 expired | **identical** |
| money recovered | 22,105.14 | **22,105.14** |
| `razorpay_event_id` values | 25 distinct | **the same 25** |
| `verified_at` on 4 records | ~10:00 | **~10:27, after the promise** |

Nothing else moved. The `_id` of those four verification documents changed, because
they are new documents; the archive holds the originals if the old ids are ever
needed.

### The alternative that was rejected

Revert the status, record the promise while the money was demonstrably already in,
then set the status back to `recovered`. This is fewer writes and touches no
verification — and it was rejected, because it does not re-order anything. It just
defeats `assert_event_promisable`, producing exactly the record that guard exists to
prevent: a commitment to pay money the system had already confirmed receiving.
Circumventing a guardrail to make a demo number appear is worse than deleting and
rewriting a record whose content is unchanged.

### A related decision: the 3 chased events were not pre-authorized

Checkpoint 6's scope said to authorize `demo_174_rcv`, `demo_175_rcv`,
`demo_176_rcv` through the ordinary gate. Doing that as a separate
`POST /authorize/{event_id}` call **would have made all three unchaseable**.
`app/policy/store.py:prior_authorized_contacts` counts an authorized contact verdict
as a reservation even when nothing has executed against it, anchoring the cooldown at
`evaluated_at`. A verdict minted now would put a fresh 24h cooldown on each event, and
the follow-up moments later would be blocked — yielding 5 suppressed promises and 0
chased, leaving `broken -> reevaluating` dead in the demo.

So the authorization for those three is the one `app/ptp/service.py:send_follow_up`
performs, which calls `authorize_event` — the same function `POST /authorize/{event_id}`
is, not a reimplementation. The gate ran on the ordinary code path and wrote an
ordinary appended verdict (`v1`, `authorized`, reason `ok`) for each. This is the only
sequence in which both the 3 chased and the 2 suppressed records are reachable.

### How to reverse this

`insertMany` the archived documents restores the original four verifications,
including their `_id`s; the four written in their place are identifiable by
`event_id` plus a `verified_at` later than 10:27 UTC. The eleven promises are
`db.promises.delete_many({"event_id": {"$regex": "^demo_"}})` — the 9 Stage 6
fixture promises do not match. The 3 follow-up executions and the 5 verdicts written
by the gate during this checkpoint are ordinary pipeline records and would need
clearing the same way checkpoint 4's were, via `scripts/s8_supersede.py`.

---

## 2026-08-27 — ratified figures, NOT a correction to data: the headline rate is 1.35%, the executed-cohort rate is 39.53%, and `~2.4%` was never a measurement

No document was written, edited or deleted for this entry, and no code in `app/` was
touched. Two figures that had been quoted in earlier entries and in Stage 8 script
output were ratified against the full 305-event dataset after checkpoint 7, and this
entry records what they are, where the superseded ones came from, and why one of them
was never reproducible.

### The two reported figures, as ratified

| figure | value | numerator / denominator |
|---|---|---|
| headline recovery rate | **1.35%** | 29,605.14 recovered / 2,187,218.02 at risk, all 305 events |
| executed-cohort recovery rate | **39.53%** | 17 recovered / 43 link-producing executions, all 305 events |

Both are measured, not forecast. Both were independently reproduced from raw
`pymongo` with no `app.metrics` imports at checkpoint 7 (104 checks, 0 failures).
**They remain two separate reported numbers and are not reconciled into one**, per the
standing instruction: the headline divides real recoveries by the whole portfolio, the
cohort rate divides them by the executions that were actually actioned.

### `~2.4%` was a pre-execution forecast, and it is arithmetically unreachable

The figure originates in `scripts/s8_dryrun.py`, section 9 ("FORECAST DASHBOARD
IMPACT"). Its numerator is `expected_recovered` — the sum of `amount x
recovery_probability` over the link-carrying executions the batch *intended* to
create — plus the 7,500.00 that was already real. **No money in that numerator had
moved.** The script has now been amended to say so on the same line it prints the
number, so it cannot be lifted out of that output and quoted as a measurement again.

It never reproduced in actual data at any point, on any cohort:

| cohort | events | at risk | recovered | rate |
|---|---|---|---|---|
| earlier fixture data | 105 | 744,127.75 | 7,500.00 | 1.01% |
| Stage 8 demo batch | 200 | 1,443,090.27 | 22,105.14 | 1.53% |
| **all** | **305** | **2,187,218.02** | **29,605.14** | **1.35%** |

This is stronger than "not observed". The combined rate is a money-weighted mixture
of the two cohorts, so it cannot exceed the higher component rate of 1.53%. **No
partition of this dataset yields 2.4%**, and no future execution of the existing plan
could have produced it either — the forecast was built over a 56-event execution plan
that the Razorpay test account's 30-link lifetime ceiling later forced down to 28.
Every reference to `~2.4%` is superseded by **1.35%**, or where a qualifier helps,
"1.35% at the current build's full dataset".

### Why 39.53% is the primary executed-cohort figure, and not the other three

Four readings are defensible, and they were enumerated rather than one being picked
silently. All four are measured; they differ only in denominator:

| reading | value | denominator |
|---|---|---|
| **all events, link-producing executions only** | **17 / 43 = 39.53%** | executions that created a Razorpay artifact |
| all events, every execution | 17 / 60 = 28.33% | every completed execution, contacts included |
| demo cohort, link-producing only | 13 / 25 = 52.00% | as above, Stage 8 demo events only |
| demo cohort, every execution | 13 / 31 = 41.94% | as above, Stage 8 demo events only |

39.53% is ratified as the primary reported figure because it is the only
apples-to-apples one across the whole dataset. A contact-type intervention
(`reminder`, `escalating_reminder_sequence`, `manual_escalation`) produces no payment
link, so **no webhook can ever report a recovery against it** — its recovery rate is
structurally zero rather than measured, which is already flagged per row by
`verifiable: false` on `GET /metrics/by-intervention`. Leaving those 17 executions in
the denominator does not make the number more conservative, it makes it
uninterpretable: it mixes interventions that could fail with interventions that
cannot succeed.

### What happens to 46.4% and ~35%

**46.4% (13 of 28) may be quoted only as "the ratified demo cohort", and is not the
headline.** It was correct when ratified at checkpoint 5 and it has since drifted for
a benign reason worth recording: checkpoint 6 wrote 3 additional follow-up executions
against demo events, so the same 13 recoveries now sit over 31 demo executions rather
than 28 — 41.94%. The 46.4% denominator is a snapshot of a cohort that has grown, not
a figure that was wrong.

**~35% is superseded twice over and should not be quoted at all.** The 2026-08-27
checkpoint-5 entry above already superseded it with 46.4%; 46.4% is now itself demoted
to an aside.

### Corresponding update to the 2026-08-26 open finding

The entry "eleven recovered verification records describe four payments" is now
numerically historical. At 305-event scale the same phenomenon reads **24 recovered
verification records describing 17 distinct payments**, with 7 records ignored as
duplicates; the literal sum would be 44,605.14 against a deduped 29,605.14. The two
duplicate groups are unchanged in kind: `exe_S5ADV_20260825T045458_HONEST` (n=6) and
`exe_S5_20260825T042248_DRETRY` (n=3), with every amount internally consistent inside
each group, so which record survives dedup is immaterial to the total. The dedup rule
in `app/metrics/reader.py:distinct_recoveries` is unchanged and correct; only the
example counts in its docstring are stale, and they were left in place because Stage 8
is under an explicit instruction not to modify `app/`.

### What was changed by this entry

Documentation and Stage 8 tooling only:

| file | change |
|---|---|
| `scripts/s8_verify.py` | module docstring and the override output now state that 13/28 is the demo cohort, not the headline, and name 39.53% as the whole-dataset figure |
| `scripts/s8_dryrun.py` | the forecast headline it prints is now labelled as a forecast on the same line, with the measured 1.35% stated beside it |
| `docs/data-corrections.md` | this entry |

**There is no `README.md` in this repository yet, and no pitch-material file.** The
instruction to replace `~2.4%` "in the README and any pitch material" therefore has no
target to edit. The requirement carries forward to first authoring instead: the README
must state **1.35%** as the headline recovery rate and **39.53%** as the executed-cohort
rate, keep them as two separate numbers, and must not quote `~2.4%` or `~35%` at all.
It must also carry checkpoint 5's disclosure that **no payment in this dataset was
genuinely completed** — all recovered money is simulated on real Razorpay link objects.

### How to reverse this

Revert the two script edits; nothing else was touched. No database document, index or
collection was read or written in producing these figures beyond the read-only
`GET /metrics/*` calls, which were confirmed byte-for-byte non-mutating at checkpoint 7
by SHA-256 fingerprinting all 8 collections before and after.

---

## 2026-08-28 — Stage 9: one verification record written by an adversarial probe, deleted, and a terminal event status walked back by direct write

### What was wrong

This is a **test-design error of mine, not a defect in Stage 9's code**. The endpoint
did exactly what it is specified to do; the input I sent it was valid, and I had
assumed it was not.

Adversarial case G15 sent `{"amount_recovered": "100.00"}` — a numeric *string* — to
`POST /executions/6a8fff171cbbe8161ee8469f/confirm-payment`, expecting a 422 on the
type. Pydantic v2 in lax mode coerces `"100.00"` to `100.0`, so the body was valid,
and the endpoint correctly:

- wrote verification `6a9128b41f4ad9695801ba04` asserting 100.00 against an expected
  2,218.95, flagged `amount_mismatch: true` and logged the mismatch at WARNING;
- transitioned `demo_172_rcv` from `awaiting_promise` to `recovered`.

The compounding mistake was where I pointed the probe. The malformed-body cases were
aimed at `6a8fff171cbbe8161ee8469f`, a live Stage 9 demonstration target, on the
assumption that every one of them would be refused before reaching the database. Three
of the fifteen cases carried a body that could in principle validate; one did.

### What was done

1. the record was archived verbatim to
   `.s9_archive/accidental_manual_confirmation.json` (723 bytes), including the
   event's status before and after the probe, and read back before anything was
   deleted;
2. verification `6a9128b41f4ad9695801ba04` was deleted by raw Mongo `delete_one`;
3. `demo_172_rcv` was returned to `awaiting_promise` by a raw Mongo `$set`.

The test method was corrected as well as the data. Malformed-body probes are now
aimed at a **link-producing** execution, which the allowlist refuses at step 5
regardless of what the body contains. That makes a write structurally impossible
during body-validation testing, and it also makes the two outcomes legible: 422 means
the body was refused, 409 means the body validated and something downstream refused
it. A probe suite whose safety depends on every case failing is a suite with no
margin; this one cannot write even if a case unexpectedly validates.

### Why step 3 had to be a direct write

`app/models/events.py:ALLOWED_STATUS_TRANSITIONS` gives `recovered` no outgoing
edges — it is in `TERMINAL_EVENT_STATUSES`. `transition_event_status` therefore
cannot walk an event out of it, by design: the guard exists so that recovered money
cannot be un-recovered through the API. Reversing an accidental entry into a terminal
state has no guarded path and cannot have one. The direct write is disclosed here for
that reason, and it is the only write in Stage 9 that bypassed application code.

### What changed in the data, and what did not

Restored: 42 verification documents, 0 with `source: "manual_confirmation"`,
`demo_172_rcv` at `awaiting_promise`. Confirmed by count and by status read before the
three deliberate confirmations were made.

Not affected: the promise on `demo_172_rcv` was untouched throughout and stayed
`broken` — `POST /promises/{event_id}/check` was never called during the probe, so no
promise state was derived from the accidental record. The event was later confirmed
deliberately, for 2,218.95 with `amount_mismatch: false`, as verification
`6a9129fd0dfca556c4d76483`.

### The change this prompted, ratified 2026-08-28

`amount_recovered` is the one field on `ManualPaymentConfirmation` that cannot be
re-derived from anything else in the system — `amount_expected` comes from the verdict
chain, `event_id` from the execution, `confirmation_id` from the execution id, `source`
and `confirmed_by` are constants. Everything else can be checked against a second
source; this cannot, so it has to arrive exactly as sent.

It now carries `strict=True`, and it is the only strict field in
`app/models/verification.py`. Under lax mode it accepted `"100.00"`, `"1e3"`, `" 100 "`
and `true` as money; all four are now 422 `float_type`. A JSON integer is still
accepted, because a whole-rupee `2218` is a legitimate amount — verified over HTTP as
part of the change, along with `gt=0` still being enforced beneath the strictness.

The strictness is on the request model only, not on `ManualVerification`. The request
model is the trust boundary; the record model's job is the arithmetic invariants, and
it also parses documents back out of MongoDB, where numeric types are BSON's business
rather than a caller's.

### How to reverse this

There is nothing to reverse — this entry documents a reversal already performed. To
*restore* the accidental record, re-insert the `verification` object from
`.s9_archive/accidental_manual_confirmation.json` and `$set` `demo_172_rcv` to
`recovered`. Doing so would collide with the deliberate confirmation on the same
execution: `uniq_confirmation_id` is unique and both records carry
`manual_conf_6a8fff171cbbe8161ee8469f`, so the insert would be refused. That collision
is the partial unique index from Stage 9's own migration doing its job.
