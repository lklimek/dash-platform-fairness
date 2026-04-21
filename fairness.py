#!/usr/bin/env python3
"""
Dash Platform Validator Fairness Score.

Computes a composite fairness score for Evo (HPMN) validators over a
configurable time window, combining three axes:

    selection     - how often the target was picked into a quorum vs its peers
    participation - of slots where the target was expected to propose, how many did it hit
    liveness      - round-failures attributable to the target while it was the expected proposer

The precommit axis (did the target's signature land in last_commit?) is intentionally
not implemented in v1 and left as TODO(precommit).

Usage:
    python3 fairness.py <protx_hash> [options]          # single validator
    python3 fairness.py --all-platform [options]        # batch every Evo MN
See README.md for the full CLI.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Callable

ALGO_VERSION = "0.2.0"

QSIZE_NOMINAL = 100
QTHRESH = 67
CORE_LLMQ_TYPE_NUM = 4  # llmq_100_67 on mainnet; Tenderdash reports quorum_type=4

DEFAULT_COMPOSITE_WEIGHTS = {"selection": 0.30, "participation": 0.50, "liveness": 0.20}

# pose_status values that mean "validator was fully eligible for the ENTIRE
# window". Only these nodes contribute to the peer baseline (member_of median):
# partial-eligibility peers (registered mid-window, deregistered mid-window,
# revived mid-window, currently banned) get SCORED AGAINST the baseline, not
# used to DEFINE it. Including the legacy pre-merge values keeps the filter
# backward-compatible with older summary.json blobs.
ALWAYS_ELIGIBLE_POSE_STATUSES = frozenset(
    {
        "active_whole_window",  # current merged category (v0.2.0+)
        "never",  # legacy pre-merge
        "revived_before_window",  # legacy pre-merge
    }
)

DEFAULT_CORE_CMD = "docker exec dashmate_2d59c0c6_mainnet-core-1 dash-cli"
DEFAULT_CORE_CMD_FALLBACK = "dash-cli"

# Matches per-validator report files written by batch mode.
# Only files with this exact shape are eligible for stale-cleanup.
# Unrelated files the user placed in reports/ are NOT touched.
REPORT_FILE_RE = re.compile(r"^[0-9a-f]{8}_[0-9]{8}T[0-9]{6}Z\.(json|html)$")

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

VERBOSE = False


def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"[fairness] {msg}", file=sys.stderr, flush=True)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    # Tenderdash returns e.g. "2026-04-21T12:00:14.393Z" — strip the trailing Z
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        frac = frac[:6]  # microsecond precision
        s = f"{head}.{frac}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# RPC clients
# ---------------------------------------------------------------------------


class TenderdashClient:
    """Tiny connection-reusing Tenderdash RPC client (plain GET, no websocket)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener()
        self.calls = 0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if params:
            qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
            url = f"{self.base_url}/{path}?{qs}"
        else:
            url = f"{self.base_url}/{path}"
        self.calls += 1
        req = urllib.request.Request(url, headers={"Connection": "keep-alive"})
        with self.opener.open(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"Tenderdash {path} HTTP {r.status}")
            raw = r.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Tenderdash {path} non-JSON: {e}; raw head: {raw[:200]!r}"
            ) from e
        # Tenderdash v1 returns fields at root; older variants wrap in "result"
        return data.get("result", data)

    def status(self) -> dict[str, Any]:
        """Return /status (sync_info, node_info)."""
        return self._get("status")

    def block(self, height: int) -> dict[str, Any]:
        """Return /block?height=H (top-level {block_id, block})."""
        return self._get("block", {"height": height})

    def validators(
        self, height: int, page: int = 1, per_page: int = 100
    ) -> dict[str, Any]:
        """Return /validators?height=H&page=P&per_page=100. Includes quorum_hash + sorted members."""
        return self._get(
            "validators", {"height": height, "page": page, "per_page": per_page}
        )


class CoreClient:
    """Thin wrapper over `dash-cli` shell invocations."""

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd
        self.argv_prefix = shlex.split(cmd)
        self.calls = 0

    def run_raw(self, *args: str, check: bool = True) -> str:
        self.calls += 1
        argv = [*self.argv_prefix, *args]
        vlog(f"core: {' '.join(shlex.quote(a) for a in argv)}")
        r = subprocess.run(argv, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(
                f"dash-cli failed ({r.returncode}): argv={argv} stderr={r.stderr.strip()}"
            )
        return r.stdout

    def run_json(self, *args: str) -> Any:
        out = self.run_raw(*args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"dash-cli {args} non-JSON: {e}; head: {out[:300]!r}"
            ) from e

    def ok(self) -> bool:
        try:
            self.run_raw("getblockcount")
            return True
        except Exception:
            return False

    # Specific helpers
    def protx_info(self, protx: str, block_hash: str | None = None) -> dict[str, Any]:
        """Return full protx info (outer + state inner). Optional blockHash gives historical state."""
        if block_hash is not None:
            return self.run_json("protx", "info", protx, block_hash)
        return self.run_json("protx", "info", protx)

    def protx_list_valid(
        self, detailed: bool = False, height: int | None = None
    ) -> Any:
        argv = ["protx", "list", "valid"]
        if detailed or height is not None:
            argv.append("true" if detailed else "false")
        if height is not None:
            argv.append(str(height))
        return self.run_json(*argv)

    def protx_list_evo(self, detailed: bool = True) -> Any:
        """Return `protx list evo [detailed]` — only EvoNodes (type=Evo)."""
        argv = ["protx", "list", "evo", "true" if detailed else "false"]
        return self.run_json(*argv)

    def protx_listdiff(self, base: int, to: int) -> dict[str, Any]:
        return self.run_json("protx", "listdiff", str(base), str(to))

    def quorum_info(self, llmq_type: int, quorum_hash: str) -> dict[str, Any]:
        """Return `quorum info <llmq_type> <quorum_hash>` — canonical LLMQ members & pubkey."""
        return self.run_json("quorum", "info", str(llmq_type), quorum_hash)

    def get_block_hash(self, height: int) -> str:
        """Return block hash at given Core height."""
        return self.run_raw("getblockhash", str(height)).strip()


# ---------------------------------------------------------------------------
# Core data classes
# ---------------------------------------------------------------------------


@dataclass
class BlockData:
    height: int
    time: datetime
    proposer: str  # upper-case hex
    validators_hash: str  # upper-case
    core_h: int
    last_commit_round: int  # round at which H-1 committed

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> "BlockData":
        b = raw["block"]
        h = b["header"]
        return cls(
            height=int(h["height"]),
            time=parse_ts(h["time"]),
            proposer=h["proposer_pro_tx_hash"].upper(),
            validators_hash=h["validators_hash"].upper(),
            core_h=int(h["core_chain_locked_height"]),
            last_commit_round=int(b["last_commit"]["round"]),
        )


@dataclass
class PoseEvent:
    banned_at_core_height: int | None
    revived_at_core_height: int | None
    platform_range_excluded: tuple[int, int] | None
    duration_seconds: int


# ---------------------------------------------------------------------------
# Window resolution via binary search
# ---------------------------------------------------------------------------


def find_start_height(td: TenderdashClient, h_tip: int, t_start: datetime) -> int:
    """Binary-search earliest height H in [1, h_tip] with block.time >= t_start."""
    lo, hi = 1, h_tip
    answer = h_tip
    while lo <= hi:
        mid = (lo + hi) // 2
        bd = BlockData.from_rpc(td.block(mid))
        if bd.time >= t_start:
            answer = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return answer


def find_core_boundary(
    td: TenderdashClient,
    platform_lo: int,
    platform_hi: int,
    core_target: int,
    blocks: dict[int, BlockData] | None = None,
) -> int:
    """
    Binary-search earliest platform H in [lo, hi] whose core_chain_locked_height >= core_target.
    Returns platform_hi+1 if no such height exists within range.

    If a pre-fetched `blocks` cache is provided, it's consulted first to avoid
    duplicate /block?height=H RPCs.
    """
    lo, hi = platform_lo, platform_hi
    answer = platform_hi + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if blocks is not None and mid in blocks:
            bd = blocks[mid]
        else:
            bd = BlockData.from_rpc(td.block(mid))
            if blocks is not None:
                blocks[mid] = bd
        if bd.core_h >= core_target:
            answer = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return answer


# ---------------------------------------------------------------------------
# Eligibility (PoSe) replay
# ---------------------------------------------------------------------------


def _enumerate_pose_segments(
    core: CoreClient,
    protx: str,
    core_lo: int,
    core_hi: int,
    block_hash_cache: dict[int, str] | None = None,
) -> list[tuple[int, int, int, int]]:
    """
    Walk the target's (PoSeBanHeight, PoSeRevivedHeight) state across [core_lo, core_hi]
    by recursive bisection of historical `protx info <hash> <blockHash>`.

    Returns a sorted list of segments (seg_lo, seg_hi, ban_h, rev_h) where the
    target's state tuple is constant.

    `block_hash_cache` is a caller-supplied shared cache (batch mode benefits
    substantially — every validator's walk hits the same ~log(range) heights).

    Heights where the MN did not yet exist (i.e. Core responds with
    "not found" because registeredHeight > h) are treated as "no ban"
    state (-1, -1). Conceptually: a non-existent MN can't be PoSe-banned.

    Complexity: O(K * log(range)) Core RPCs where K is the number of transitions
    (typically 1-4 inside a 30-day window).
    """
    state_cache: dict[int, tuple[int, int]] = {}
    if block_hash_cache is None:
        block_hash_cache = {}

    def bh(h: int) -> str:
        v = block_hash_cache.get(h)
        if v is None:
            v = core.get_block_hash(h)
            block_hash_cache[h] = v
        return v

    def state(h: int) -> tuple[int, int]:
        cached = state_cache.get(h)
        if cached is not None:
            return cached
        try:
            info = core.protx_info(protx, bh(h))
        except RuntimeError as e:
            # Pre-registration: Core returns "<hash> not found" (error code -8).
            # Treat as "no ban" — the MN didn't exist so it can't be banned.
            if "not found" in str(e).lower():
                r = (-1, -1)
                state_cache[h] = r
                return r
            raise
        s = info.get("state") or {}
        try:
            ban = int(s.get("PoSeBanHeight") or -1)
        except (TypeError, ValueError):
            ban = -1
        try:
            rev = int(s.get("PoSeRevivedHeight") or -1)
        except (TypeError, ValueError):
            rev = -1
        r = (ban, rev)
        state_cache[h] = r
        return r

    segments: list[tuple[int, int, tuple[int, int]]] = []

    # Iterative bisection via an explicit stack: (lo, hi, s_lo, s_hi)
    stack: list[tuple[int, int, tuple[int, int], tuple[int, int]]] = [
        (core_lo, core_hi, state(core_lo), state(core_hi))
    ]
    while stack:
        lo, hi, s_lo, s_hi = stack.pop()
        if s_lo == s_hi:
            segments.append((lo, hi, s_lo))
            continue
        if hi - lo <= 1:
            segments.append((lo, lo, s_lo))
            segments.append((hi, hi, s_hi))
            continue
        mid = (lo + hi) // 2
        s_mid = state(mid)
        s_mid_next = state(mid + 1) if mid + 1 != hi else s_hi
        stack.append((mid + 1, hi, s_mid_next, s_hi))
        stack.append((lo, mid, s_lo, s_mid))

    segments.sort()
    # Coalesce adjacent same-state segments
    merged: list[tuple[int, int, int, int]] = []
    for lo, hi, (ban, rev) in segments:
        if (
            merged
            and merged[-1][2] == ban
            and merged[-1][3] == rev
            and merged[-1][1] + 1 == lo
        ):
            prev = merged[-1]
            merged[-1] = (prev[0], hi, ban, rev)
        else:
            merged.append((lo, hi, ban, rev))
    return merged


def build_pose_events(
    core: CoreClient,
    protx: str,
    core_lo: int,
    core_hi: int,
    td: TenderdashClient,
    platform_lo: int,
    platform_hi: int,
    t_of_block: Callable[[int], datetime],
    blocks: dict[int, BlockData] | None = None,
    block_hash_cache: dict[int, str] | None = None,
) -> tuple[list[PoseEvent], float]:
    """
    Build PoSe ban events in [core_lo, core_hi] by walking historical Core state
    with `protx info <hash> <blockHash>` and bisecting for transitions. Multi-ban
    timelines are captured (not just the most recent one).

    Each "banned" segment (PoSeBanHeight > 0) in core heights is converted to a
    platform height range via `find_core_boundary`, and its duration is subtracted
    from window_seconds to yield `eligible_fraction`.
    """
    segments = _enumerate_pose_segments(
        core, protx, core_lo, core_hi, block_hash_cache=block_hash_cache
    )
    events: list[PoseEvent] = []
    window_seconds = float(
        (t_of_block(platform_hi) - t_of_block(platform_lo)).total_seconds()
    )
    excluded_seconds = 0.0

    for seg_lo_c, seg_hi_c, ban_h, rev_h in segments:
        if ban_h <= 0:
            continue  # active segment, no ban

        p_start = (
            find_core_boundary(td, platform_lo, platform_hi, seg_lo_c, blocks)
            if seg_lo_c > 0
            else platform_lo
        )
        p_first_after = find_core_boundary(
            td, platform_lo, platform_hi, seg_hi_c + 1, blocks
        )
        p_end = min(p_first_after - 1, platform_hi)
        p_start = max(p_start, platform_lo)
        if p_end < p_start:
            continue
        dur = (t_of_block(p_end) - t_of_block(p_start)).total_seconds()
        excluded_seconds += dur
        revived_core = None
        for other in segments:
            if other[0] > seg_hi_c and other[2] <= 0 and other[3] >= seg_lo_c:
                revived_core = other[3] if other[3] > 0 else None
                break
        events.append(
            PoseEvent(
                banned_at_core_height=ban_h if ban_h > 0 else None,
                revived_at_core_height=revived_core,
                platform_range_excluded=(p_start, p_end),
                duration_seconds=int(dur),
            )
        )

    eligible_fraction = 1.0
    if window_seconds > 0:
        eligible_fraction = max(0.0, min(1.0, 1.0 - excluded_seconds / window_seconds))
    return events, eligible_fraction


# ---------------------------------------------------------------------------
# Block enumeration + quorum grouping
# ---------------------------------------------------------------------------


def enumerate_blocks(
    td: TenderdashClient, h_lo: int, h_hi: int
) -> dict[int, BlockData]:
    """Fetch every block in [h_lo, h_hi]. Reports progress every 500 blocks if verbose."""
    blocks: dict[int, BlockData] = {}
    total = h_hi - h_lo + 1
    t0 = time.monotonic()
    for i, H in enumerate(range(h_lo, h_hi + 1)):
        blocks[H] = BlockData.from_rpc(td.block(H))
        if VERBOSE and (i + 1) % 500 == 0:
            elapsed = time.monotonic() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0.0
            eta = (total - (i + 1)) / rate if rate > 0 else 0.0
            vlog(f"blocks {i + 1}/{total} ({rate:.1f}/s, ETA {eta:.0f}s)")
    return blocks


def group_by_quorum(
    blocks: dict[int, BlockData],
) -> list[tuple[str, int, int]]:
    """
    Group contiguous runs of equal validators_hash. Returns list of (vhash, lo, hi).
    (The actual quorum_hash is resolved later via /validators.)
    """
    heights = sorted(blocks.keys())
    runs: list[tuple[str, int, int]] = []
    if not heights:
        return runs
    cur_v = blocks[heights[0]].validators_hash
    cur_lo = heights[0]
    prev = heights[0]
    for H in heights[1:]:
        v = blocks[H].validators_hash
        if v != cur_v or H != prev + 1:
            runs.append((cur_v, cur_lo, prev))
            cur_v = v
            cur_lo = H
        prev = H
    runs.append((cur_v, cur_lo, prev))
    return runs


def resolve_tenderdash_quorum(
    td: TenderdashClient, any_height: int
) -> tuple[str, list[str]]:
    """
    Call /validators at any height. Returns (lower-case quorum_hash, sorted-ascending
    members as lower-case pro_tx_hashes).
    """
    r = td.validators(any_height, page=1, per_page=100)
    qh = r["quorum_hash"].lower()
    members = [v["pro_tx_hash"].lower() for v in r["validators"]]
    if members != sorted(members):
        members = sorted(members)
    return qh, members


def resolve_canonical_members(core: CoreClient, quorum_hash: str) -> list[str]:
    """
    Fetch the DKG-canonical member list for an llmq_platform quorum from Core's
    `quorum info`.
    """
    qi = core.quorum_info(CORE_LLMQ_TYPE_NUM, quorum_hash)
    members = [m["proTxHash"].lower() for m in qi.get("members", [])]
    members.sort()
    return members


def extend_quorum_boundaries(
    td: TenderdashClient,
    blocks: dict[int, BlockData],
    runs: list[tuple[str, int, int]],
    global_h_lo: int,
    global_h_hi: int,
) -> list[tuple[str, int, int, bool, bool]]:
    """
    For runs shorter than QSIZE_NOMINAL, walk outward fetching neighbours until
    vhash changes or we hit the chain ends. Returns runs annotated with
    (vhash, lo, hi, left_real, right_real).
    """
    extended: list[tuple[str, int, int, bool, bool]] = []
    for vhash, lo, hi in runs:
        count = hi - lo + 1
        new_lo, new_hi = lo, hi
        left_real, right_real = False, False

        # Extend leftward
        cur = lo - 1
        while count < QSIZE_NOMINAL * 2 and cur >= 1:
            if cur not in blocks:
                blocks[cur] = BlockData.from_rpc(td.block(cur))
            if blocks[cur].validators_hash != vhash:
                left_real = True
                break
            new_lo = cur
            count += 1
            cur -= 1
        else:
            if cur < 1:
                left_real = False
            elif count >= QSIZE_NOMINAL * 2:
                left_real = True

        if (
            new_lo > 1
            and new_lo - 1 in blocks
            and blocks[new_lo - 1].validators_hash != vhash
        ):
            left_real = True

        # Extend rightward
        cur = hi + 1
        while cur <= 10_000_000:
            if cur not in blocks:
                try:
                    blocks[cur] = BlockData.from_rpc(td.block(cur))
                except Exception:
                    break
            if blocks[cur].validators_hash != vhash:
                right_real = True
                break
            new_hi = cur
            cur += 1
            if new_hi - new_lo + 1 >= QSIZE_NOMINAL * 3:
                break

        if (hi - lo + 1) >= QSIZE_NOMINAL:
            new_lo, new_hi = lo, hi
            left_real = lo > global_h_lo
            right_real = hi < global_h_hi

        extended.append((vhash, new_lo, new_hi, left_real, right_real))
    return extended


# ---------------------------------------------------------------------------
# Per-quorum classification (target-agnostic)
# ---------------------------------------------------------------------------


@dataclass
class QuorumClassification:
    """
    Target-agnostic data for one sub-run or one aggregated LLMQ. The derived
    per-validator fields (target_status, etc.) are computed on demand in
    ValidatorQuorumView — never stored here so the cache can be shared.
    """

    quorum_hash: str
    vhash: str
    lo: int
    hi: int
    boundary_left_real: bool
    boundary_right_real: bool
    members: list[
        str
    ]  # sorted ascending, lower-case (Tenderdash or canonical, see context)
    rotation_ok: bool = True
    cover_ups: list[dict[str, Any]] = field(default_factory=list)
    # For liveness: list of (height, round_index, member_at_that_slot) for each failed round
    round_failure_attributions: list[tuple[int, int, str]] = field(default_factory=list)
    # index in members[] for each proposed block in [lo, hi]
    observed_proposer_indices: list[int] = field(default_factory=list)
    # actual proposal counts per member
    proposal_counts: dict[str, int] = field(default_factory=dict)
    # first expected round-0 slot height per member
    expected_slot_of: dict[str, int] = field(default_factory=dict)


def fill_quorum_stats(
    qc: QuorumClassification,
    blocks: dict[int, BlockData],
) -> None:
    """
    Verify rotation and fill in target-agnostic stats. Target-specific status
    is derived later via derive_target_status.
    """
    M = qc.members
    N = len(M)
    if N == 0:
        qc.rotation_ok = False
        return

    R_lo = 0
    if (qc.lo + 1) in blocks:
        R_lo = blocks[qc.lo + 1].last_commit_round
    first = blocks[qc.lo]
    proposer_lower = first.proposer.lower()
    try:
        successful_idx_lo = M.index(proposer_lower)
    except ValueError:
        qc.rotation_ok = False
        return
    start_idx = (successful_idx_lo - R_lo) % N

    cumulative = 0
    counts: dict[str, int] = {}
    expected_slot: dict[str, int] = {}
    observed_indices: list[int] = []
    round_failures: list[tuple[int, int, str]] = []

    for H in range(qc.lo, qc.hi + 1):
        bd = blocks[H]
        R_at_H = 0
        if (H + 1) in blocks:
            R_at_H = blocks[H + 1].last_commit_round

        round0_idx = (start_idx + cumulative) % N
        success_idx = (start_idx + cumulative + R_at_H) % N
        successful_member = M[success_idx]

        if bd.proposer.lower() != successful_member:
            qc.rotation_ok = False
            return

        round0_member = M[round0_idx]
        if round0_member not in expected_slot:
            expected_slot[round0_member] = H

        observed_indices.append(success_idx)
        counts[successful_member] = counts.get(successful_member, 0) + 1

        for r in range(R_at_H):
            fidx = (start_idx + cumulative + r) % N
            round_failures.append((H, r, M[fidx]))

        cumulative += 1 + R_at_H

    qc.proposal_counts = counts
    qc.expected_slot_of = expected_slot
    qc.observed_proposer_indices = observed_indices
    qc.round_failure_attributions = round_failures

    for v, c in counts.items():
        if c >= 2:
            qc.cover_ups.append(
                {"quorum_hash": qc.quorum_hash, "proposer": v, "count": c}
            )


# ---------------------------------------------------------------------------
# Aggregated LLMQ (one quorum_hash, possibly many sub-runs)
# ---------------------------------------------------------------------------


@dataclass
class AggregatedQuorum:
    """
    One unique LLMQ (quorum_hash) across possibly multiple Tenderdash sub-runs.
    Stores target-agnostic aggregated data. Per-target view computed on demand.
    """

    quorum_hash: str
    lo: int
    hi: int
    boundary_left_real: bool
    boundary_right_real: bool
    canonical_members: list[str]  # DKG-roster, sorted ascending
    rotation_ok: bool
    sub_runs: list[QuorumClassification]
    round_failure_attributions: list[tuple[int, int, str]]
    cover_ups: list[dict[str, Any]]
    total_proposal_counts: dict[str, int]  # sum across sub-runs
    # first expected slot height per member across sub-runs (min across sub-runs)
    expected_slot_of: dict[str, int]


def aggregate_by_quorum_hash(
    sub_classifications: list[QuorumClassification],
    canonical_cache: dict[str, list[str]],
) -> list[AggregatedQuorum]:
    """Group sub-runs by quorum_hash; compute target-agnostic aggregates."""
    by_qh: dict[str, list[QuorumClassification]] = {}
    for qc in sub_classifications:
        by_qh.setdefault(qc.quorum_hash, []).append(qc)

    aggregated: list[AggregatedQuorum] = []
    for qh, subs in by_qh.items():
        canonical = canonical_cache[qh]
        subs.sort(key=lambda q: q.lo)
        lo = subs[0].lo
        hi = subs[-1].hi
        left_real = subs[0].boundary_left_real
        right_real = subs[-1].boundary_right_real
        rotation_ok = all(q.rotation_ok for q in subs)

        total_counts: dict[str, int] = {}
        expected_slot: dict[str, int] = {}
        round_misses: list[tuple[int, int, str]] = []
        cover_ups: list[dict[str, Any]] = []

        for q in subs:
            for m, c in q.proposal_counts.items():
                total_counts[m] = total_counts.get(m, 0) + c
            for m, h in q.expected_slot_of.items():
                cur = expected_slot.get(m)
                if cur is None or h < cur:
                    expected_slot[m] = h
            round_misses.extend(q.round_failure_attributions)
            cover_ups.extend(q.cover_ups)

        aggregated.append(
            AggregatedQuorum(
                quorum_hash=qh,
                lo=lo,
                hi=hi,
                boundary_left_real=left_real,
                boundary_right_real=right_real,
                canonical_members=canonical,
                rotation_ok=rotation_ok,
                sub_runs=subs,
                round_failure_attributions=round_misses,
                cover_ups=cover_ups,
                total_proposal_counts=total_counts,
                expected_slot_of=expected_slot,
            )
        )
    aggregated.sort(key=lambda a: a.lo)
    return aggregated


# ---------------------------------------------------------------------------
# Per-target status derivation
# ---------------------------------------------------------------------------


def derive_target_status_for_agg(
    agg: AggregatedQuorum,
    target_lower: str,
) -> dict[str, Any]:
    """
    Given an aggregated LLMQ and a target pro_tx_hash, compute the target's
    status, expected slot, and actual proposal count.
    """
    target_is_member = target_lower in agg.canonical_members

    expected_slot: int | None = None
    actual_proposals = agg.total_proposal_counts.get(target_lower, 0)
    for q in agg.sub_runs:
        h = q.expected_slot_of.get(target_lower)
        if h is not None and (expected_slot is None or h < expected_slot):
            expected_slot = h

    sub_statuses: list[str] = []
    for q in agg.sub_runs:
        if not q.rotation_ok:
            sub_statuses.append("BAD_ROTATION")
            continue
        is_sub_member = target_lower in q.members
        exp_h = q.expected_slot_of.get(target_lower)
        act = q.proposal_counts.get(target_lower, 0)
        if not is_sub_member:
            sub_statuses.append("NA")
        elif act >= 1:
            sub_statuses.append("MET")
        elif exp_h is not None:
            sub_statuses.append("SKIPPED")
        else:
            if not (q.boundary_left_real and q.boundary_right_real):
                sub_statuses.append("INCONCLUSIVE")
            else:
                sub_statuses.append("NA")

    # Aggregate status across sub-runs
    if not target_is_member:
        agg_status = "NA"
    elif actual_proposals >= 1:
        agg_status = "MET"
    elif "SKIPPED" in sub_statuses:
        agg_status = "SKIPPED"
    elif "INCONCLUSIVE" in sub_statuses:
        agg_status = "INCONCLUSIVE"
    elif all(target_lower not in q.members for q in agg.sub_runs):
        # Target is in canonical set but absent from every sub-run's eligible
        # set: runtime-banned for the whole quorum's platform range.
        agg_status = "SKIPPED"
    else:
        agg_status = "NA"

    return {
        "target_is_member": target_is_member,
        "target_expected_slot_height": expected_slot,
        "target_actual_proposals": actual_proposals,
        "target_status": agg_status,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class PeerStats:
    pool_size: int
    member_of_median: float
    proposed_rate_median: float


def compute_scores(
    member_of: int,
    met: int,
    skipped: int,
    inconclusive: int,
    round_misses: int,
    peer_stats: PeerStats,
    eligible_fraction: float,
    has_peer_scan: bool,
) -> dict[str, Any]:
    # Selection
    if has_peer_scan and peer_stats.member_of_median > 0:
        denom = peer_stats.member_of_median * max(eligible_fraction, 0.01)
        selection = min(1.0, member_of / denom) if denom > 0 else None
    else:
        selection = 1.0 if member_of >= 1 else 0.0

    # Participation
    denom_p = met + skipped
    participation = met / denom_p if denom_p > 0 else None

    # Liveness
    if denom_p > 0:
        liveness = max(0.0, 1.0 - round_misses / denom_p)
    else:
        liveness = None

    # Composite
    weights = DEFAULT_COMPOSITE_WEIGHTS
    components = []
    total_weight = 0.0
    for name, val in (
        ("selection", selection),
        ("participation", participation),
        ("liveness", liveness),
    ):
        if val is None:
            continue
        components.append(val * weights[name])
        total_weight += weights[name]
    composite = sum(components) / total_weight if total_weight > 0 else None

    def band(c: float | None) -> str:
        if c is None:
            return "N/A"
        if c >= 0.95:
            return "Excellent"
        if c >= 0.85:
            return "Good"
        if c >= 0.70:
            return "Concerning"
        return "Poor"

    return {
        "selection": round(selection, 4) if selection is not None else None,
        "participation": round(participation, 4) if participation is not None else None,
        "liveness": round(liveness, 4) if liveness is not None else None,
        "precommit": None,
        "composite": round(composite, 4) if composite is not None else None,
        "band": band(composite),
    }


# ---------------------------------------------------------------------------
# Window cache (shared across a batch)
# ---------------------------------------------------------------------------


@dataclass
class WindowCache:
    """Shared, target-agnostic state built once per batch run."""

    td: TenderdashClient
    core: CoreClient
    days: int
    h_tip: int
    h_start: int
    t_start: datetime
    t_tip: datetime
    core_lo: int
    core_hi: int
    blocks: dict[int, BlockData]
    aggregated_quorums: list[AggregatedQuorum]
    bad_rotation_count: int
    core_h_tip: int
    # Evo MN registry at tip, keyed by lower-case protx hash → raw protx entry
    evo_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Shared Core block hash cache for PoSe bisection
    block_hash_cache: dict[int, str] = field(default_factory=dict)


def build_window_cache(
    td: TenderdashClient,
    core: CoreClient,
    days: int,
) -> WindowCache:
    """
    Fetch all target-agnostic state: status, block range, sub-run validators,
    canonical members, and LLMQ aggregation. Target-specific (eligibility,
    per-target status) is derived later per validator.
    """
    status = td.status()
    h_tip = int(status["sync_info"]["latest_block_height"])
    tip_block = BlockData.from_rpc(td.block(h_tip))
    t_tip = tip_block.time
    t_start = t_tip - timedelta(days=days)
    h_start = find_start_height(td, h_tip, t_start)
    start_block = BlockData.from_rpc(td.block(h_start))
    core_lo, core_hi = start_block.core_h, tip_block.core_h

    vlog(
        f"window: platform [{h_start}..{h_tip}] "
        f"= core [{core_lo}..{core_hi}] "
        f"time [{iso_utc(start_block.time)} .. {iso_utc(t_tip)}]"
    )

    blocks = enumerate_blocks(td, h_start, h_tip)

    runs = group_by_quorum(blocks)
    extended = extend_quorum_boundaries(td, blocks, runs, h_start, h_tip)

    subrun_cache: dict[str, tuple[str, list[str]]] = {}
    sub_classifications: list[QuorumClassification] = []
    for vhash, lo, hi, left_real, right_real in extended:
        if vhash in subrun_cache:
            qh, members = subrun_cache[vhash]
        else:
            qh, members = resolve_tenderdash_quorum(td, lo)
            subrun_cache[vhash] = (qh, members)
        qc = QuorumClassification(
            quorum_hash=qh,
            vhash=vhash,
            lo=lo,
            hi=hi,
            boundary_left_real=left_real,
            boundary_right_real=right_real,
            members=members,
        )
        fill_quorum_stats(qc, blocks)
        sub_classifications.append(qc)

    canonical_cache: dict[str, list[str]] = {}
    for qc in sub_classifications:
        if qc.quorum_hash not in canonical_cache:
            canonical_cache[qc.quorum_hash] = resolve_canonical_members(
                core, qc.quorum_hash
            )

    aggregated = aggregate_by_quorum_hash(sub_classifications, canonical_cache)
    bad_rotation = sum(1 for a in aggregated if not a.rotation_ok)
    if bad_rotation:
        vlog(
            f"WARNING: {bad_rotation} aggregated quorum(s) failed rotation sanity check."
        )

    # Core tip height (for peer-pool snapshot)
    core_h_tip = core_hi

    return WindowCache(
        td=td,
        core=core,
        days=days,
        h_tip=h_tip,
        h_start=h_start,
        t_start=start_block.time,
        t_tip=t_tip,
        core_lo=core_lo,
        core_hi=core_hi,
        blocks=blocks,
        aggregated_quorums=aggregated,
        bad_rotation_count=bad_rotation,
        core_h_tip=core_h_tip,
    )


# ---------------------------------------------------------------------------
# Per-validator scoring (uses cache)
# ---------------------------------------------------------------------------


def score_validator_from_cache(
    cache: WindowCache,
    protx: str,
    peer_stats: PeerStats,
    has_peer_scan: bool,
    protx_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compute the full fairness report for `protx` using the prebuilt cache.

    If `protx_info` is passed it's reused (batch mode fetches this once for
    every Evo MN via `protx list evo true`).
    """
    target_upper = protx.upper()
    target_lower = protx.lower()

    info = protx_info if protx_info is not None else cache.core.protx_info(target_lower)
    node_type = info.get("type")
    state = info.get("state") or {}
    reg_h = state.get("registeredHeight")

    if node_type != "Evo":
        return {
            "protx": target_upper,
            "status": "NOT_APPLICABLE",
            "reason": f"node type is {node_type!r}, not 'Evo' (HPMN)",
            "node_type": node_type,
            "generated_at": iso_utc(datetime.now(timezone.utc)),
        }

    def t_of_block(h: int) -> datetime:
        return cache.blocks[h].time

    try:
        reg_h_int = int(reg_h) if reg_h is not None else 0
    except (TypeError, ValueError):
        reg_h_int = 0

    # Deregistration height in core heights, or None if the MN is still
    # registered at tip. Batch mode annotates this via protx_info["_dereg_core_h"]
    # when a MN was present at core_lo but not at tip.
    dereg_h_raw = info.get("_dereg_core_h") if isinstance(info, dict) else None
    try:
        dereg_h_int: int | None = int(dereg_h_raw) if dereg_h_raw is not None else None
    except (TypeError, ValueError):
        dereg_h_int = None

    # Fast path: if the MN has never been PoSe-banned (both fields unset) then
    # there are no transitions to find. Saves O(log(range)) Core RPCs per
    # clean validator, which compounds across batch runs.
    try:
        tip_ban = int(state.get("PoSeBanHeight") or -1)
    except (TypeError, ValueError):
        tip_ban = -1
    try:
        tip_rev = int(state.get("PoSeRevivedHeight") or -1)
    except (TypeError, ValueError):
        tip_rev = -1

    if tip_ban <= 0 and tip_rev <= 0:
        pose_events: list[PoseEvent] = []
        eligible_fraction = 1.0
    else:
        # Clamp the walk range to the MN's registered lifetime. A protx info
        # lookup at a height before registeredHeight returns "not found".
        walk_lo = max(cache.core_lo, reg_h_int) if reg_h_int > 0 else cache.core_lo
        # If the MN was deregistered mid-window, don't walk past the dereg
        # height — the MN no longer exists there so Core returns "not found".
        walk_hi = (
            min(cache.core_hi, dereg_h_int - 1)
            if dereg_h_int is not None
            else cache.core_hi
        )
        if walk_lo > walk_hi:
            pose_events = []
            eligible_fraction = 1.0
        else:
            pose_events, eligible_fraction = build_pose_events(
                cache.core,
                target_lower,
                walk_lo,
                walk_hi,
                cache.td,
                cache.h_start,
                cache.h_tip,
                t_of_block,
                blocks=cache.blocks,
                block_hash_cache=cache.block_hash_cache,
            )

    # Symmetric eligibility cap: reduce eligible_fraction for late registration
    # AND early deregistration. Without this, newly-registered or soon-to-be-
    # deregistered MNs are compared against the full-window peer median and
    # their selection score falsely tanks.
    window_len = max(cache.core_hi - cache.core_lo, 1)
    reg_start_h = max(reg_h_int, cache.core_lo) if reg_h_int > 0 else cache.core_lo
    reg_end_h = (
        min(dereg_h_int, cache.core_hi) if dereg_h_int is not None else cache.core_hi
    )
    reg_eligible = max(0.0, (reg_end_h - reg_start_h) / window_len)
    eligible_fraction = min(eligible_fraction, reg_eligible)

    eligible_seconds = int(
        eligible_fraction * (cache.t_tip - cache.t_start).total_seconds()
    )

    # Per-quorum target classification
    met = 0
    skipped = 0
    inconclusive = 0
    not_applicable_slots = 0
    member_of = 0
    round_misses_on_target = 0
    cover_up_doubles: list[dict[str, Any]] = []

    member_quorums_detail: list[dict[str, Any]] = []
    skipped_quorums_detail: list[dict[str, Any]] = []
    inconclusive_quorums_detail: list[dict[str, Any]] = []

    for agg in cache.aggregated_quorums:
        if not agg.rotation_ok:
            inconclusive += 1
            inconclusive_quorums_detail.append(
                {
                    "quorum_hash": agg.quorum_hash,
                    "range": [agg.lo, agg.hi],
                    "reason": "rotation-sanity-failed",
                }
            )
            continue
        tv = derive_target_status_for_agg(agg, target_lower)
        if tv["target_is_member"]:
            member_of += 1
            for H, r, v in agg.round_failure_attributions:
                if v == target_lower:
                    round_misses_on_target += 1
            st = tv["target_status"]
            if st == "MET":
                met += 1
            elif st == "SKIPPED":
                skipped += 1
            elif st == "INCONCLUSIVE":
                inconclusive += 1
            elif st == "NA":
                not_applicable_slots += 1

            for cu in agg.cover_ups:
                if cu["proposer"] != target_lower:
                    cover_up_doubles.append(cu)

            ts_lo = iso_utc(cache.blocks[agg.lo].time)
            ts_hi = iso_utc(cache.blocks[agg.hi].time)
            member_quorums_detail.append(
                {
                    "quorum_hash": agg.quorum_hash,
                    "range": [agg.lo, agg.hi],
                    "ts": [ts_lo, ts_hi],
                    "expected_slot_height": tv["target_expected_slot_height"],
                    "actual_proposals": tv["target_actual_proposals"],
                    "status": st,
                }
            )
            if st == "SKIPPED":
                cover_up_proposer = None
                exp_h = tv["target_expected_slot_height"]
                if exp_h is not None:
                    cover_up_proposer = cache.blocks[exp_h].proposer.lower()
                skipped_quorums_detail.append(
                    {
                        "quorum_hash": agg.quorum_hash,
                        "range": [agg.lo, agg.hi],
                        "expected_slot_height": exp_h,
                        "cover_up": cover_up_proposer,
                    }
                )
            elif st == "INCONCLUSIVE":
                inconclusive_quorums_detail.append(
                    {
                        "quorum_hash": agg.quorum_hash,
                        "range": [agg.lo, agg.hi],
                        "reason": "boundary-edge",
                    }
                )

    scores = compute_scores(
        member_of,
        met,
        skipped,
        inconclusive,
        round_misses_on_target,
        peer_stats,
        eligible_fraction,
        has_peer_scan=has_peer_scan,
    )

    report = {
        "protx": target_upper,
        "generated_at": iso_utc(datetime.now(timezone.utc)),
        "algorithm_version": ALGO_VERSION,
        "window": {
            "days": cache.days,
            "from_time": iso_utc(cache.t_start),
            "to_time": iso_utc(cache.t_tip),
            "platform_range": [cache.h_start, cache.h_tip],
            "core_range": [cache.core_lo, cache.core_hi],
        },
        "eligibility": {
            "registered_height": reg_h,
            "deregistered_core_height": dereg_h_int,
            "node_type": node_type,
            "pose_events": [
                {
                    "banned_at_core_height": ev.banned_at_core_height,
                    "revived_at_core_height": ev.revived_at_core_height,
                    "platform_range_excluded": list(ev.platform_range_excluded)
                    if ev.platform_range_excluded
                    else None,
                    "duration_seconds": ev.duration_seconds,
                }
                for ev in pose_events
            ],
            "eligible_seconds": eligible_seconds,
            "eligible_fraction": round(eligible_fraction, 4),
        },
        "quorum_stats": {
            "quorums_in_window": len(cache.aggregated_quorums),
            "member_of": member_of,
            "met": met,
            "skipped": skipped,
            "not_applicable_slots": not_applicable_slots,
            "inconclusive": inconclusive,
            "round_misses_on_target": round_misses_on_target,
            "peer_pool_size": peer_stats.pool_size,
            "peer_member_of_median": peer_stats.member_of_median,
            "peer_proposed_rate_median": peer_stats.proposed_rate_median,
            "cover_up_doubles_in_member_quorums": cover_up_doubles,
            "bad_rotation_quorums": cache.bad_rotation_count,
        },
        "scores": scores,
        "detail": {
            "member_quorums": member_quorums_detail,
            "skipped_quorums": skipped_quorums_detail,
            "inconclusive_quorums": inconclusive_quorums_detail,
        },
        # Raw info used internally for batch pose_status classification.
        # Consumers of the public JSON can ignore this; included so summary.json
        # generation doesn't need a second protx info fetch.
        "_pose_state_at_tip": {
            "PoSeBanHeight": state.get("PoSeBanHeight"),
            "PoSeRevivedHeight": state.get("PoSeRevivedHeight"),
        },
    }
    return report


# ---------------------------------------------------------------------------
# Peer-relative pass
# ---------------------------------------------------------------------------


def compute_peer_stats_from_pool(
    core: CoreClient,
    core_h_tip: int,
    core_lo: int,
    aggregated: list[AggregatedQuorum],
    skip: bool,
) -> PeerStats:
    """
    Build peer stats from the current Evo pool using the aggregated quorums'
    proposal counts. This is the fallback path for single-target mode when
    --skip-peer-scan is NOT set; batch mode uses the richer
    compute_peer_stats_from_batch_results.

    We don't have full pose_status classification here (single-target mode
    runs the scan BEFORE per-peer pose events are computed), so we use a
    cheap proxy: exclude peers whose ``registeredHeight > core_lo`` — i.e.,
    peers that appeared mid-window. These are the "registered_in_window"
    cohort; including them in the baseline deflates the median. We can't
    cheaply detect mid-window revives or bans in single-target mode, so
    the baseline here is still slightly deflated — but less than before.
    (Batch mode has the full classification and excludes all partial-
    eligibility statuses correctly.)
    """
    pool_raw = core.protx_list_valid(detailed=True, height=core_h_tip)
    evo_list: list[str] = []
    evo_baseline: list[str] = []  # subset eligible for baseline (reg_h <= core_lo)
    for x in pool_raw:
        if not isinstance(x, dict):
            continue
        t = x.get("type") or (x.get("state") or {}).get("type")
        if t == "Evo":
            s = x.get("state") or {}
            reg_h = s.get("registeredHeight") or x.get("registeredHeight")
            try:
                reg_h_i = int(reg_h) if reg_h is not None else 0
            except (TypeError, ValueError):
                reg_h_i = 0
            if reg_h_i <= core_h_tip:
                protx_lower = x["proTxHash"].lower()
                evo_list.append(protx_lower)
                if reg_h_i <= core_lo:
                    evo_baseline.append(protx_lower)
    pool_size = len(evo_list)

    if skip:
        return PeerStats(
            pool_size=pool_size, member_of_median=0.0, proposed_rate_median=1.0
        )

    member_of_by_peer: dict[str, int] = {p: 0 for p in evo_list}
    proposed_by_peer: dict[str, int] = {p: 0 for p in evo_list}
    expected_by_peer: dict[str, int] = {p: 0 for p in evo_list}

    for agg in aggregated:
        if not agg.rotation_ok:
            continue
        canonical_set = set(agg.canonical_members)
        for peer in evo_list:
            if peer in canonical_set:
                member_of_by_peer[peer] += 1
                proposed_by_peer[peer] += agg.total_proposal_counts.get(peer, 0)
                if peer in agg.expected_slot_of:
                    expected_by_peer[peer] += 1

    # Only peers eligible for the full window (reg_h <= core_lo) define the
    # baseline. Fall back to the full pool if the proxy excludes everyone.
    baseline_peers = evo_baseline or evo_list
    member_of_values = [
        member_of_by_peer[p] for p in baseline_peers if member_of_by_peer[p] > 0
    ]
    member_of_median = float(median(member_of_values)) if member_of_values else 0.0
    rates = []
    for p in baseline_peers:
        if expected_by_peer[p] > 0:
            rates.append(proposed_by_peer[p] / expected_by_peer[p])
    proposed_rate_median = float(median(rates)) if rates else 1.0

    return PeerStats(
        pool_size=pool_size,
        member_of_median=member_of_median,
        proposed_rate_median=proposed_rate_median,
    )


def compute_peer_stats_from_batch_results(
    batch_rows: list[dict[str, Any]],
) -> PeerStats:
    """
    Compute peer medians from the aggregated per-validator results produced
    by a batch run.

    Only validators eligible the ENTIRE window (pose_status in
    ``ALWAYS_ELIGIBLE_POSE_STATUSES``) contribute to the baseline.
    Partial-eligibility validators (registered mid-window, deregistered
    mid-window, revived mid-window, currently banned) are SCORED AGAINST the
    baseline; using them to DEFINE it would deflate the median and inflate
    everyone's selection score.
    """
    member_ofs: list[int] = []
    rates: list[float] = []
    for r in batch_rows:
        if r.get("pose_status") not in ALWAYS_ELIGIBLE_POSE_STATUSES:
            continue
        mo = int(r.get("member_of", 0) or 0)
        met = int(r.get("met", 0) or 0)
        skipped = int(r.get("skipped", 0) or 0)
        if mo > 0:
            member_ofs.append(mo)
        if (met + skipped) > 0:
            rates.append(met / (met + skipped))

    pool_size = len(batch_rows)
    member_of_median = float(median(member_ofs)) if member_ofs else 0.0
    proposed_rate_median = float(median(rates)) if rates else 1.0
    return PeerStats(
        pool_size=pool_size,
        member_of_median=member_of_median,
        proposed_rate_median=proposed_rate_median,
    )


# ---------------------------------------------------------------------------
# PoSe status classification (for summary)
# ---------------------------------------------------------------------------


def classify_pose_status(report: dict[str, Any], core_lo: int | None = None) -> str:
    """
    Classify PoSe / eligibility status from per-validator pose_events + protx info.

    Precedence (first match wins):

    - deregistered_in_window  MN no longer in the registry at tip but was at core_lo
    - currently_banned        PoSeBanHeight > PoSeRevivedHeight at tip
    - revived_in_window       at least one revive event falls inside the window
    - registered_in_window    registeredHeight > core_lo (new MN during the window)
    - active_whole_window     fully eligible the entire window (no PoSe events, or
                              only historical events before the window started)

    The `registered_in_window` and `deregistered_in_window` buckets are
    diagnostic *eligibility* categories, separate from performance issues.
    Dereg takes precedence over ban/registered because a MN that is no longer
    in the registry is the most specific statement we can make about it.

    Previously emitted `never` and `revived_before_window` are now unified
    into `active_whole_window` — both represent validators that were fully
    eligible for the entire window and follow the same theoretical selection
    distribution under random sortition.
    """
    el = report.get("eligibility", {})
    pose_events = el.get("pose_events", []) or []
    tip = report.get("_pose_state_at_tip", {}) or {}
    ban_h = tip.get("PoSeBanHeight")
    rev_h = tip.get("PoSeRevivedHeight")
    try:
        ban_h = int(ban_h) if ban_h is not None else -1
    except (TypeError, ValueError):
        ban_h = -1
    try:
        rev_h = int(rev_h) if rev_h is not None else -1
    except (TypeError, ValueError):
        rev_h = -1

    # Dereg beats every other state: the MN no longer exists at tip.
    if el.get("deregistered_core_height") is not None:
        return "deregistered_in_window"

    if ban_h > 0 and ban_h > rev_h:
        return "currently_banned"
    if any(ev.get("revived_at_core_height") for ev in pose_events):
        return "revived_in_window"

    reg_h = el.get("registered_height")
    try:
        reg_h_int = int(reg_h) if reg_h is not None else 0
    except (TypeError, ValueError):
        reg_h_int = 0
    if core_lo is not None and reg_h_int > core_lo:
        return "registered_in_window"

    # Both "never had any PoSe event" and "revived before window" are fully
    # eligible throughout the window — consolidate into a single category.
    return "active_whole_window"


# ---------------------------------------------------------------------------
# HTML rendering (per-validator report)
# ---------------------------------------------------------------------------


HTML_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 24px; background: #f6f7f9; color: #1a1a1a; max-width: 1100px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e8ec; }
  .card { background: #191c22 !important; border-color: #2a2e36 !important; }
  th, td { border-color: #2a2e36 !important; }
  code, pre { background: #0b0d11 !important; color: #e6e8ec !important; }
  .muted { color: #8892a6 !important; }
  a { color: #8ab4f8; }
}
h1 { margin: 0 0 6px 0; font-size: 22px; }
h2 { margin: 28px 0 10px 0; font-size: 16px; letter-spacing: .3px; }
.muted { color: #667085; font-size: 13px; }
.card { background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; padding: 18px 22px;
        margin: 14px 0; }
.hero { display: grid; grid-template-columns: 1fr 2fr; gap: 24px; align-items: center; }
.score-big { font-size: 64px; font-weight: 700; line-height: 1; }
.band { display: inline-block; padding: 4px 12px; border-radius: 999px;
        font-weight: 600; font-size: 14px; letter-spacing: .4px; }
.band-Excellent { background: #d7f5dc; color: #0a6b24; }
.band-Good      { background: #dcecff; color: #1352b5; }
.band-Concerning{ background: #fff3cd; color: #8a5a00; }
.band-Poor      { background: #fde2e1; color: #9b2423; }
.band-NA        { background: #e4e7ec; color: #404756; }
@media (prefers-color-scheme: dark) {
  .band-Excellent { background: #12361a; color: #69e186; }
  .band-Good      { background: #17305e; color: #7fb2ff; }
  .band-Concerning{ background: #3d2f07; color: #efc66b; }
  .band-Poor      { background: #421315; color: #ff8d8d; }
  .band-NA        { background: #2a2e36; color: #aeb4c0; }
}
.gauges { display: grid; grid-template-columns: 1fr; gap: 10px; }
.gauge { display: grid; grid-template-columns: 130px 1fr 60px; align-items: center; gap: 12px;
         font-size: 13px; }
.bar { background: #e4e7ec; height: 10px; border-radius: 5px; overflow: hidden; }
@media (prefers-color-scheme: dark) { .bar { background: #2a2e36; } }
.fill { height: 100%; background: linear-gradient(90deg, #4c9ef8, #3b82f6); border-radius: 5px; }
.grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.stat { padding: 10px 12px; border: 1px solid #e4e7ec; border-radius: 8px; }
@media (prefers-color-scheme: dark) { .stat { border-color: #2a2e36; } }
.stat .k { font-size: 12px; color: #667085; }
.stat .v { font-size: 22px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 8px; border-bottom: 1px solid #e4e7ec; text-align: left; vertical-align: top; }
th { font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: #667085; }
code, pre { background: #f1f3f6; padding: 2px 5px; border-radius: 4px; font-size: 12px; }
.copy { cursor: pointer; user-select: all; }
.copy:hover { text-decoration: underline; }
details { margin: 8px 0; }
summary { cursor: pointer; font-weight: 600; font-size: 14px; padding: 4px 0; }
.row-skipped td { background: rgba(255, 80, 80, 0.08); }
.row-inconclusive td { background: rgba(255, 200, 0, 0.08); }
footer { margin-top: 30px; font-size: 12px; color: #667085; }
"""


HTML_JS = """
document.querySelectorAll('.copy').forEach(el => {
  el.addEventListener('click', () => {
    const text = el.dataset.full || el.textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(text);
  });
});
"""


def short(h: str, n: int = 12) -> str:
    return h[:n] + "..." + h[-4:] if len(h) > n + 4 else h


def gauge(label: str, val: float | None) -> str:
    if val is None:
        return (
            f'<div class="gauge"><div>{html_mod.escape(label)}</div>'
            f'<div class="bar"></div><div>N/A</div></div>'
        )
    pct = max(0.0, min(1.0, val)) * 100
    return (
        f'<div class="gauge"><div>{html_mod.escape(label)}</div>'
        f'<div class="bar"><div class="fill" style="width:{pct:.1f}%"></div></div>'
        f"<div>{val:.3f}</div></div>"
    )


def stat_box(label: str, value: str) -> str:
    return (
        f'<div class="stat"><div class="k">{html_mod.escape(label)}</div>'
        f'<div class="v">{html_mod.escape(str(value))}</div></div>'
    )


def render_hash(h: str | None) -> str:
    if not h:
        return "-"
    return (
        f'<code class="copy" data-full="{html_mod.escape(h)}" '
        f'title="click to copy">{html_mod.escape(short(h))}</code>'
    )


def render_html(report: dict[str, Any]) -> str:
    s = report["scores"]
    w = report["window"]
    el = report["eligibility"]
    qs = report["quorum_stats"]
    d = report["detail"]

    composite_display = f"{s['composite']:.3f}" if s["composite"] is not None else "N/A"
    band_class = "band-" + (s["band"].replace(" ", "") if s["band"] else "NA")

    gauges_html = (
        gauge("Selection", s["selection"])
        + gauge("Participation", s["participation"])
        + gauge("Liveness", s["liveness"])
    )

    stats_html = "".join(
        [
            stat_box("Quorums in window", qs["quorums_in_window"]),
            stat_box("Member of", qs["member_of"]),
            stat_box("MET", qs["met"]),
            stat_box("SKIPPED", qs["skipped"]),
            stat_box("Inconclusive", qs["inconclusive"]),
            stat_box("Round misses on target", qs["round_misses_on_target"]),
            stat_box(
                "Peer member-of median",
                f"{qs['peer_member_of_median']:.1f}"
                if qs["peer_member_of_median"]
                else "skipped",
            ),
            stat_box(
                "Peer proposed-rate median",
                f"{qs['peer_proposed_rate_median']:.3f}"
                if qs["peer_proposed_rate_median"] is not None
                else "skipped",
            ),
        ]
    )

    pose_rows = ""
    for ev in el["pose_events"]:
        pose_rows += (
            "<tr>"
            f"<td>{ev.get('banned_at_core_height') or '-'}</td>"
            f"<td>{ev.get('revived_at_core_height') or '-'}</td>"
            f"<td>{ev.get('platform_range_excluded') or '-'}</td>"
            f"<td>{ev.get('duration_seconds', 0)}s "
            f"({ev.get('duration_seconds', 0) / 86400:.2f}d)</td>"
            "</tr>"
        )
    if not pose_rows:
        pose_rows = (
            '<tr><td colspan="4" class="muted">No PoSe events in window.</td></tr>'
        )

    mq_rows = ""
    for q in d["member_quorums"]:
        cls = (
            "row-skipped"
            if q["status"] == "SKIPPED"
            else "row-inconclusive"
            if q["status"] == "INCONCLUSIVE"
            else ""
        )
        mq_rows += (
            f'<tr class="{cls}">'
            f"<td>{render_hash(q['quorum_hash'])}</td>"
            f"<td>{q['range'][0]} – {q['range'][1]}</td>"
            f"<td>{q['ts'][0]} – {q['ts'][1]}</td>"
            f"<td>{q.get('expected_slot_height') or '-'}</td>"
            f"<td>{q['actual_proposals']}</td>"
            f"<td><strong>{q['status']}</strong></td>"
            "</tr>"
        )
    if not mq_rows:
        mq_rows = '<tr><td colspan="6" class="muted">Target was not a member of any quorum in this window.</td></tr>'

    sk_rows = ""
    for q in d["skipped_quorums"]:
        sk_rows += (
            '<tr class="row-skipped">'
            f"<td>{render_hash(q['quorum_hash'])}</td>"
            f"<td>{q['range'][0]} – {q['range'][1]}</td>"
            f"<td>{q.get('expected_slot_height') or '-'}</td>"
            f"<td>{render_hash(q.get('cover_up'))}</td>"
            "</tr>"
        )
    if not sk_rows:
        sk_rows = (
            '<tr><td colspan="4" class="muted">No skipped quorums. Nice.</td></tr>'
        )

    inc_rows = ""
    for q in d.get("inconclusive_quorums", []):
        inc_rows += (
            '<tr class="row-inconclusive">'
            f"<td>{render_hash(q['quorum_hash'])}</td>"
            f"<td>{q['range'][0]} – {q['range'][1]}</td>"
            f"<td>{q.get('reason', 'boundary')}</td>"
            "</tr>"
        )

    # Strip the internal _pose_state_at_tip blob from the embedded public JSON.
    public_report = {k: v for k, v in report.items() if not k.startswith("_")}
    json_blob = json.dumps(public_report, indent=2).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fairness report – {short(report["protx"])}</title>
<style>{HTML_CSS}</style>
</head>
<body>

<header>
  <h1>Dash Platform Validator Fairness Report</h1>
  <div class="muted">
    Target: <code class="copy" data-full="{html_mod.escape(report["protx"])}">
    {html_mod.escape(short(report["protx"], 20))}</code>
    &middot; Window: {w["from_time"]} → {w["to_time"]} ({w["days"]} days)
    &middot; Generated: {report["generated_at"]}
  </div>
</header>

<section class="card hero">
  <div>
    <div class="muted">Composite</div>
    <div class="score-big">{composite_display}</div>
    <div style="margin-top:8px;"><span class="band {band_class}">{
        s["band"]
    }</span></div>
  </div>
  <div class="gauges">
    {gauges_html}
    <div class="muted" style="font-size:12px;">
      Weights: selection {DEFAULT_COMPOSITE_WEIGHTS["selection"]:.2f},
      participation {DEFAULT_COMPOSITE_WEIGHTS["participation"]:.2f},
      liveness {DEFAULT_COMPOSITE_WEIGHTS["liveness"]:.2f};
      precommit axis not included (v{ALGO_VERSION}).
    </div>
  </div>
</section>

<section class="card">
  <h2>Eligibility</h2>
  <div class="grid-4">
    {stat_box("Node type", el["node_type"] or "?")}
    {stat_box("Registered (core H)", el.get("registered_height") or "?")}
    {stat_box("Eligible seconds", f"{el['eligible_seconds']}")}
    {stat_box("Eligible fraction", f"{el['eligible_fraction']:.3f}")}
  </div>
  <h2 style="margin-top:16px;">PoSe events</h2>
  <table>
    <thead><tr>
      <th>Banned @ core H</th><th>Revived @ core H</th>
      <th>Platform range excluded</th><th>Duration</th>
    </tr></thead>
    <tbody>{pose_rows}</tbody>
  </table>
</section>

<section class="card">
  <h2>Quorum stats</h2>
  <div class="grid-4">{stats_html}</div>
</section>

<section class="card">
  <h2>Skipped quorums</h2>
  <table>
    <thead><tr>
      <th>Quorum</th><th>Range</th><th>Expected slot H</th><th>Cover-up proposer</th>
    </tr></thead>
    <tbody>{sk_rows}</tbody>
  </table>
</section>

<section class="card">
  <details>
    <summary>Member-of quorums ({len(d["member_quorums"])})</summary>
    <table>
      <thead><tr>
        <th>Quorum</th><th>Platform range</th><th>Time</th>
        <th>Expected slot H</th><th>Proposed</th><th>Status</th>
      </tr></thead>
      <tbody>{mq_rows}</tbody>
    </table>
  </details>
</section>

{
        f'''<section class="card">
  <h2>Inconclusive quorums</h2>
  <table>
    <thead><tr><th>Quorum</th><th>Range</th><th>Reason</th></tr></thead>
    <tbody>{inc_rows}</tbody>
  </table>
</section>'''
        if inc_rows
        else ""
    }

<footer>
  <p>Algorithm v{
        ALGO_VERSION
    }. Caveats: proposer rotation model assumes sorted-ascending
     validator list from Tenderdash /validators; BLS threshold signature identity of signers
     is not verified; precommit-signer axis is not implemented. PoSe replay walks historical
     <code>protx info</code> state via bisection — multi-ban timelines in the window are
     captured.</p>
</footer>

<script type="application/json" id="data">{json_blob}</script>
<script>{HTML_JS}</script>
</body></html>
"""
    return html


# ---------------------------------------------------------------------------
# Batch index.html (scatter chart + summary table)
# ---------------------------------------------------------------------------


INDEX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dash Platform fairness — all validators</title>
<style>__CSS__</style>
</head>
<body>

<header>
  <h1>Dash Platform validator fairness</h1>
  <div class="muted" id="header-meta">Loading…</div>
</header>

<section class="card" id="summary-card">
  <div class="grid-4" id="band-stats"></div>
</section>

<section class="card" id="dist-card">
  <h2>Quorum-selection distribution &mdash; validators active the whole window</h2>
  <p class="legend-note" id="dist-subtitle">Loading&hellip;</p>
  <div id="dist-wrap" style="position:relative;overflow-x:auto;">
    <svg id="dist-hist" role="img" aria-label="Histogram of member_of counts with theoretical expected distribution curve"></svg>
    <svg id="dist-strip" role="img" aria-label="Strip plot of individual validator member_of values"></svg>
    <div id="dist-tooltip" class="tooltip" style="display:none;"></div>
  </div>
  <p class="legend-note" id="dist-footnote" style="margin-top:6px;"></p>
</section>

<section class="card">
  <h2>Proposed blocks vs PoSe status</h2>
  <div id="chart-wrap" style="position:relative;overflow-x:auto;">
    <svg id="chart" width="980" height="500" role="img" aria-label="Scatter chart of proposed blocks vs PoSe status"></svg>
    <div id="tooltip" class="tooltip" style="display:none;"></div>
  </div>
  <div class="legend">
    <span class="legend-item"><span class="sw sw-Excellent"></span>Excellent</span>
    <span class="legend-item"><span class="sw sw-Good"></span>Good</span>
    <span class="legend-item"><span class="sw sw-Concerning"></span>Concerning</span>
    <span class="legend-item"><span class="sw sw-Poor"></span>Poor</span>
  </div>
  <p class="legend-note">
    X axis groups validators by PoSe / eligibility state. The first three
    buckets (<em>Active whole window</em>,
    <em>Revived in window</em>, <em>Currently banned</em>) reflect
    <strong>performance-oriented</strong> classes — their scores are
    comparable. The two buckets after the divider
    (<em>Registered in window</em>, <em>Deregistered in window</em>) are
    <strong>eligibility-limited</strong>: those validators were live for only
    part of the window and are normalised via <code>eligible_fraction</code>,
    so their bands should not be compared directly with the performance
    group. Dot colour = overall band.
  </p>
</section>

<div class="footnote-block">
  <p>Algorithm v__ALGO__. Each dot is one validator; hover for details, click to open
     platform-explorer.com. Per-validator JSON and HTML reports live alongside this file.</p>
</div>

<section class="card">
  <h2>All validators</h2>
  <div class="filter-row">
    <label for="validator-filter" class="sr-only">Filter by protx hash</label>
    <input type="search" id="validator-filter" placeholder="Filter by protx hash…" aria-label="Filter by protx hash" autocomplete="off">
    <span class="muted" id="filter-count" aria-live="polite"></span>
  </div>
  <div class="table-wrap">
    <table id="tbl">
      <thead>
        <tr>
          <th>#</th>
          <th data-col="protx" data-type="lex">Pro TX</th>
          <th data-col="band" data-type="lex">Band</th>
          <th data-col="composite" data-type="num">Composite</th>
          <th data-col="member_of" data-type="num">Member of</th>
          <th data-col="met" data-type="num">MET</th>
          <th data-col="delta" data-type="num">&#916; median</th>
          <th data-col="skipped" data-type="num">SKIPPED</th>
          <th data-col="pose_status" data-type="lex">PoSe status</th>
          <th>Report</th>
        </tr>
      </thead>
      <tbody id="tbl-body"></tbody>
    </table>
  </div>
</section>

<footer class="site-footer" role="contentinfo">
  <p>
    &copy; 2026 Lukasz Klimek &middot;
    vibe-coded with <a href="https://github.com/lklimek/claudius">Claudius the Magnificent</a>
    and <a href="https://claude.com/claude-code">Claude Code</a> &middot;
    <a href="https://github.com/lklimek/dash-platform-fairness/">source on GitHub</a>
  </p>
</footer>

<script type="application/json" id="boot-meta">__BOOT_META__</script>
<script>__JS__</script>
</body></html>
"""


INDEX_HTML_CSS = """
:root {
  color-scheme: light dark;
  --delta-pos: #1a7f37;
  --delta-neg: #cf222e;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 24px; background: #f6f7f9; color: #1a1a1a; max-width: 1200px; }
@media (prefers-color-scheme: dark) {
  :root { --delta-pos: #57ab5a; --delta-neg: #ff8d8d; }
  body { background: #0f1115; color: #e6e8ec; }
  .card { background: #191c22 !important; border-color: #2a2e36 !important; }
  th, td { border-color: #2a2e36 !important; }
  .muted { color: #8892a6 !important; }
  a { color: #8ab4f8; }
  .tooltip { background: #0b0d11 !important; color: #e6e8ec !important;
             border-color: #2a2e36 !important; }
  text.axis, .axis text { fill: #aeb4c0; }
  .axis line, .axis path { stroke: #2a2e36; }
  .gridline { stroke: #2a2e36; }
  #chart-bg { fill: #0b0d11; }
}
h1 { margin: 0 0 6px 0; font-size: 24px; }
h2 { margin: 4px 0 14px 0; font-size: 16px; letter-spacing: .3px; }
.muted { color: #667085; font-size: 13px; }
.card { background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; padding: 18px 22px;
        margin: 14px 0; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.stat { padding: 10px 12px; border: 1px solid #e4e7ec; border-radius: 8px; }
@media (prefers-color-scheme: dark) { .stat { border-color: #2a2e36; } }
.stat .k { font-size: 12px; color: #667085; }
.stat .v { font-size: 20px; font-weight: 600; }
.stat .v small { font-size: 12px; color: #667085; font-weight: 400; }
.band { display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-weight: 600; font-size: 12px; letter-spacing: .3px; }
.band-Excellent { background: #d7f5dc; color: #0a6b24; }
.band-Good      { background: #dcecff; color: #1352b5; }
.band-Concerning{ background: #fff3cd; color: #8a5a00; }
.band-Poor      { background: #fde2e1; color: #9b2423; }
.band-NA        { background: #e4e7ec; color: #404756; }
@media (prefers-color-scheme: dark) {
  .band-Excellent { background: #12361a; color: #69e186; }
  .band-Good      { background: #17305e; color: #7fb2ff; }
  .band-Concerning{ background: #3d2f07; color: #efc66b; }
  .band-Poor      { background: #421315; color: #ff8d8d; }
  .band-NA        { background: #2a2e36; color: #aeb4c0; }
}
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 8px; border-bottom: 1px solid #e4e7ec; text-align: left; vertical-align: middle; }
th { font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: #667085; }
tbody tr:hover td { background: rgba(100, 150, 250, 0.06); }
code { background: #f1f3f6; padding: 2px 5px; border-radius: 4px; font-size: 12px;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (prefers-color-scheme: dark) { code { background: #0b0d11; color: #e6e8ec; } }
a { color: #0a6bb5; text-decoration: none; }
a:hover { text-decoration: underline; }
.tooltip { position: absolute; background: #fff; color: #1a1a1a; border: 1px solid #e4e7ec;
           border-radius: 6px; padding: 8px 10px; font-size: 12px; pointer-events: none;
           box-shadow: 0 2px 10px rgba(0, 0, 0, .1); z-index: 10; max-width: 380px;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.tooltip b { font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
.legend { margin-top: 10px; font-size: 13px; color: #667085; display: flex; gap: 16px;
          flex-wrap: wrap; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-note { margin: 8px 0 0 0; font-size: 12px; color: #667085; line-height: 1.5; max-width: 900px; }
.legend-note em { font-style: normal; color: #404756; }
@media (prefers-color-scheme: dark) { .legend-note em { color: #cdd3dc; } }
.sw { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
.sw-Excellent   { background: #2b9348; }
.sw-Good        { background: #2f6fe4; }
.sw-Concerning  { background: #e0a000; }
.sw-Poor        { background: #cc3b33; }
text.axis, .axis text { fill: #667085; font-size: 11px; font-family: -apple-system, sans-serif; }
.axis line, .axis path { stroke: #c8ccd1; stroke-width: 1; fill: none; }
.gridline { stroke: #e4e7ec; stroke-width: 1; stroke-dasharray: 2, 3; }
.dot { cursor: pointer; stroke: #fff; stroke-width: 0.5; }
@media (prefers-color-scheme: dark) { .dot { stroke: #0f1115; } }
.dot:hover { stroke-width: 2; stroke: #000; }
@media (prefers-color-scheme: dark) { .dot:hover { stroke: #fff; } }
.group-sep { stroke: #8892a6; stroke-width: 1.2; stroke-dasharray: 4, 3; opacity: 0.75; }
@media (prefers-color-scheme: dark) { .group-sep { stroke: #5d6776; } }
text.group-caption { font-size: 11px; font-weight: 600; letter-spacing: .2px; fill: #404756; }
@media (prefers-color-scheme: dark) { text.group-caption { fill: #aeb4c0; } }
.eligibility-mark { cursor: help; color: #8892a6; font-size: 11px; margin-left: 2px; }
@media (prefers-color-scheme: dark) { .eligibility-mark { color: #6a7380; } }
.footnote-block { border-top: 1px solid #e4e7ec; margin: 4px 0 0 0; padding: 10px 0 4px 0;
                  font-size: 12px; color: #667085; }
@media (prefers-color-scheme: dark) { .footnote-block { border-color: #2a2e36; color: #8892a6; } }
.footnote-block p { margin: 0; }
.filter-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
#validator-filter { padding: 6px 10px; border: 1px solid #c8ccd1; border-radius: 6px;
                    font-size: 13px; width: 320px; background: inherit; color: inherit; }
@media (prefers-color-scheme: dark) { #validator-filter { border-color: #2a2e36; background: #0b0d11; } }
#filter-count { font-size: 12px; }
th[data-col] { cursor: pointer; user-select: none; }
th[data-col]:hover { background: rgba(100,150,250,0.07); }
@media (prefers-color-scheme: dark) { th[data-col]:hover { background: rgba(100,150,250,0.12); } }
.sort-glyph { margin-left: 4px; font-size: 10px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
           overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.report-links { font-size: 11px; white-space: nowrap; }
.site-footer { margin: 32px 0 16px 0; padding-top: 16px; border-top: 1px solid #e4e7ec;
               font-size: 12px; color: #667085; text-align: center; }
@media (prefers-color-scheme: dark) { .site-footer { border-color: #2a2e36; color: #8892a6; } }
.site-footer a { color: inherit; text-decoration: underline; text-decoration-thickness: 1px;
                 text-underline-offset: 2px; }
.site-footer a:hover { color: #0366d6; }
@media (prefers-color-scheme: dark) { .site-footer a:hover { color: #79b8ff; } }
.site-footer p { margin: 0; }
.report-links a { color: inherit; opacity: 0.75; }
.report-links a:hover { opacity: 1; text-decoration: underline; }
.report-sep { color: #c8ccd1; margin: 0 3px; }
@media (prefers-color-scheme: dark) { .report-sep { color: #2a2e36; } }
.delta-pos { color: var(--delta-pos); }
.delta-neg { color: var(--delta-neg); }
/* Distribution chart */
#dist-hist, #dist-strip { display: block; width: 100%; }
.dist-bar { fill: #7eb8f7; fill-opacity: 0.65; }
@media (prefers-color-scheme: dark) { .dist-bar { fill: #3a6db5; fill-opacity: 0.7; } }
.dist-bar.highlight { fill-opacity: 1; }
.dist-curve { fill: none; stroke: #e0a000; stroke-width: 2; stroke-dasharray: 6,3; }
@media (prefers-color-scheme: dark) { .dist-curve { stroke: #efc66b; } }
.dist-mean-line { stroke: #cc3b33; stroke-width: 1.5; stroke-dasharray: 4,3; }
@media (prefers-color-scheme: dark) { .dist-mean-line { stroke: #ff8d8d; } }
.dist-sigma-band { fill: #2f6fe4; fill-opacity: 0.06; }
@media (prefers-color-scheme: dark) { .dist-sigma-band { fill: #2f6fe4; fill-opacity: 0.1; } }
.dist-dot { cursor: pointer; stroke-width: 0.8; }
.dist-dot:hover { stroke-width: 2.5; }
@media (prefers-color-scheme: dark) { .dist-dot:hover { stroke: #fff; } }
text.dist-axis, .dist-axis text { fill: #667085; font-size: 11px;
  font-family: -apple-system, sans-serif; }
@media (prefers-color-scheme: dark) { text.dist-axis, .dist-axis text { fill: #aeb4c0; } }
.dist-axis line, .dist-axis path { stroke: #c8ccd1; stroke-width: 1; fill: none; }
@media (prefers-color-scheme: dark) { .dist-axis line, .dist-axis path { stroke: #2a2e36; } }
.dist-gridline { stroke: #e4e7ec; stroke-width: 1; stroke-dasharray: 2,3; }
@media (prefers-color-scheme: dark) { .dist-gridline { stroke: #2a2e36; } }
.dist-legend { margin-top: 6px; font-size: 12px; color: #667085;
  display: flex; gap: 16px; flex-wrap: wrap; }
.dist-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.dist-swatch { display: inline-block; width: 16px; height: 3px; }
.dist-swatch-bar { background: #7eb8f7; height: 10px; border-radius: 2px; }
@media (prefers-color-scheme: dark) { .dist-swatch-bar { background: #3a6db5; } }
.dist-swatch-curve { background: #e0a000; height: 3px; border-top: 2px dashed #e0a000; }
@media (prefers-color-scheme: dark) { .dist-swatch-curve { border-top-color: #efc66b; } }
.dist-swatch-mean { background: #cc3b33; height: 3px; border-top: 2px dashed #cc3b33; }
@media (prefers-color-scheme: dark) { .dist-swatch-mean { border-top-color: #ff8d8d; } }
"""


INDEX_HTML_JS = r"""
(async () => {
  const META = JSON.parse(document.getElementById('boot-meta').textContent);
  const BAND_COLORS = {
    Excellent: '#2b9348',
    Good:      '#2f6fe4',
    Concerning:'#e0a000',
    Poor:      '#cc3b33',
    'N/A':     '#8892a6',
  };

  // ---- Selection distribution chart (Panel 1: histogram + curve, Panel 2: strip) ----
  (function renderDist() {
    const D = META.dist;
    if (!D || !D.dots || D.dots.length < 2) return;

    const SVG_NS = 'http://www.w3.org/2000/svg';
    function mkSvg(tag, attrs, parent) {
      const el = document.createElementNS(SVG_NS, tag);
      for (const k in attrs) el.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(el);
      return el;
    }

    const mu = D.mu;
    const sigma = D.sigma;
    const normScale = D.norm_scale;  // n / (sigma * sqrt(2π))
    const n = D.n;

    // Subtitle
    document.getElementById('dist-subtitle').textContent =
        'Theoretical curve shows expected distribution under random sortition ' +
        '(validators active the whole window). ' +
        'μ = ' + mu.toFixed(2) + ', σ = ' + sigma.toFixed(2) +
        ', n = ' + n + '.';

    // --- Bin the data (bin width 2) ---
    const BIN_W = 2;
    const xMin = Math.floor(Math.min(...D.dots.map((d) => d.member_of)) / BIN_W) * BIN_W;
    const xMax = Math.ceil(Math.max(...D.dots.map((d) => d.member_of)) / BIN_W) * BIN_W;
    const binCount = (xMax - xMin) / BIN_W;
    const bins = new Array(binCount).fill(null).map((_, i) => ({
      x0: xMin + i * BIN_W,
      x1: xMin + (i + 1) * BIN_W,
      dots: [],
    }));
    for (const dot of D.dots) {
      const bi = Math.min(Math.floor((dot.member_of - xMin) / BIN_W), binCount - 1);
      if (bi >= 0) bins[bi].dots.push(dot);
    }
    const maxCount = Math.max(...bins.map((b) => b.dots.length), 1);

    // --- Panel 1: Histogram ---
    const histSvg = document.getElementById('dist-hist');
    const HW = histSvg.parentElement.clientWidth || 860;
    const HH = 200;
    histSvg.setAttribute('width', HW);
    histSvg.setAttribute('height', HH);

    const HM = {top: 20, right: 20, bottom: 36, left: 44};
    const hPlotW = HW - HM.left - HM.right;
    const hPlotH = HH - HM.top - HM.bottom;

    // x scale: linear over [xMin, xMax]
    const xRange = xMax - xMin;
    const hxScale = (v) => HM.left + ((v - xMin) / xRange) * hPlotW;
    const hyScale = (v) => HM.top + hPlotH - (v / (maxCount * 1.15)) * hPlotH;
    const yTop = Math.ceil(maxCount * 1.15);

    // Sigma bands (±1σ and ±2σ)
    for (const [lo, hi] of [[mu - 2*sigma, mu + 2*sigma], [mu - sigma, mu + sigma]]) {
      const bx = Math.max(hxScale(lo), HM.left);
      const bw = Math.min(hxScale(hi), HM.left + hPlotW) - bx;
      mkSvg('rect', {
        class: 'dist-sigma-band',
        x: bx, y: HM.top, width: Math.max(bw, 0), height: hPlotH,
      }, histSvg);
    }

    // Gridlines + y-axis ticks
    const nYTicks = 4;
    const tickStep = Math.max(1, Math.ceil(yTop / nYTicks));
    for (let v = 0; v <= yTop; v += tickStep) {
      const y = hyScale(v);
      mkSvg('line', {class: 'dist-gridline', x1: HM.left, x2: HM.left + hPlotW, y1: y, y2: y}, histSvg);
      const t = mkSvg('text', {class: 'dist-axis', x: HM.left - 6, y: y + 4, 'text-anchor': 'end'}, histSvg);
      t.textContent = v;
    }

    // Bars (track bin index per bar for highlight)
    const barEls = [];
    bins.forEach((bin, bi) => {
      const bx = hxScale(bin.x0) + 1;
      const bw = Math.max(hxScale(bin.x1) - hxScale(bin.x0) - 2, 1);
      const by = hyScale(bin.dots.length);
      const bh = HM.top + hPlotH - by;
      const rect = mkSvg('rect', {
        class: 'dist-bar',
        x: bx, y: by, width: bw, height: Math.max(bh, 0),
        'data-bi': bi,
      }, histSvg);
      barEls.push(rect);
    });

    // Theoretical curve (scale PDF by normScale * BIN_W)
    const curveScale = normScale * BIN_W;
    const pts = D.curve.map((p) => {
      const cx = hxScale(p.x);
      const cy = hyScale(p.y * curveScale);
      return cx + ',' + cy;
    }).join(' ');
    mkSvg('polyline', {class: 'dist-curve', points: pts, 'vector-effect': 'non-scaling-stroke'}, histSvg);

    // Mean line
    const mx = hxScale(mu);
    mkSvg('line', {class: 'dist-mean-line', x1: mx, x2: mx, y1: HM.top, y2: HM.top + hPlotH}, histSvg);
    const mlabel = mkSvg('text', {
      class: 'dist-axis', x: mx + 4, y: HM.top + 12, 'text-anchor': 'start',
      style: 'font-size:10px;',
    }, histSvg);
    mlabel.textContent = 'μ=' + mu.toFixed(1);

    // X-axis ticks
    mkSvg('line', {class: 'dist-axis', x1: HM.left, x2: HM.left + hPlotW, y1: HM.top + hPlotH, y2: HM.top + hPlotH}, histSvg);
    mkSvg('line', {class: 'dist-axis', x1: HM.left, x2: HM.left, y1: HM.top, y2: HM.top + hPlotH}, histSvg);
    for (let v = Math.ceil(xMin / 5) * 5; v <= xMax; v += 5) {
      const tx = hxScale(v);
      mkSvg('line', {class: 'dist-axis', x1: tx, x2: tx, y1: HM.top + hPlotH, y2: HM.top + hPlotH + 4}, histSvg);
      const t = mkSvg('text', {class: 'dist-axis', x: tx, y: HM.top + hPlotH + 16, 'text-anchor': 'middle'}, histSvg);
      t.textContent = v;
    }

    // Y axis label
    const yLabelH = mkSvg('text', {
      class: 'dist-axis',
      x: -(HM.top + hPlotH / 2), y: 12, 'text-anchor': 'middle', transform: 'rotate(-90)',
    }, histSvg);
    yLabelH.textContent = 'Count';

    // --- Panel 2: Strip plot ---
    const stripSvg = document.getElementById('dist-strip');
    const SW = HW;
    const SH = 70;
    stripSvg.setAttribute('width', SW);
    stripSvg.setAttribute('height', SH);

    const SM = {top: 10, right: 20, bottom: 28, left: 44};
    const sPlotW = SW - SM.left - SM.right;
    const sPlotH = SH - SM.top - SM.bottom;
    const sCy = SM.top + sPlotH / 2;
    const sxScale = (v) => SM.left + ((v - xMin) / xRange) * sPlotW;

    mkSvg('line', {class: 'dist-axis', x1: SM.left, x2: SM.left + sPlotW, y1: SH - SM.bottom, y2: SH - SM.bottom}, stripSvg);

    // X-axis ticks (shared with hist)
    for (let v = Math.ceil(xMin / 5) * 5; v <= xMax; v += 5) {
      const tx = sxScale(v);
      mkSvg('line', {class: 'dist-axis', x1: tx, x2: tx, y1: SH - SM.bottom, y2: SH - SM.bottom + 4}, stripSvg);
      const t = mkSvg('text', {class: 'dist-axis', x: tx, y: SH - SM.bottom + 14, 'text-anchor': 'middle'}, stripSvg);
      t.textContent = v;
    }

    // Tooltip
    const tip = document.getElementById('dist-tooltip');
    function showDistTip(evt, dot) {
      tip.style.display = 'block';
      const zSign = dot.z >= 0 ? '+' : '';
      tip.innerHTML =
          '<b>' + esc(dot.protx) + '</b><br>' +
          'member_of: ' + dot.member_of + '<br>' +
          'z = ' + zSign + dot.z.toFixed(2) + 'σ<br>' +
          'band: ' + esc(dot.band) + '<br>' +
          'status: ' + esc(dot.pose_status);
      const rect = stripSvg.getBoundingClientRect();
      tip.style.left = (evt.clientX - rect.left + 14) + 'px';
      tip.style.top  = (evt.clientY - rect.top  + 14) + 'px';
    }
    function hideDistTip() { tip.style.display = 'none'; }

    // Dots — deterministic vertical jitter using same hash trick as scatter
    function jitterFor(protx) {
      if (!protx || protx.length < 8) return 0;
      let h = 0;
      for (let i = 0; i < Math.min(protx.length, 16); i++) {
        h = (h * 33 + protx.charCodeAt(i)) >>> 0;
      }
      return ((h & 0xffff) / 0xffff) * 2 - 1;
    }

    const dotEls = [];  // [{el, bi, dot}]
    for (const dot of D.dots) {
      const bi = Math.min(Math.floor((dot.member_of - xMin) / BIN_W), binCount - 1);
      const cx = sxScale(dot.member_of);
      const jy = jitterFor(dot.protx) * (sPlotH * 0.38);
      const cy = sCy + jy;
      const color = BAND_COLORS[dot.band] || '#8892a6';

      const el = mkSvg('circle', {
        class: 'dist-dot',
        cx: cx, cy: cy, r: 4,
        fill: color, 'fill-opacity': '0.75',
        stroke: color,
      }, stripSvg);

      el.addEventListener('mousemove', (e) => { showDistTip(e, dot); });
      el.addEventListener('mouseleave', hideDistTip);
      el.addEventListener('click', () => {
        const href = dot.report_html || explorerUrl(dot.protx);
        window.open(href, '_blank', 'noopener');
      });
      dotEls.push({el, bi, dot});
    }

    // Histogram bar hover → highlight dots in that bin (optional interaction)
    barEls.forEach((rect, bi) => {
      rect.addEventListener('mouseenter', () => {
        for (const d of dotEls) {
          if (d.bi !== bi) continue;
          d.el.setAttribute('r', '6');
          d.el.setAttribute('fill-opacity', '1');
        }
      });
      rect.addEventListener('mouseleave', () => {
        for (const d of dotEls) {
          if (d.bi !== bi) continue;
          d.el.setAttribute('r', '4');
          d.el.setAttribute('fill-opacity', '0.75');
        }
      });
    });

    // Legend + footnote
    document.getElementById('dist-footnote').innerHTML =
        '<span class="dist-legend">' +
        '<span class="dist-legend-item"><span class="dist-swatch dist-swatch-bar">&nbsp;</span> Observed count per bin (width 2)</span>' +
        '<span class="dist-legend-item"><span class="dist-swatch dist-swatch-curve"></span> Expected distribution (theoretical normal, μ='+mu.toFixed(2)+', σ='+sigma.toFixed(2)+')</span>' +
        '<span class="dist-legend-item"><span class="dist-swatch dist-swatch-mean"></span> Mean (μ)</span>' +
        '<span class="dist-legend-item">Dot colour = band; each dot = one validator</span>' +
        '</span>';
  })();

  // Category layout: first 3 are "performance" buckets (diagnostic of
  // actual behaviour), last 2 are "eligibility" buckets (limited window
  // coverage — not a performance signal). A visual separator between them
  // stops operators from mistakenly comparing apples to oranges.
  // Legacy 6-category summary.json (never + revived_before_window) is
  // normalized to active_whole_window by the Python renderer before embedding.
  const POSE_CATEGORIES = [
    {key: 'active_whole_window',       label: 'Active whole window',       group: 'performance'},
    {key: 'revived_in_window',         label: 'Revived in window',         group: 'performance'},
    {key: 'currently_banned',          label: 'Currently banned',          group: 'performance'},
    {key: 'registered_in_window',      label: 'Registered in window',      group: 'eligibility'},
    {key: 'deregistered_in_window',    label: 'Deregistered in window',    group: 'eligibility'},
  ];
  const LIMITED_ELIGIBILITY = new Set(['registered_in_window', 'deregistered_in_window']);
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function abbrev(h) {
    if (!h) return '—';
    const s = String(h);
    if (s.length <= 14) return s;
    return s.slice(0, 8) + '…' + s.slice(-4);
  }

  function explorerUrl(protx) {
    return 'https://platform-explorer.com/validator/' + encodeURIComponent(protx);
  }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // Deterministic x-jitter derived from protx hash hex prefix.
  function jitterFor(protx) {
    if (!protx || protx.length < 8) return 0;
    let h = 0;
    for (let i = 0; i < Math.min(protx.length, 16); i++) {
      h = (h * 33 + protx.charCodeAt(i)) >>> 0;
    }
    return ((h & 0xffff) / 0xffff) * 2 - 1;
  }

  let rows;
  try {
    const resp = await fetch('summary.json', {cache: 'no-store'});
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    // Support both new object format {window, validators} and legacy plain array.
    rows = Array.isArray(data) ? data : (data.validators || []);
  } catch (e) {
    document.getElementById('header-meta').textContent =
        'Failed to load summary.json: ' + e.message;
    return;
  }

  // Compute median of met values across all rows (fix #5, #9).
  function median(arr) {
    if (!arr.length) return 0;
    const s = arr.slice().sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  const metValues = rows.map((r) => Number(r.met || 0));
  const medianMet = median(metValues);

  // Header meta
  const total = rows.length;
  const bandCounts = {Excellent: 0, Good: 0, Concerning: 0, Poor: 0, 'N/A': 0};
  for (const r of rows) {
    const b = r.band || 'N/A';
    bandCounts[b] = (bandCounts[b] || 0) + 1;
  }
  const windowDesc = META.window_desc || '';
  document.getElementById('header-meta').innerHTML =
      `${total} validators  &middot;  ${esc(windowDesc)}  ` +
      `&middot;  generated ${esc(META.generated_at || '')}`;

  const bandStats = document.getElementById('band-stats');
  bandStats.innerHTML = '';
  for (const b of ['Excellent', 'Good', 'Concerning', 'Poor']) {
    const c = bandCounts[b] || 0;
    const pct = total ? ((c / total) * 100).toFixed(1) : '0.0';
    const div = document.createElement('div');
    div.className = 'stat';
    div.innerHTML = `<div class="k">${b}</div><div class="v">${c} <small>(${pct}%)</small></div>`;
    bandStats.appendChild(div);
  }

  // ---- Scatter chart ----
  const svg = document.getElementById('chart');
  const W = svg.clientWidth || 980;
  const H = svg.clientHeight || 480;
  const M = {top: 38, right: 80, bottom: 82, left: 56};
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  // X layout: N slots + a gap between the "performance" and "eligibility"
  // groups. Each slot gets one unit; the gap gets GAP_UNITS (0.6 feels like
  // a clear break without wasting space).
  const catCount = POSE_CATEGORIES.length;
  const GAP_UNITS = 0.6;
  const groupChangeIdx = POSE_CATEGORIES.findIndex((c) => c.group === 'eligibility');
  const hasSeparator = groupChangeIdx > 0 && groupChangeIdx < catCount;
  const totalUnits = catCount + (hasSeparator ? GAP_UNITS : 0);
  const unitW = plotW / totalUnits;
  // Offset in units for category i (0..catCount-1). The gap sits at
  // groupChangeIdx, so anything at or past it shifts by GAP_UNITS.
  function slotOffset(i) {
    return i + (hasSeparator && i >= groupChangeIdx ? GAP_UNITS : 0);
  }
  const xCenter = (i) => M.left + unitW * (slotOffset(i) + 0.5);
  const slotLeft = (i) => M.left + unitW * slotOffset(i);

  // Y scale: auto-range from met counts. Pad top.
  let yMax = 0;
  for (const r of rows) yMax = Math.max(yMax, Number(r.met || 0));
  yMax = Math.max(yMax, 1);
  const yPad = Math.ceil(yMax * 0.08) || 1;
  const yTop = yMax + yPad;
  const yScale = (v) => M.top + plotH - (v / yTop) * plotH;

  function addSvg(tag, attrs, parent) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    (parent || svg).appendChild(el);
    return el;
  }

  // Background rect for the plot area (dark-mode visibility)
  addSvg('rect', {id: 'chart-bg', x: 0, y: 0, width: W, height: H, fill: 'transparent'});

  // Gridlines + y-axis ticks (~6 ticks)
  const nTicks = 6;
  const tickStep = Math.max(1, Math.ceil(yTop / nTicks));
  for (let v = 0; v <= yTop; v += tickStep) {
    const y = yScale(v);
    addSvg('line', {
      class: 'gridline', x1: M.left, x2: M.left + plotW, y1: y, y2: y,
    });
    const t = addSvg('text', {
      class: 'axis', x: M.left - 8, y: y + 3, 'text-anchor': 'end',
    });
    t.textContent = v;
  }

  // X-axis baseline
  addSvg('line', {
    class: 'axis', x1: M.left, x2: M.left + plotW,
    y1: M.top + plotH, y2: M.top + plotH,
  });
  // Y-axis baseline
  addSvg('line', {
    class: 'axis', x1: M.left, x2: M.left,
    y1: M.top, y2: M.top + plotH,
  });

  // X-axis category labels + between-slot gridlines. Skip the gridline at
  // the group boundary — the explicit separator below replaces it and a
  // faint gridline next to a bold separator reads as noise.
  // Long labels are wrapped onto two lines so they don't overlap when the
  // chart is narrow. Heuristic: split on the first space if the label is
  // longer than 10 chars.
  function splitLabel(s) {
    if (s.length <= 10 || !s.includes(' ')) return [s];
    const i = s.indexOf(' ');
    return [s.slice(0, i), s.slice(i + 1)];
  }
  for (let i = 0; i < catCount; i++) {
    const cx = xCenter(i);
    const parts = splitLabel(POSE_CATEGORIES[i].label);
    const t = addSvg('text', {
      class: 'axis', x: cx, y: M.top + plotH + 18, 'text-anchor': 'middle',
    });
    parts.forEach((part, j) => {
      const ts = document.createElementNS(SVG_NS, 'tspan');
      ts.setAttribute('x', cx);
      ts.setAttribute('dy', j === 0 ? '0' : '1.2em');
      ts.textContent = part;
      t.appendChild(ts);
    });
    if (i > 0 && i !== groupChangeIdx) {
      addSvg('line', {
        class: 'gridline',
        x1: slotLeft(i), x2: slotLeft(i),
        y1: M.top, y2: M.top + plotH,
      });
    }
  }

  // Group separator + captions: visually split the chart into two
  // semantic halves. Dashed/thicker line sits in the middle of the gap.
  if (hasSeparator) {
    const sepX = slotLeft(groupChangeIdx) - (unitW * GAP_UNITS) / 2;
    addSvg('line', {
      class: 'group-sep',
      x1: sepX, x2: sepX,
      y1: M.top - 14, y2: M.top + plotH + 6,
    });
    // Performance caption (centred over the performance half)
    const perfMid = (xCenter(0) + xCenter(groupChangeIdx - 1)) / 2;
    const pc = addSvg('text', {
      class: 'axis group-caption',
      x: perfMid, y: M.top - 22, 'text-anchor': 'middle',
    });
    pc.textContent = 'Performance issues';
    // Eligibility caption (centred over the eligibility half)
    const eligMid = (xCenter(groupChangeIdx) + xCenter(catCount - 1)) / 2;
    const ec = addSvg('text', {
      class: 'axis group-caption',
      x: eligMid, y: M.top - 22, 'text-anchor': 'middle',
    });
    ec.textContent = 'Limited eligibility';
  }

  // Y-axis label
  const yLabel = addSvg('text', {
    class: 'axis',
    x: -(M.top + plotH / 2),
    y: 14,
    'text-anchor': 'middle',
    transform: 'rotate(-90)',
  });
  yLabel.textContent = 'Proposed blocks (MET)';

  // (No in-SVG title — the surrounding <h2> already names the chart and a
  // second title crowds the group-separator captions.)

  // Median line (fix #5): drawn before dots so it renders behind them.
  const medY = yScale(medianMet);
  addSvg('line', {
    class: 'gridline',
    x1: M.left, x2: M.left + plotW,
    y1: medY, y2: medY,
    style: 'stroke-dasharray:6,4;stroke-width:1.5;opacity:0.7;',
  });
  const medLabel = addSvg('text', {
    class: 'axis',
    x: M.left + plotW + 6,
    y: medY + 4,
    'text-anchor': 'start',
    style: 'font-size:10px;',
  });
  medLabel.textContent = 'median: ' + Math.round(medianMet);

  // Tooltip
  const tip = document.getElementById('tooltip');
  function showTip(evt, r) {
    tip.style.display = 'block';
    const limitedNote = LIMITED_ELIGIBILITY.has(r.pose_status)
        ? '<br><em>(limited eligibility — partial window)</em>'
        : '';
    tip.innerHTML =
        '<b>' + esc(r.protx) + '</b><br>' +
        'status: ' + esc(r.pose_status) + '<br>' +
        'proposed: ' + esc(r.met) + '<br>' +
        'skipped: ' + esc(r.skipped) + '<br>' +
        'member_of: ' + esc(r.member_of) + '<br>' +
        'band: ' + esc(r.band) + ' (' +
        (r.composite == null ? '—' : Number(r.composite).toFixed(4)) + ')' +
        limitedNote;
    positionTip(evt);
  }
  function positionTip(evt) {
    const rect = svg.getBoundingClientRect();
    const x = evt.clientX - rect.left + 14;
    const y = evt.clientY - rect.top + 14;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }

  const catIdx = {};
  POSE_CATEGORIES.forEach((c, i) => { catIdx[c.key] = i; });

  // Dots (drawn after median line so they appear on top)
  for (const r of rows) {
    const ci = catIdx[r.pose_status];
    if (ci == null) continue;
    const jx = jitterFor(r.protx) * 15;
    const cx = xCenter(ci) + jx;
    const cy = yScale(Number(r.met || 0));
    const color = BAND_COLORS[r.band] || '#8892a6';
    const dot = addSvg('circle', {
      class: 'dot',
      cx: cx, cy: cy, r: 5,
      fill: color,
      'fill-opacity': 0.8,
    });
    dot.addEventListener('mousemove', (e) => { showTip(e, r); });
    dot.addEventListener('mouseleave', hideTip);
    dot.addEventListener('click', () => {
      window.open(explorerUrl(r.protx), '_blank', 'noopener');
    });
  }

  // ---- Table ----

  // Sort state
  let sortCol = 'composite';
  let sortDir = -1;  // -1 = desc, 1 = asc
  let filterStr = '';

  // Column type map: numeric vs lexicographic
  const COL_NUMERIC = new Set(['composite', 'member_of', 'met', 'skipped', 'delta']);

  function sortVal(r, col) {
    if (col === 'delta') return Number(r.met || 0) - medianMet;
    const v = r[col];
    if (COL_NUMERIC.has(col)) return v == null ? -Infinity : Number(v);
    return String(v == null ? '' : v).toLowerCase();
  }

  function renderTable() {
    const needle = filterStr.toLowerCase();
    const visible = rows.filter((r) =>
      !needle || String(r.protx || '').toLowerCase().includes(needle)
    );
    visible.sort((a, b) => {
      const va = sortVal(a, sortCol);
      const vb = sortVal(b, sortCol);
      if (va < vb) return sortDir;
      if (va > vb) return -sortDir;
      return 0;
    });

    const countEl = document.getElementById('filter-count');
    if (needle) {
      countEl.textContent = `Showing ${visible.length} of ${total}`;
    } else {
      countEl.textContent = `${total} validators`;
    }

    const body = document.getElementById('tbl-body');
    body.innerHTML = '';
    visible.forEach((r, i) => {
      const delta = Math.round(Number(r.met || 0) - medianMet);
      const deltaSign = delta > 0 ? '+' : '';
      const deltaCls = delta > 0 ? 'delta-pos' : delta < 0 ? 'delta-neg' : '';
      const limited = LIMITED_ELIGIBILITY.has(r.pose_status);
      const mark = limited
        ? '<abbr class="eligibility-mark" title="Limited eligibility: validator was live for only part of the window — Δ against the full-window median is not directly comparable.">*</abbr>'
        : '';
      const deltaInner = deltaCls
        ? `<span class="${deltaCls}">${deltaSign}${delta}</span>`
        : `${deltaSign}${delta}`;
      const deltaCell = deltaInner + mark;

      const htmlLink = r.report_html
        ? `<a href="${esc(r.report_html)}" title="${esc(r.protx)}">HTML</a>`
        : '';
      const jsonLink = r.report_json
        ? `<a href="${esc(r.report_json)}">JSON</a>`
        : '';
      const explorerLink = `<a href="${explorerUrl(r.protx)}" target="_blank" rel="noopener">Explorer</a>`;
      const sep = '<span class="report-sep">·</span>';
      const reportLinks = [htmlLink, jsonLink, explorerLink].filter(Boolean).join(sep);

      const deltaTitle = limited
        ? `Δ from median (${medianMet}) — limited eligibility, see legend`
        : `Δ from median (${medianMet})`;

      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + (i + 1) + '</td>' +
        '<td><a href="' + esc(r.report_html || explorerUrl(r.protx)) + '"' +
          ' title="' + esc(r.protx) + '">' +
          '<code>' + esc(abbrev(r.protx)) + '</code></a></td>' +
        '<td><span class="band band-' + esc(r.band || 'N/A').replace(/[^A-Za-z]/g, '') + '">' +
          esc(r.band) + '</span></td>' +
        '<td>' + (r.composite == null ? '—' : Number(r.composite).toFixed(4)) + '</td>' +
        '<td>' + esc(r.member_of) + '</td>' +
        '<td>' + esc(r.met) + '</td>' +
        '<td class="' + deltaCls + '" title="' + esc(deltaTitle) + '">' + deltaCell + '</td>' +
        '<td>' + esc(r.skipped) + '</td>' +
        '<td><code>' + esc(r.pose_status) + '</code></td>' +
        '<td class="report-links">' + reportLinks + '</td>';
      body.appendChild(tr);
    });
  }

  // Filter input (fix #2)
  const filterInput = document.getElementById('validator-filter');
  filterInput.addEventListener('input', () => {
    filterStr = filterInput.value;
    renderTable();
  });

  // Sort headers (fix #3)
  document.querySelectorAll('th[data-col]').forEach((th) => {
    const col = th.dataset.col;
    th.addEventListener('click', () => {
      if (sortCol === col) {
        sortDir = -sortDir;
      } else {
        sortCol = col;
        sortDir = col === 'protx' || col === 'pose_status' || col === 'band' ? 1 : -1;
      }
      // Update glyph on all headers
      document.querySelectorAll('th[data-col]').forEach((h) => {
        const g = h.querySelector('.sort-glyph');
        if (h.dataset.col === sortCol) {
          if (g) g.textContent = sortDir === -1 ? ' ▼' : ' ▲';
          else h.insertAdjacentHTML('beforeend', `<span class="sort-glyph">${sortDir === -1 ? '▼' : '▲'}</span>`);
        } else {
          if (g) g.remove();
        }
      });
      renderTable();
    });
  });

  // Initial render
  renderTable();
  // Set initial sort glyph on composite header
  const initTh = document.querySelector('th[data-col="composite"]');
  if (initTh) initTh.insertAdjacentHTML('beforeend', '<span class="sort-glyph">▼</span>');
})();
"""


def _format_window_desc(w: dict) -> str:
    """Build a compact human-readable window description from a window dict."""

    def _compact(iso: str) -> str:
        # "2026-03-22T13:18:21Z" -> "2026-03-22 13:18 UTC"
        return iso.replace("T", " ").rstrip("Z")[:16] + " UTC"

    lo, hi = w.get("platform_range", [None, None])
    parts = [
        f"{w['days']}-day window",
        f"{_compact(w['from_time'])} → {_compact(w['to_time'])}",
    ]
    if lo is not None and hi is not None:
        parts.append(f"platform blocks {lo}–{hi}")
    return "  ·  ".join(parts)


def normalize_pose_status(s: str) -> str:
    """Map legacy 6-category pose_status values to the current 5-category scheme.

    Older summary.json files may contain ``never`` or ``revived_before_window``.
    Both represent validators that were fully eligible for the entire window and
    are merged into ``active_whole_window`` for all user-facing rendering.
    """
    if s in ("never", "revived_before_window"):
        return "active_whole_window"
    return s


def _build_dist_meta(validators: list[dict]) -> dict:
    """Pre-compute selection-distribution data for the active-whole-window cohort.

    Cohort = validators whose pose_status is ``active_whole_window`` (or the
    legacy equivalents ``never`` / ``revived_before_window``).  These were
    fully eligible for the entire analysis window, so their member_of counts
    follow the same theoretical distribution (roughly binomial -> normal
    approximation).  Pre-computing mu, sigma, z-scores and the theoretical
    curve points server-side means the JS renderer doesn't need a stats lib.
    """
    import math

    COHORT_STATUSES = {"active_whole_window", "never", "revived_before_window"}
    cohort = [v for v in validators if v.get("pose_status") in COHORT_STATUSES]
    if len(cohort) < 2:
        return {"n": len(cohort), "mu": 0.0, "sigma": 1.0, "dots": [], "curve": []}

    mo_values = [v["member_of"] for v in cohort]
    mu = mean(mo_values)
    sigma = stdev(mo_values)
    n = len(cohort)

    # Per-dot data for the strip plot.
    dots = [
        {
            "protx": v["protx"],
            "member_of": v["member_of"],
            "band": v.get("band", "N/A"),
            "pose_status": v.get("pose_status", ""),
            "report_html": v.get("report_html"),
            "z": round((v["member_of"] - mu) / sigma, 2),
        }
        for v in cohort
    ]

    # Theoretical normal curve evaluated on a fine grid over [mu-4σ, mu+4σ],
    # clamped to the actual data range with a small margin.
    x_lo = max(min(mo_values) - 1, mu - 4 * sigma)
    x_hi = min(max(mo_values) + 1, mu + 4 * sigma)
    n_grid = 120
    step = (x_hi - x_lo) / n_grid
    two_sigma_sq = 2.0 * sigma * sigma
    # Scale so area under curve == n (to overlay on histogram count axis).
    # For bin width BIN_W: scale = n * BIN_W / (sigma * sqrt(2π)).
    # We embed scale_factor = n / (sigma * sqrt(2π)) and let JS multiply by bin_w.
    norm_scale = n / (sigma * math.sqrt(2 * math.pi))
    curve = []
    x = x_lo
    for _ in range(n_grid + 1):
        pdf = math.exp(-((x - mu) ** 2) / two_sigma_sq)
        curve.append({"x": round(x, 3), "y": round(pdf, 6)})
        x += step

    return {
        "n": n,
        "mu": round(mu, 4),
        "sigma": round(sigma, 4),
        "norm_scale": round(norm_scale, 6),
        "dots": dots,
        "curve": curve,
    }


def render_index_html(
    generated_at: str,
    window_desc: str,
    validators: list[dict] | None = None,
) -> str:
    dist_meta = _build_dist_meta(validators or [])
    boot_meta = {
        "generated_at": generated_at,
        "window_desc": window_desc,
        "algorithm_version": ALGO_VERSION,
        "dist": dist_meta,
    }
    boot_json = json.dumps(boot_meta).replace("</", "<\\/")
    return (
        INDEX_HTML_TEMPLATE.replace("__CSS__", INDEX_HTML_CSS)
        .replace("__JS__", INDEX_HTML_JS)
        .replace("__ALGO__", ALGO_VERSION)
        .replace("__BOOT_META__", boot_json)
    )


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_reports(
    report: dict[str, Any], out_dir: Path, target_upper: str, json_only: bool
) -> tuple[Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_id = target_upper[:8].lower()
    json_path = out_dir / f"{short_id}_{ts}.json"
    html_path = out_dir / f"{short_id}_{ts}.html"

    # Public JSON: drop the internal _pose_state_at_tip blob.
    public_report = {k: v for k, v in report.items() if not k.startswith("_")}
    json_path.write_text(json.dumps(public_report, indent=2), encoding="utf-8")
    if json_only:
        return json_path, None

    if report.get("status") == "NOT_APPLICABLE":
        html = (
            f"<!doctype html><meta charset=utf-8>"
            f"<title>N/A</title>"
            f"<body style='font-family:sans-serif;padding:40px;'>"
            f"<h2>Not applicable</h2>"
            f"<p>{html_mod.escape(report.get('reason', ''))}</p>"
            f"<pre>{html_mod.escape(json.dumps(public_report, indent=2))}</pre>"
        )
    else:
        html = render_html(report)
    html_path.write_text(html, encoding="utf-8")
    return json_path, html_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def detect_core_cmd(preferred: str | None) -> CoreClient:
    """Auto-detect working Core CLI invocation. Returns a live CoreClient."""
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates += [DEFAULT_CORE_CMD, DEFAULT_CORE_CMD_FALLBACK]
    tried = []
    for c in candidates:
        client = CoreClient(c)
        if client.ok():
            return client
        tried.append(c)
    raise SystemExit(
        f"Could not find a working dash-cli. Tried: {tried}\n"
        f"Pass --core-cmd '<your invocation>'."
    )


def run_single(args: argparse.Namespace, out_dir: Path) -> int:
    target_lower = args.protx.lower()

    td = TenderdashClient(args.tenderdash_url)
    core = detect_core_cmd(args.core_cmd)
    vlog(f"using core: {core.cmd}")

    cache = build_window_cache(td, core, args.days)

    # Peer stats: use the Evo pool pass (matches v0.1.0 behaviour)
    peer_stats = compute_peer_stats_from_pool(
        core,
        cache.core_h_tip,
        cache.core_lo,
        cache.aggregated_quorums,
        skip=args.skip_peer_scan,
    )

    report = score_validator_from_cache(
        cache,
        target_lower,
        peer_stats,
        has_peer_scan=not args.skip_peer_scan,
    )

    json_path, html_path = write_reports(
        report, out_dir, report["protx"], args.json_only
    )
    print(str(json_path.resolve()))
    if html_path:
        print(str(html_path.resolve()))
    return 0


def _existing_reports_for(
    protx_lower: str, out_dir: Path
) -> tuple[Path | None, Path | None]:
    """Return (json, html) paths for the newest pre-existing report for this protx, if any."""
    prefix = protx_lower[:8] + "_"
    matches = sorted(out_dir.glob(f"{prefix}*.json"))
    if not matches:
        return None, None
    j = matches[-1]
    h = j.with_suffix(".html")
    return j, (h if h.exists() else None)


def _bisect_deregistration_height(
    core: CoreClient,
    protx: str,
    lo: int,
    hi: int,
    block_hash_cache: dict[int, str],
) -> int | None:
    """
    Find the earliest core height in (lo, hi] where `protx info` returns
    "not found" (i.e. the MN ceased to exist). Returns None if no such
    height is found within the range (shouldn't happen if caller already
    established lo=exists, hi=not-found).

    Uses `block_hash_cache` (shared with the PoSe bisection) so the extra
    RPC cost stays bounded — ~log2(range) protx_info calls per dereg'd MN.
    """

    def bh(h: int) -> str:
        v = block_hash_cache.get(h)
        if v is None:
            v = core.get_block_hash(h)
            block_hash_cache[h] = v
        return v

    def exists_at(h: int) -> bool:
        try:
            core.protx_info(protx, bh(h))
            return True
        except RuntimeError as e:
            if "not found" in str(e).lower():
                return False
            raise

    # Invariant: exists_at(lo)==True, exists_at(hi)==False. Binary search for
    # smallest h in (lo, hi] with exists_at(h)==False.
    answer = hi
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if exists_at(mid):
            lo = mid
        else:
            answer = mid
            hi = mid
    return answer


def cleanup_stale_reports(out_dir: Path) -> None:
    """Delete per-validator report files from previous batch runs.

    Only files matching REPORT_FILE_RE (<8-hex>_<YYYYMMDDThhmmssZ>.{json,html})
    are removed. index.html, summary.json, subdirectories, and anything else
    the user may have placed in out_dir are left untouched.
    """
    if not out_dir.is_dir():
        return  # first run — nothing to clean

    targets = [
        f for f in out_dir.iterdir() if f.is_file() and REPORT_FILE_RE.match(f.name)
    ]
    if not targets:
        return

    print(
        f"[batch] cleanup: removing {len(targets)} stale per-validator reports from {out_dir}/",
        file=sys.stderr,
        flush=True,
    )
    for path in targets:
        try:
            path.unlink()
        except OSError as exc:
            print(
                f"[batch] cleanup: warning — could not remove {path.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )


def run_batch(args: argparse.Namespace, out_dir: Path) -> int:
    td = TenderdashClient(args.tenderdash_url)
    core = detect_core_cmd(args.core_cmd)
    vlog(f"using core: {core.cmd}")

    t_batch_start = time.monotonic()

    if not args.keep_history:
        cleanup_stale_reports(out_dir)

    # Build the shared cache first — we need core_lo to enumerate the "who was
    # a member of the Evo pool at any point in the window" union.
    cache = build_window_cache(td, core, args.days)

    # Enumerate Evo MNs at BOTH window boundaries. A MN present at tip but not
    # at core_lo was registered mid-window; one present at core_lo but not at
    # tip was deregistered mid-window. Either way the union gives full
    # coverage of validators who mattered during the window.
    evo_tip_raw = core.protx_list_evo(detailed=True)
    if not isinstance(evo_tip_raw, list):
        raise SystemExit(
            f"Unexpected protx list evo (tip) output type: {type(evo_tip_raw)}"
        )
    evo_tip = {
        x["proTxHash"].lower(): x
        for x in evo_tip_raw
        if isinstance(x, dict) and x.get("type") == "Evo"
    }

    evo_lo_raw = core.run_json("protx", "list", "evo", "true", str(cache.core_lo))
    if not isinstance(evo_lo_raw, list):
        raise SystemExit(
            f"Unexpected protx list evo (lo) output type: {type(evo_lo_raw)}"
        )
    evo_lo = {
        x["proTxHash"].lower(): x
        for x in evo_lo_raw
        if isinstance(x, dict) and x.get("type") == "Evo"
    }

    # Dereg candidates — in lo but not tip. Bisect each to pin the precise
    # deregistration height so scoring can cap eligible_fraction at the
    # correct boundary.
    dereg_only = [p for p in evo_lo if p not in evo_tip]
    if dereg_only:
        print(
            f"[batch] {len(dereg_only)} Evo MN(s) present at core_lo "
            f"({cache.core_lo}) but deregistered by tip — bisecting dereg heights",
            file=sys.stderr,
            flush=True,
        )
    for protx in dereg_only:
        dereg_h = _bisect_deregistration_height(
            core, protx, cache.core_lo, cache.core_hi, cache.block_hash_cache
        )
        entry = dict(evo_lo[protx])  # snapshot of state at core_lo
        entry["_dereg_core_h"] = dereg_h
        evo_tip[protx] = entry

    evo_nodes = list(evo_tip.values())

    print(
        f"[batch] enumerated tip={len(evo_tip_raw)} + core_lo-only={len(dereg_only)} "
        f"-> {len(evo_nodes)} Evo masternodes (union)",
        file=sys.stderr,
        flush=True,
    )

    cache.evo_registry = evo_tip
    t_cache_built = time.monotonic()
    print(
        f"[batch] window cache built in {t_cache_built - t_batch_start:.1f}s "
        f"(blocks={len(cache.blocks)}, quorums={len(cache.aggregated_quorums)}, "
        f"td.calls={td.calls}, core.calls={core.calls})",
        file=sys.stderr,
        flush=True,
    )

    # Placeholder peer stats for the first pass (implicit --skip-peer-scan).
    first_pass_peer = PeerStats(
        pool_size=len(evo_nodes), member_of_median=0.0, proposed_rate_median=1.0
    )

    # Sort by protx hash so the progress log has a deterministic order.
    evo_protxs = sorted(cache.evo_registry.keys())

    # First pass: compute per-validator reports with placeholder peer stats
    reports: list[dict[str, Any]] = []
    for idx, protx in enumerate(evo_protxs, start=1):
        # Resume: skip if an existing report matches the current window.
        if args.resume:
            jpath, hpath = _existing_reports_for(protx, out_dir)
            if jpath is not None:
                try:
                    existing = json.loads(jpath.read_text())
                    w = existing.get("window") or {}
                    if (
                        w.get("platform_range") == [cache.h_start, cache.h_tip]
                        and w.get("days") == args.days
                    ):
                        # Re-use existing report — but we need pose_state_at_tip
                        # for summary classification; derive it from tip info.
                        tip_info = cache.evo_registry[protx]
                        state = tip_info.get("state") or {}
                        existing["_pose_state_at_tip"] = {
                            "PoSeBanHeight": state.get("PoSeBanHeight"),
                            "PoSeRevivedHeight": state.get("PoSeRevivedHeight"),
                        }
                        # Propagate the deregistration height (may be absent
                        # in reports generated before this field existed).
                        dereg_h = tip_info.get("_dereg_core_h")
                        if dereg_h is not None:
                            existing.setdefault("eligibility", {})[
                                "deregistered_core_height"
                            ] = dereg_h
                        existing["_report_filenames"] = {
                            "json": jpath.name,
                            "html": hpath.name if hpath else None,
                        }
                        reports.append(existing)
                        print(
                            f"[batch] {idx}/{len(evo_protxs)} resumed "
                            f"— {protx[:8].upper()}... (from {jpath.name})",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                except Exception as e:
                    vlog(f"resume: failed to reuse {jpath}: {e}")

        tip_info = cache.evo_registry[protx]
        rep = score_validator_from_cache(
            cache,
            protx,
            first_pass_peer,
            has_peer_scan=False,  # first pass — absolute scoring
            protx_info=tip_info,
        )
        reports.append(rep)

        band = rep.get("scores", {}).get("band", "N/A")
        comp = rep.get("scores", {}).get("composite")
        comp_s = "None" if comp is None else f"{comp:.4f}"
        print(
            f"[batch] {idx}/{len(evo_protxs)} processed — "
            f"current: {protx[:8].upper()}...{protx[-4:].upper()} "
            f"({band}, composite={comp_s})",
            file=sys.stderr,
            flush=True,
        )

    t_first_pass = time.monotonic()
    print(
        f"[batch] first-pass done in {t_first_pass - t_cache_built:.1f}s",
        file=sys.stderr,
        flush=True,
    )

    # Recompute peer stats from aggregated results, then re-score.
    # Classify pose_status eagerly so the median filter can exclude
    # partial-eligibility validators — see compute_peer_stats_from_batch_results.
    batch_rows_stub = [
        {
            "member_of": r["quorum_stats"]["member_of"],
            "met": r["quorum_stats"]["met"],
            "skipped": r["quorum_stats"]["skipped"],
            "pose_status": classify_pose_status(r, core_lo=cache.core_lo),
        }
        for r in reports
        if r.get("status") != "NOT_APPLICABLE"
    ]
    batch_peer_stats = compute_peer_stats_from_batch_results(batch_rows_stub)
    baseline_n = sum(
        1 for r in batch_rows_stub if r["pose_status"] in ALWAYS_ELIGIBLE_POSE_STATUSES
    )
    print(
        f"[batch] peer member_of_median (filtered to always-eligible, "
        f"n={baseline_n}/{batch_peer_stats.pool_size}): "
        f"{batch_peer_stats.member_of_median:.2f}",
        file=sys.stderr,
        flush=True,
    )
    vlog(
        f"batch peer medians: pool_size={batch_peer_stats.pool_size} "
        f"member_of_median={batch_peer_stats.member_of_median:.2f} "
        f"proposed_rate_median={batch_peer_stats.proposed_rate_median:.4f}"
    )

    # Second pass: recompute ONLY scores using batch-derived peer medians.
    # (Quorum classification and eligibility don't change, so we patch in place.)
    for rep in reports:
        if rep.get("status") == "NOT_APPLICABLE":
            continue
        qs = rep["quorum_stats"]
        el = rep["eligibility"]
        new_scores = compute_scores(
            member_of=qs["member_of"],
            met=qs["met"],
            skipped=qs["skipped"],
            inconclusive=qs["inconclusive"],
            round_misses=qs["round_misses_on_target"],
            peer_stats=batch_peer_stats,
            eligible_fraction=el["eligible_fraction"],
            has_peer_scan=True,
        )
        rep["scores"] = new_scores
        qs["peer_pool_size"] = batch_peer_stats.pool_size
        qs["peer_member_of_median"] = batch_peer_stats.member_of_median
        qs["peer_proposed_rate_median"] = batch_peer_stats.proposed_rate_median

    # Write per-validator reports + build summary rows.
    summary_rows: list[dict[str, Any]] = []
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    for rep in reports:
        protx_upper = rep["protx"]
        short_id = protx_upper[:8].lower()

        if "_report_filenames" in rep:
            # Resumed: filenames already exist, but we re-render HTML because
            # scores were recomputed. Preserve original JSON filenames so
            # URLs stay stable.
            json_name = rep["_report_filenames"]["json"]
            html_name = rep["_report_filenames"]["html"] or f"{short_id}_{ts}.html"
            json_path = out_dir / json_name
            html_path = out_dir / html_name
        else:
            json_path = out_dir / f"{short_id}_{ts}.json"
            html_path = out_dir / f"{short_id}_{ts}.html"

        public_report = {k: v for k, v in rep.items() if not k.startswith("_")}
        json_path.write_text(json.dumps(public_report, indent=2), encoding="utf-8")
        if not args.json_only:
            if rep.get("status") == "NOT_APPLICABLE":
                html = (
                    f"<!doctype html><meta charset=utf-8>"
                    f"<title>N/A</title>"
                    f"<body style='font-family:sans-serif;padding:40px;'>"
                    f"<h2>Not applicable</h2>"
                    f"<p>{html_mod.escape(rep.get('reason', ''))}</p>"
                    f"<pre>{html_mod.escape(json.dumps(public_report, indent=2))}</pre>"
                )
            else:
                html = render_html(rep)
            html_path.write_text(html, encoding="utf-8")

        if rep.get("status") == "NOT_APPLICABLE":
            # Not really expected in --all-platform, but be safe.
            summary_rows.append(
                {
                    "protx": protx_upper,
                    "pose_status": "active_whole_window",
                    "member_of": 0,
                    "met": 0,
                    "skipped": 0,
                    "inconclusive": 0,
                    "composite": None,
                    "band": "N/A",
                    "report_html": html_path.name if not args.json_only else None,
                    "report_json": json_path.name,
                }
            )
            continue

        qs = rep["quorum_stats"]
        scores = rep["scores"]
        summary_rows.append(
            {
                "protx": protx_upper,
                "pose_status": classify_pose_status(rep, core_lo=cache.core_lo),
                "member_of": qs["member_of"],
                "met": qs["met"],
                "skipped": qs["skipped"],
                "inconclusive": qs["inconclusive"],
                "composite": scores["composite"],
                "band": scores["band"],
                "report_html": html_path.name if not args.json_only else None,
                "report_json": json_path.name,
            }
        )

    # Write summary.json + index.html
    summary_path = out_dir / "summary.json"
    summary_window = {
        "days": cache.days,
        "from_time": iso_utc(cache.t_start),
        "to_time": iso_utc(cache.t_tip),
        "platform_range": [cache.h_start, cache.h_tip],
    }
    summary_doc = {"window": summary_window, "validators": summary_rows}
    summary_path.write_text(json.dumps(summary_doc, indent=2), encoding="utf-8")

    if not args.json_only:
        window_desc = _format_window_desc(summary_window)
        index_html = render_index_html(
            generated_at=iso_utc(datetime.now(timezone.utc)),
            window_desc=window_desc,
            validators=summary_rows,
        )
        (out_dir / "index.html").write_text(index_html, encoding="utf-8")

    t_end = time.monotonic()
    print(
        f"[batch] total {t_end - t_batch_start:.1f}s "
        f"(cache {t_cache_built - t_batch_start:.1f}s, "
        f"first-pass {t_first_pass - t_cache_built:.1f}s, "
        f"write/rescore {t_end - t_first_pass:.1f}s)",
        file=sys.stderr,
        flush=True,
    )
    print(str(summary_path.resolve()))
    if not args.json_only:
        print(str((out_dir / "index.html").resolve()))
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dash Platform validator fairness score over a time window."
    )
    p.add_argument(
        "protx",
        nargs="?",
        default=None,
        help="Target validator pro_tx_hash (hex, 64 chars). Omit with --all-platform.",
    )
    p.add_argument(
        "--all-platform",
        action="store_true",
        help="Score every Evo (HPMN) masternode; write summary.json + index.html.",
    )
    p.add_argument(
        "--days", type=int, default=30, help="Window length in days (default 30)"
    )
    p.add_argument(
        "--tenderdash-url",
        default="http://127.0.0.1:26657",
        help="Tenderdash RPC base URL (default http://127.0.0.1:26657)",
    )
    p.add_argument(
        "--core-cmd",
        default=None,
        help="Shell-quoted dash-cli invocation. Default: auto-detect.",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory (default ./reports relative to script)",
    )
    p.add_argument(
        "--skip-peer-scan",
        action="store_true",
        help="Skip peer-median pass in single-target mode "
        "(ignored in --all-platform mode, which always derives medians "
        "from the batch).",
    )
    p.add_argument("--json-only", action="store_true", help="Skip HTML render")
    p.add_argument("--verbose", action="store_true", help="Log RPC calls to stderr")
    p.add_argument(
        "--resume",
        action="store_true",
        help="In --all-platform mode, skip validators with an existing "
        "up-to-date report (matching window).",
    )
    p.add_argument(
        "--keep-history",
        action="store_true",
        help="In --all-platform mode, preserve per-validator reports from "
        "prior runs instead of wiping them at batch start. Useful for "
        "comparing results across runs.",
    )
    p.add_argument(
        "--from-summary",
        metavar="PATH",
        default=None,
        help="Re-render index.html from an existing summary.json without "
        "re-fetching any chain data. PATH is the summary.json file; "
        "index.html is written to the same directory.",
    )
    return p.parse_args()


def run_from_summary(summary_path: Path) -> int:
    """Re-render index.html from an existing summary.json (no chain I/O)."""
    if not summary_path.exists():
        print(f"Error: {summary_path} not found.", file=sys.stderr)
        return 1
    out_dir = summary_path.parent
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "window" in data:
        window_desc = _format_window_desc(data["window"])
        validators = data.get("validators", [])
    else:
        # Legacy format: plain list — no window metadata available.
        window_desc = f"(re-rendered from {summary_path.name})"
        validators = data if isinstance(data, list) else []
    # Normalize legacy 6-category pose_status to current 5-category scheme.
    validators = [
        {**v, "pose_status": normalize_pose_status(v.get("pose_status", ""))}
        for v in validators
    ]
    index_html = render_index_html(
        generated_at=iso_utc(datetime.now(timezone.utc)),
        window_desc=window_desc,
        validators=validators,
    )
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    print(str(index_path.resolve()))
    return 0


def main() -> int:
    global VERBOSE
    args = parse_args()
    VERBOSE = args.verbose

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).parent / "reports"

    if args.from_summary:
        return run_from_summary(Path(args.from_summary))

    if args.all_platform and args.protx:
        print(
            "Error: pass either <protx> or --all-platform, not both.",
            file=sys.stderr,
        )
        return 2
    if not args.all_platform and not args.protx:
        print(
            "Error: missing protx argument. Use --all-platform for batch mode.",
            file=sys.stderr,
        )
        return 2

    if args.protx:
        protx = args.protx.strip().lower()
        if len(protx) != 64 or any(c not in "0123456789abcdef" for c in protx):
            print(
                f"Error: protx must be 64 hex chars, got {args.protx!r}",
                file=sys.stderr,
            )
            return 2
        args.protx = protx

    if args.all_platform:
        return run_batch(args, out_dir)
    return run_single(args, out_dir)


if __name__ == "__main__":
    sys.exit(main())
