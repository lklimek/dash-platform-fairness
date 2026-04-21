# platform-fairness

Dash Platform validator fairness score — a single-file Python CLI that
estimates, over a configurable time window, how fairly an Evo (HPMN)
validator was treated by the quorum selection process and how reliably
it behaved when chosen.

Two modes:

- **single-target** — score one validator; produce per-validator JSON + HTML.
- **`--all-platform`** (v0.2.0) — score every registered Evo masternode in
  one pass, share the expensive block/quorum work, and publish an
  interactive `index.html` with a scatter chart + sortable table.

## Requirements

- Python 3.10+ (stdlib only; no pip install)
- A reachable Tenderdash RPC endpoint (`http://127.0.0.1:26657` by default)
- A working `dash-cli` invocation (auto-detected: the dashmate mainnet container
  first, then a plain `dash-cli` on PATH). Override via `--core-cmd`.

## Usage

```bash
# Default 30-day window, peer-relative scoring (slowest, most accurate)
python3 fairness.py <protx_hash>

# Fast absolute-threshold pass
python3 fairness.py <protx_hash> --days 7 --skip-peer-scan

# Batch every Evo MN — writes per-validator files + summary.json + index.html
python3 fairness.py --all-platform --days 30

# Incremental re-run: skip validators whose existing report matches the window
python3 fairness.py --all-platform --days 30 --resume

# Remote Tenderdash / custom core
python3 fairness.py <protx_hash> \
    --tenderdash-url http://10.0.0.5:26657 \
    --core-cmd 'docker exec my_core_container dash-cli' \
    --out-dir ./out

# JSON only (for scripting)
python3 fairness.py <protx_hash> --days 30 --json-only
```

Exits 0 on success; writes the JSON path (and HTML path) to stdout.

## Scoring (v0.2.0)

Three axes combined with default weights `selection=0.30, participation=0.50,
liveness=0.20`:

| Axis          | Definition                                                              |
|---------------|--------------------------------------------------------------------------|
| selection     | `member_of / (peer_member_of_median * eligible_fraction)` (≤1)          |
| participation | `met / (met + skipped)` across quorums where the target was a member    |
| liveness      | `1 − round_misses_on_target / (met + skipped)`                          |

Bands: ≥0.95 Excellent, 0.85–0.95 Good, 0.70–0.85 Concerning, <0.70 Poor.

In `--all-platform` mode the peer median is derived from the batch's own
aggregated per-validator `member_of` and participation rates. Validators
with zero (met + skipped) are excluded from the participation median;
validators with zero `member_of` are excluded from the `member_of` median.

## `--all-platform` pipeline

1. Enumerate Evo masternodes via `protx list evo true` (type = `Evo`, no
   Regular MNs).
2. Build a shared window cache once:
   - Tenderdash `/status` + `/block?height=H` for every H in the window.
   - Tenderdash `/validators?height=<sub_run_lo>` per unique `validators_hash`.
   - Core `quorum info 4 <qh>` per unique LLMQ.
3. Per validator, derive eligibility + target-specific per-quorum status,
   and render per-validator JSON + HTML.
4. Aggregate batch medians; re-score every validator against those medians.
5. Write `reports/summary.json` and `reports/index.html` (scatter chart +
   sortable table, self-contained, no CDNs).

Typical runtime on mainnet (~13k blocks, ~360 Evo MNs): 4-8 minutes total,
of which ~30 s is the shared cache pass and the rest is per-validator PoSe
bisection. Validators with no PoSe history take a fast path that skips the
historical `protx info` walk entirely.

## summary.json shape

```json
[
  {
    "protx": "0EBF...3E96",
    "pose_status": "revived_in_window",
    "member_of": 29,
    "met": 27,
    "skipped": 2,
    "inconclusive": 0,
    "composite": 0.9059,
    "band": "Good",
    "report_html": "0ebfbb9b_20260421T132225Z.html",
    "report_json": "0ebfbb9b_20260421T132225Z.json"
  },
  ...
]
```

`pose_status` values:

| value                   | meaning                                                  |
|-------------------------|----------------------------------------------------------|
| `never`                 | No `PoSeRevivedHeight` and no bans detected in window    |
| `revived_before_window` | Historical revivals but none inside the window           |
| `revived_in_window`     | ≥1 revive event falls inside the window                  |
| `currently_banned`      | `PoSeBanHeight > PoSeRevivedHeight` at the latest tip    |

## index.html

Self-contained single HTML file. No external assets (no Chart.js, no D3,
no fonts) — only embedded CSS + vanilla-SVG scatter chart. Loads
`summary.json` at page load via `fetch('summary.json')`. Supports
`prefers-color-scheme` dark/light mode. Click a dot to open the
validator on `platform-explorer.com`; click a table row link to open the
individual HTML report.

## Caveats / scope

- **BLS threshold signature** — this tool does NOT verify the BLS-67 aggregate
  signature on each `last_commit`. It reasons purely about proposer rotation
  and round-failure attribution from block headers.
- **Precommit axis is stubbed out.** Whether the target's individual precommit
  signature actually made it into the aggregate requires parsing drive-abci
  debug logs or a validator-signing instrumentation RPC — left for a later
  release (`TODO(precommit)` in the source).
- **Proposer rotation sanity check.** The algorithm assumes Tenderdash's
  deterministic rotation: sorted-ascending eligible-validator list from
  `/validators?height=<sub_run_lo>`, advancing by `1 + rounds_consumed_at_H`
  per block. Target membership is checked against Core's canonical DKG
  roster (`quorum info <type> <hash>`), so a transiently banned member
  still counts as a member of the LLMQ it was selected into.
- **PoSe replay** walks the target's state via historical
  `protx info <hash> <blockHash>` queries, bisecting on the `(PoSeBanHeight,
  PoSeRevivedHeight)` tuple. Batch mode shares a single block-hash cache
  across validators and short-circuits clean (never-banned) nodes.
- **Boundary quorums** at the edges of the window are detected by extending
  outward until a `validators_hash` change is found; if the chain ends are
  reached first, those quorums are marked `INCONCLUSIVE`.

## Output files

Single-target:
```
reports/<first-8-hex>_<YYYYMMDDThhmmssZ>.json
reports/<first-8-hex>_<YYYYMMDDThhmmssZ>.html
```

Batch (`--all-platform`):
```
reports/<first-8-hex>_<YYYYMMDDThhmmssZ>.json     (one per Evo MN)
reports/<first-8-hex>_<YYYYMMDDThhmmssZ>.html     (one per Evo MN)
reports/summary.json
reports/index.html
```

The HTML files embed the data in a `<script type="application/json">`
block; no external fonts, images, or scripts are loaded. They render
offline.

## Development notes

The tool is deliberately single-file; no dependencies outside the standard
library. Format with `ruff format fairness.py`; lint with `ruff check
fairness.py`.
