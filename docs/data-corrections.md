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
