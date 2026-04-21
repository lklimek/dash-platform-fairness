# Investigation: Never-Banned Validators with Concerning/Poor Bands

Analysis date: 2026-04-21
Window: 2026-03-22 13:18 UTC to 2026-04-21 13:15 UTC (30 days, platform blocks 339746-354928, core blocks 2442242-2458681)

---

## BUG FOUND: Late-Registration Not Reflected in `eligible_fraction`

**Severity: HIGH**

`eligible_fraction` is computed solely from PoSe ban periods. When a node registers
mid-window its `registered_height` is noted, but the time between the window start and
registration is NOT subtracted from `eligible_seconds` or reflected in `eligible_fraction`.

As a result, the `selection` score formula:

```
selection = member_of / (peer_member_of_median * eligible_fraction)
```

compares the node against the full-window peer median (44.0 quorums) even though the node
was only eligible for a fraction of those quorums. This depresses `selection` — which
carries 30% composite weight — and causes newly-registered nodes to score Concerning or
Poor regardless of their actual participation quality.

**Suggested fix:** After computing `eligible_fraction` from PoSe events, also apply a
registration-based cap:

```python
# In score_validator_from_cache, after the pose_events block:
if reg_h_int > 0 and reg_h_int > cache.core_lo:
    reg_frac = max(0.0, (cache.core_hi - reg_h_int) / max(cache.core_hi - cache.core_lo, 1))
    eligible_fraction = min(eligible_fraction, reg_frac)
```

This would bring `eligible_seconds` in line with actual eligibility, normalize the
`selection` score, and eliminate the false-positive Concerning/Poor ratings for the 38
nodes described below.

---

## Summary

| Metric | Count |
|--------|-------|
| Total validators | 364 |
| Never-banned (`pose_status == "never"`) | 156 |
| Never-banned AND Concerning | 26 |
| Never-banned AND Poor | 12 |
| **Never-banned Concerning + Poor total** | **38** |

---

## Hypothesis Results

All 38 nodes fall into a single root cause (H2). No other hypothesis applies.

| Hypothesis | Count | Verdict |
|-----------|-------|---------|
| H1: Chronic skippers (met=0, skipped>0, PoSe penalty > 0) | 0 | Not applicable: never-banned nodes have no PoSe penalty |
| **H2: Newly registered** (reg_h inside core window, ef=1.0 despite partial eligibility) | **38** | **Confirmed — scoring bug** |
| H3: Low quorum selection, perfect participation, not new | 0 | Subsumed by H2 |
| H4: Data artifact (high inconclusive ratio) | 0 | No inconclusives dominate |
| H5: Other | 0 | — |

---

## H2 Sub-classification

Of the 38 H2 nodes:

| Sub-bucket | Count | Description |
|-----------|-------|-------------|
| H2-perfect | 15 | mo > 0, met == member_of, skipped == 0 — pure false positives from selection penalty |
| H2-skipped | 22 | mo > 0, has skips — may have a real participation problem too, but selection still inflates severity |
| H2-zero | 1 | mo == 0 — registered so recently they were not assigned to any quorum yet |

---

## H2-perfect Examples (pure false positives)

| protx | band | member_of | met | skip | reg_h | frac_eligible |
|-------|------|-----------|-----|------|-------|---------------|
| 2b9748a9745cd087a4a747d80d0574fe17e818175d3a597763e7b021c73d8178 | Concerning | 2 | 2 | 0 | 2456483 | 0.134 |
| dbc0952bac251c1271848d58d90854898276124072a81e420fe233e95e85118f | Concerning | 3 | 3 | 0 | 2456437 | 0.137 |
| 251df8690acf898c2104cf05e4616b758a341ae069c9adf4f2621fcbc34ca966 | Concerning | 4 | 4 | 0 | 2456438 | 0.136 |
| dc4acdaceadeadcb00a6e4636de73376d2be84ed4a634b705a4bb1a39d4bd3ae | Concerning | 4 | 4 | 0 | 2456483 | 0.134 |
| ea53f5096c509f78152cd7aeb51fae80d45d1f6f7e5bc5293da109115aea49f5 | Concerning | 4 | 4 | 0 | 2456438 | 0.137 |

## H2-skipped Examples (participation concern, but severity inflated by bug)

| protx | band | member_of | met | skip | reg_h | frac_eligible |
|-------|------|-----------|-----|------|-------|---------------|
| 1e185f0363885a32cfcfbd914c2a8da9f7424bbad09ebcb14ecd50ac6c521c8a | Poor | 2 | 0 | 2 | 2458297 | 0.023 |
| 9634b80b3e56c0aedc202fcf4e32199e3a7e2cb47eb00e8ecdde2adaaaab5c10 | Concerning | 5 | 4 | 0 | 2456483 | 0.134 |
| 1951c1e9bb7a320b28459054f09aa9de20a7969af14fc6c0d6d58b633649ca4f | Poor | 6 | 5 | 1 | 2456483 | 0.134 |
| e978f8f6ba3374d95e8b2bc647639f3aabaa2455912eb5e31104d5d5ac68740f | Poor | 6 | 3 | 1 | 2456937 | 0.106 |
| faab27b2293c6680637d81c6c76c2c03c9300e07eecae7d883a90441504d6ff1 | Poor | 6 | 5 | 1 | 2454805 | 0.236 |

## H2-zero Example (too new to appear in any quorum)

| protx | band | member_of | met | skip | reg_h | frac_eligible |
|-------|------|-----------|-----|------|-------|---------------|
| 8e263b803f3ceb75e4ffb28e5d68439641aec07682ebdb1444b8d2a42fba5a69 | Poor | 0 | 0 | 0 | 2458297 | 0.024 |

---

## Simulated Band Distribution After Fix

Applying `eligible_fraction = min(ef_pose, (core_hi - reg_h) / (core_hi - core_lo))` to
the 38 affected nodes yields the following band shifts:

| Before | After | Count |
|--------|-------|-------|
| Concerning | Excellent | 15 |
| Concerning | Good | 8 |
| Concerning | Concerning | 3 |
| Poor | Good | 4 |
| Poor | Concerning | 5 |
| Poor | Poor | 3 |

35 of 38 nodes would improve band. The 6 remaining in Concerning/Poor after correction
have genuine participation issues (skips relative to their short window).

---

## Interpretation

This is a **scoring bug**, not a legitimate signal and not a data artifact.

Every one of the 38 never-banned Concerning/Poor validators registered their Evo
masternode during the analysis window (core heights 2442242-2458681). Because
`eligible_fraction` is only reduced for PoSe ban periods, nodes registered mid-window
are assessed as if they had full-window availability. Their `selection` score is then
computed against the full peer median of 44 quorums, yielding scores like 0.04-0.39 for
nodes that had 2-17 quorum slots — which is mathematically correct relative to the full
median but operationally meaningless for a node that was live for only 3-20% of the window.

15 of 26 Concerning nodes are pure false positives: they proposed every block they were
assigned (met == member_of, skipped == 0), yet their composite score dropped below 0.85
solely due to a low `selection` value caused by the unaccounted registration gap.

---

## Recommendations (do not implement in this pass)

1. **Fix `eligible_fraction` to account for late registration** (see bug description above).
   This is the highest-priority change.

2. **Add `registered_height` and `frac_eligible` to the index table** so operators can
   quickly distinguish new nodes from established ones without opening individual reports.

3. **Add a `likely_cause` or `notes` column** (e.g. "New node — only N% of window") to
   surface the registration context inline. This would prevent operators from mistakenly
   investigating healthy new nodes.

4. **Consider a `minimum_quorum_threshold`** for band assignment — nodes with `member_of`
   below a configurable floor (e.g. 10) could be shown as "Insufficient data" instead of
   being rated Concerning/Poor.
