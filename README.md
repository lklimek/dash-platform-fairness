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

1. Enumerate Evo masternodes via `protx list evo true` at **both** the
   window-start (`core_lo`) and tip (`core_hi`) heights and take the
   union. This covers mid-window registrations and deregistrations that
   would otherwise be invisible. For MNs present at `core_lo` but gone
   at tip, bisect the deregistration height (cheap — reuses the shared
   block-hash cache).
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

Top-level `window` block:

```json
{
  "window": {
    "days": 30,
    "from_time": "...",
    "to_time": "...",
    "platform_range": [h_start, h_tip]
  },
  "validators": [ ... ]
}
```

`pose_status` values:

| value                      | meaning                                                                 |
|----------------------------|-------------------------------------------------------------------------|
| `active_whole_window`      | Fully eligible throughout the window (no bans or revives inside it)     |
| `revived_in_window`        | ≥1 revive event falls inside the window                                 |
| `currently_banned`         | `PoSeBanHeight > PoSeRevivedHeight` at the latest tip                   |
| `registered_in_window`     | `registeredHeight` falls inside the window (new MN during this window)  |
| `deregistered_in_window`   | MN was in `protx list evo` at `core_lo` but absent at tip               |

Classification precedence (first match wins):
`deregistered_in_window` → `currently_banned` → `revived_in_window` →
`registered_in_window` → `active_whole_window`.

The last two categories (`registered_in_window`, `deregistered_in_window`)
are **eligibility-limited** buckets: those validators were live for only
part of the window. Their `selection` axis is normalised via
`eligible_fraction`, but their raw `member_of` / `met` counts aren't
directly comparable to the performance buckets. The index scatter chart
places them in a separate visual section for exactly this reason.

Per-validator JSON adds `eligibility.deregistered_core_height` (null for
MNs still registered at tip; the core height at which the MN was removed
from `protx list evo` otherwise).

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

By default, stale per-validator reports from prior runs are removed at the start of each batch. Use `--keep-history` to preserve them for comparison across runs.

The HTML files embed the data in a `<script type="application/json">`
block; no external fonts, images, or scripts are loaded. They render
offline.

## Deployment (Cloudflare Pages)

Two scripts wrap the publish flow:

- **`deploy.sh`** — pushes the current `reports/` directory to a
  Cloudflare Pages project via `wrangler pages deploy`. Auto-installs
  `wrangler` via `npm` if missing, pre-creates the Pages project
  idempotently on first run, loads `CLOUDFLARE_API_TOKEN` from `.env`
  if present.
- **`run.sh`** — cron-friendly wrapper: regenerate batch, then deploy.
  Flock-based concurrency guard, timestamped logging under `logs/`,
  `--dry-run` / `--skip-batch` / `--skip-deploy` flags. See
  `./run.sh --help` for cron examples.

### Cloudflare API token permissions

Create a scoped token at <https://dash.cloudflare.com/profile/api-tokens>
(**Create Token → Custom token**) with these permissions:

| Section | Resource | Access |
|---------|----------|--------|
| Account | Cloudflare Pages | Edit |
| User    | Memberships      | Read |
| User    | User Details     | Read |
| Zone    | Zone             | Read *(only if attaching a custom domain)* |

Resource scopes:

- **Account Resources** → *Include* → your specific account
- **Zone Resources** → *Include → Specific zone* → your domain *(only if
  attaching a custom domain)*

The Pages-only token that some guides suggest (`Account → Cloudflare
Pages → Edit` alone) is insufficient: `wrangler` also reads `User →
Memberships` and `User → User Details` during auth, and returns
`Authentication error [code: 10000]` or
`NoDefaultValueProvided` if those scopes are missing.

If you plan to use a custom domain on top of the default
`*.pages.dev` URL, also add `Zone → Zone → Read` and include the
target zone in *Zone Resources*.

### IP allowlist gotcha

Cloudflare tokens can be restricted to specific source IPs. If the
deploy runs from a different machine than the one the token was minted
on (e.g. a VPS, CI runner, relocated laptop), the API call fails with:

```
Cannot use the access token from location: <IP>  [code: 9109]
```

Fix: **Dash → API Tokens → (your token) → Edit** — add the deploy
machine's egress IP to *IP Address Filtering*, or remove the
restriction.

### Quickstart

```bash
cp .env.example .env
# Edit .env: paste your cf_pat_... token as CLOUDFLARE_API_TOKEN
./deploy.sh --dry-run          # smoke-test token + project resolution
./deploy.sh                    # first real deploy
```

Subsequent deploys hit the same `*.pages.dev` URL. First run
auto-creates the Pages project.

## Development notes

The tool is deliberately single-file; no dependencies outside the standard
library. Format with `ruff format fairness.py`; lint with `ruff check
fairness.py`.
