#!/usr/bin/env bash
#
# run.sh — cron-friendly wrapper: regenerate fairness batch, then deploy.
#
# Usage: ./run.sh [--dry-run] [--skip-batch] [--skip-deploy] [-h|--help]
# See --help for env-var config and cron entry examples.

set -euo pipefail
IFS=$'\n\t'

# ---------- Resolve script directory (must be first — cron has wrong CWD) ----------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd -- "${SCRIPT_DIR}"

# ---------- Cron-safe environment ----------
HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
export HOME
PATH="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin:/snap/bin${PATH:+:${PATH}}"
export PATH

# ---------- Configurable defaults ----------
DAYS="${DAYS:-30}"
PYTHON="${PYTHON:-python3}"
LOG_FILE="${LOG_FILE:-${SCRIPT_DIR}/logs/run.log}"
# Lock file: prefer /var/lock (system-wide), fall back to script dir.
if [[ -w /var/lock ]] || mkdir -p /var/lock 2>/dev/null; then
    LOCK_FILE="${LOCK_FILE:-/var/lock/platform-fairness.lock}"
else
    LOCK_FILE="${LOCK_FILE:-${SCRIPT_DIR}/.run.lock}"
fi

# ---------- Load .env (shell env wins over file values) ----------
for _env_f in "${SCRIPT_DIR}/.env" "./.env"; do
    if [[ -r "${_env_f}" ]]; then
        # Snapshot vars we want shell env to win on.
        _saved_days="${DAYS}"
        _saved_python="${PYTHON}"
        _saved_log="${LOG_FILE}"
        _saved_lock="${LOCK_FILE}"
        set -a
        # shellcheck disable=SC1090
        source "${_env_f}"
        set +a
        # Restore any values that were explicitly set before sourcing.
        [[ -n "${_saved_days}" ]]   && DAYS="${_saved_days}"
        [[ -n "${_saved_python}" ]] && PYTHON="${_saved_python}"
        [[ -n "${_saved_log}" ]]    && LOG_FILE="${_saved_log}"
        [[ -n "${_saved_lock}" ]]   && LOCK_FILE="${_saved_lock}"
        break
    fi
done
unset _env_f _saved_days _saved_python _saved_log _saved_lock

# ---------- Cleanup trap ----------
# Released automatically when the fd is closed on exit.
cleanup() {
    local rc=$?
    exit "${rc}"
}
trap cleanup EXIT INT TERM

# ---------- Flags ----------
DRY_RUN=0
SKIP_BATCH=0
SKIP_DEPLOY=0
DEPLOY_EXTRA_ARGS=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] [--dry-run] [--skip-batch] [--skip-deploy]

Regenerate the platform fairness batch, then deploy to Cloudflare Pages.

Options:
  -h, --help       Show this help and exit.
  --dry-run        Pass --dry-run to deploy.sh; batch still runs for real.
  --skip-batch     Skip Phase 1 (fairness.py). Deploy existing reports only.
  --skip-deploy    Skip Phase 2 (deploy.sh). Useful for manual inspection.

Environment variables (override via .env or shell export):
  DAYS             Analysis window in days.          Default: 30
  PYTHON           Python interpreter path.          Default: python3
  LOG_FILE         Append-mode log file path.        Default: ./logs/run.log
  LOCK_FILE        Exclusive-lock file path.         Default: /var/lock/platform-fairness.lock
                   (falls back to .run.lock in script dir if /var/lock unwritable)

Cron example (daily at 03:00 UTC):
  0 3 * * * /home/ubuntu/platform-fairness/run.sh >/dev/null 2>&1

Every 6 hours:
  0 */6 * * * /home/ubuntu/platform-fairness/run.sh >/dev/null 2>&1

Exit codes:
  0  Success, OR another run already in progress (cron-friendly — no spam).
  1  Batch (fairness.py) failed.
  2  Deploy (deploy.sh) failed.
  3  Bad usage / unknown argument.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --dry-run)
            DRY_RUN=1
            DEPLOY_EXTRA_ARGS+=(--dry-run)
            shift
            ;;
        --skip-batch)
            SKIP_BATCH=1
            shift
            ;;
        --skip-deploy)
            SKIP_DEPLOY=1
            shift
            ;;
        *)
            printf 'ERROR: Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 3
            ;;
    esac
done

# ---------- Logging helpers ----------
mkdir -p "$(dirname "${LOG_FILE}")"

ts()  { date -u +%FT%TZ; }
log() { printf '%s %s\n' "$(ts)" "$*" | tee -a "${LOG_FILE}"; }

# ---------- Concurrency guard ----------
# Open fd 9 on the lock file; flock -n returns non-zero immediately if locked.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    printf '%s previous run still in progress, skipping\n' "$(ts)" >> "${LOG_FILE}"
    exit 0
fi

# ---------- Banner ----------
log "==== run.sh starting ===="
[[ "${DRY_RUN}"    -eq 1 ]] && log "DRY RUN (deploy only — batch runs for real)"
[[ "${SKIP_BATCH}" -eq 1 ]] && log "SKIP_BATCH set — skipping Phase 1"
[[ "${SKIP_DEPLOY}" -eq 1 ]] && log "SKIP_DEPLOY set — skipping Phase 2"

START_TS=$(date +%s)

# ---------- Phase 1: batch regeneration ----------
if [[ "${SKIP_BATCH}" -eq 0 ]]; then
    log "[batch] starting batch regeneration (DAYS=${DAYS})"
    set +e
    "${PYTHON}" fairness.py --all-platform --days "${DAYS}" --verbose 2>&1 | tee -a "${LOG_FILE}"
    BATCH_RC=${PIPESTATUS[0]}
    set -e
    if [[ "${BATCH_RC}" -ne 0 ]]; then
        log "[batch] FAILED (exit ${BATCH_RC}) — aborting, deploy skipped"
        exit 1
    fi
    log "[batch] completed successfully"
else
    log "[batch] skipped"
fi

# ---------- Phase 2: deploy ----------
if [[ "${SKIP_DEPLOY}" -eq 0 ]]; then
    log "[deploy] starting deploy"
    set +e
    "${SCRIPT_DIR}/deploy.sh" "${DEPLOY_EXTRA_ARGS[@]}" 2>&1 | tee -a "${LOG_FILE}"
    DEPLOY_RC=${PIPESTATUS[0]}
    set -e
    if [[ "${DEPLOY_RC}" -ne 0 ]]; then
        log "[deploy] FAILED (exit ${DEPLOY_RC})"
        exit 2
    fi
    log "[deploy] completed successfully"
else
    log "[deploy] skipped"
fi

# ---------- Done ----------
END_TS=$(date +%s)
ELAPSED=$(( END_TS - START_TS ))
log "==== done in ${ELAPSED}s ===="

# Update last-run symlink (best-effort — non-fatal if log dir is read-only).
ln -sf "$(basename "${LOG_FILE}")" "$(dirname "${LOG_FILE}")/last-run.log" 2>/dev/null || true
