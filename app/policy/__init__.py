"""Stage 4 — Policy.

The authorizing half of the recommend/authorize split. Stage 3 produces a
`Decision` ("this is the best action"); this stage produces a `PolicyVerdict`
("this action is / is not permitted"). They are separate packages, separate
collections, and separate models, so the split is a fact about the code rather
than a claim about it.

Three things are true of everything in here:

* No LLM. Policy never reasons in natural language; `app/policy/engine.py` holds
  no model client and the rules are constants and comparisons.
* No execution. A `PolicyVerdict` has no field that can express that anything
  happened. Doing the authorized thing is Stage 5.
* Nothing is short-circuited. Every check is evaluated and recorded on every
  verdict, so a refusal shows its whole evaluation trail.
* Every verdict records which rulebook judged it. The parameters are a versioned,
  fingerprinted value (`app/policy/rulebook.py`) passed into the engine, not
  constants it reads, so an old verdict can be re-derived under the policy that
  actually produced it.
"""

from app.models.policy import (
    ALLOWED_REASONS,
    ALLOWED_VERDICTS,
    CHECK_FOR_REASON,
    FINGERPRINT_DIGEST_CHARS,
    FINGERPRINT_PATTERN,
    FINGERPRINT_SCHEME,
    POLICY_CHECKS,
    REASON_PRECEDENCE,
    REASON_VERDICT,
    UNATTESTED_FINGERPRINT_SOURCES,
    RulebookFingerprintSource,
)
from app.policy.engine import (
    MODEL_ENFORCED_FIELDS,
    CheckOutcome,
    PolicyContext,
    UnreproducibleRulebook,
    assert_applicable,
    evaluate,
    run_checks,
)
from app.policy.rulebook import (
    HASHED_FIELDS,
    SUPERSEDED_RULEBOOKS,
    Rulebook,
    canonical_form,
    fingerprint_of,
)
from app.policy.rules import (
    AUTO_AUTHORIZE_BELOW,
    CONTACT_INTERVENTIONS,
    COOLDOWN,
    COOLDOWN_HOURS,
    MAX_CONTACTS_PER_EVENT,
    MINIMUM_ERV,
    NEVER_AUTO_AT_OR_ABOVE,
    ZERO_COST_EXEMPT_FROM_ERV_FLOOR,
    AutonomyTier,
    current_fingerprint,
    current_rulebook,
    erv_floor_applies,
    is_contact_intervention,
    rulebook_registry,
    tier_for,
)
from app.policy.store import (
    AUTHORIZED_INDEX,
    COLLECTION_NAME,
    OPT_OUT_COLLECTION_NAME,
    OPT_OUT_INDEX,
    VERSION_INDEX,
    DanglingDecisionReference,
    DecisionReferenceError,
    StaleDecisionReference,
    add_opt_out,
    append,
    ensure_indexes,
    gather_context,
    is_opted_out,
    latest_version,
    list_opt_outs,
    list_verdicts,
    prior_authorized_contacts,
)

__all__ = [
    "ALLOWED_REASONS",
    "ALLOWED_VERDICTS",
    "AUTHORIZED_INDEX",
    "AUTO_AUTHORIZE_BELOW",
    "CHECK_FOR_REASON",
    "COLLECTION_NAME",
    "CONTACT_INTERVENTIONS",
    "COOLDOWN",
    "COOLDOWN_HOURS",
    "FINGERPRINT_DIGEST_CHARS",
    "FINGERPRINT_PATTERN",
    "FINGERPRINT_SCHEME",
    "HASHED_FIELDS",
    "MAX_CONTACTS_PER_EVENT",
    "MINIMUM_ERV",
    "MODEL_ENFORCED_FIELDS",
    "NEVER_AUTO_AT_OR_ABOVE",
    "OPT_OUT_COLLECTION_NAME",
    "OPT_OUT_INDEX",
    "POLICY_CHECKS",
    "REASON_PRECEDENCE",
    "REASON_VERDICT",
    "SUPERSEDED_RULEBOOKS",
    "UNATTESTED_FINGERPRINT_SOURCES",
    "VERSION_INDEX",
    "ZERO_COST_EXEMPT_FROM_ERV_FLOOR",
    "AutonomyTier",
    "CheckOutcome",
    "DanglingDecisionReference",
    "DecisionReferenceError",
    "PolicyContext",
    "Rulebook",
    "RulebookFingerprintSource",
    "StaleDecisionReference",
    "UnreproducibleRulebook",
    "add_opt_out",
    "append",
    "assert_applicable",
    "canonical_form",
    "current_fingerprint",
    "current_rulebook",
    "ensure_indexes",
    "erv_floor_applies",
    "evaluate",
    "fingerprint_of",
    "gather_context",
    "is_contact_intervention",
    "is_opted_out",
    "latest_version",
    "list_opt_outs",
    "list_verdicts",
    "prior_authorized_contacts",
    "rulebook_registry",
    "run_checks",
    "tier_for",
]
