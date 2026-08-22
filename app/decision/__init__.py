"""Stage 3 — Decision.

Scores each candidate intervention by Expected Recovery Value:

    ERV = (amount at risk) x (probability of recovery) - (cost of action)

and selects the highest-ERV option from a FIXED, bounded list of allowed
interventions. The LLM cannot introduce a new action type here.
"""
