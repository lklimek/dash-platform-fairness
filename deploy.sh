#!/usr/bin/env bash
#
# Deploy the fairness reports directory to Cloudflare Pages via wrangler.
#
# One-shot script. No daemons, no interactive prompts.
# See `./deploy.sh --help` for usage.

set -euo pipefail

# ---------- Cleanup trap ----------
# Temp files live under $TMP_ROOT; removed on any exit (success or failure).
TMP_ROOT=""
cleanup() {
    local rc=$?
    if [[ -n "${TMP_ROOT}" && -d "${TMP_ROOT}" ]]; then
        rm -rf -- "${TMP_ROOT}"
    fi
    exit "${rc}"
}
trap cleanup EXIT INT TERM

# ---------- Paths / defaults ----------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

REPORTS_DIR="${REPORTS_DIR:-${SCRIPT_DIR}/reports}"
CF_PROJECT_NAME="${CF_PROJECT_NAME:-dash-fairness-reports}"
CF_BRANCH="${CF_BRANCH:-main}"

# Cloudflare Pages Free-plan limits (see https://developers.cloudflare.com/pages/platform/limits/).
MAX_FILES_WARN=20000
MAX_FILE_BYTES=$((25 * 1024 * 1024))  # 25 MiB

# ---------- Color helpers (TTY only) ----------
if [[ -t 1 ]] && command -v tput &>/dev/null && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    C_GREEN="$(tput setaf 2)"
    C_YELLOW="$(tput setaf 3)"
    C_RED="$(tput setaf 1)"
    C_BOLD="$(tput bold)"
    C_RESET="$(tput sgr0)"
else
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
    C_BOLD=""
    C_RESET=""
fi

log()  { printf '%s\n' "$*"; }
warn() { printf '%sWARN:%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
err()  { printf '%sERROR:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }

# ---------- Usage ----------
usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] [-n|--dry-run]

Deploys a static-site directory to Cloudflare Pages via wrangler.

Environment variables:
  CLOUDFLARE_API_TOKEN  (required) Token with 'Cloudflare Pages — Edit' scope.
                        Create one at:
                        https://dash.cloudflare.com/profile/api-tokens
  REPORTS_DIR           Directory to deploy.
                        Default: <script-dir>/reports
                        Current: ${REPORTS_DIR}
  CF_PROJECT_NAME       Cloudflare Pages project name.
                        Default: dash-fairness-reports
                        Current: ${CF_PROJECT_NAME}
  CF_BRANCH             Branch name for the deployment.
                        Default: main
                        Current: ${CF_BRANCH}

Options:
  -h, --help            Show this help and exit.
  -n, --dry-run         Print what would run without deploying.

  Alternative: put KEY=VALUE pairs in a \`.env\` file next to the script or
  in the current directory instead of exporting in the shell.

Examples:
  export CLOUDFLARE_API_TOKEN=cf_pat_...
  ./deploy.sh

  CF_PROJECT_NAME=fairness-staging CF_BRANCH=preview ./deploy.sh

  # Or use a .env file:
  echo 'CLOUDFLARE_API_TOKEN=cf_pat_...' > .env
  ./deploy.sh
EOF
}

# ---------- Arg parsing ----------
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -n|--dry-run)
            DRY_RUN=1
            shift
            ;;
        *)
            err "Unknown argument: $1"
            usage >&2
            exit 2
            ;;
    esac
done

# ---------- Load .env (shell env wins over file values) ----------
for _env_f in "${SCRIPT_DIR}/.env" "./.env"; do
    if [[ -r "${_env_f}" ]]; then
        _saved_token="${CLOUDFLARE_API_TOKEN:-}"
        set -a
        # shellcheck disable=SC1090
        source "${_env_f}"
        set +a
        [[ -n "${_saved_token}" ]] && CLOUDFLARE_API_TOKEN="${_saved_token}"
        log "Loaded env from: ${_env_f}"
        break
    fi
done
unset _env_f _saved_token

# ---------- Precondition: API token ----------
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]] && [[ "${DRY_RUN}" -eq 0 ]]; then
    err "CLOUDFLARE_API_TOKEN is not set."
    cat >&2 <<EOF

Create a token with the 'Cloudflare Pages — Edit' scope at:
  https://dash.cloudflare.com/profile/api-tokens

Then export it:
  export CLOUDFLARE_API_TOKEN=cf_pat_...

EOF
    exit 2
fi

# ---------- Precondition: node/npm ----------
if ! command -v node &>/dev/null; then
    err "node is not on PATH. Install Node.js (>=18) first."
    exit 2
fi
if ! command -v npm &>/dev/null; then
    err "npm is not on PATH. Install npm alongside Node.js."
    exit 2
fi

# ---------- Precondition: wrangler (install or fall back to npx) ----------
# Default: invoke via an array so we can swap between `wrangler` and `npx wrangler@latest`.
WRANGLER_CMD=()
if command -v wrangler &>/dev/null; then
    WRANGLER_CMD=(wrangler)
elif (( DRY_RUN == 1 )); then
    # In dry-run mode we don't install anything — show what the user would get.
    WRANGLER_CMD=(wrangler)
    log "wrangler not found on PATH (dry-run: skipping install; would 'npm install -g wrangler' or fall back to 'npx wrangler@latest')."
else
    log "wrangler not found on PATH; attempting 'npm install -g wrangler'..."
    # If `npm prefix -g` is writable, no sudo needed (typical with nvm/asdf).
    npm_prefix="$(npm prefix -g 2>/dev/null || echo /usr/local)"
    if [[ ! -w "${npm_prefix}" ]]; then
        warn "Global npm prefix '${npm_prefix}' is not writable — sudo may be required."
    fi
    if npm install -g wrangler >/dev/null 2>&1 && command -v wrangler &>/dev/null; then
        WRANGLER_CMD=(wrangler)
        log "Installed wrangler globally."
    else
        warn "Global wrangler install failed; falling back to 'npx wrangler@latest ...'"
        WRANGLER_CMD=(npx --yes wrangler@latest)
    fi
fi

# ---------- Precondition: REPORTS_DIR ----------
if [[ ! -d "${REPORTS_DIR}" ]]; then
    err "REPORTS_DIR does not exist or is not a directory: ${REPORTS_DIR}"
    exit 2
fi
if [[ ! -f "${REPORTS_DIR}/index.html" ]]; then
    err "REPORTS_DIR is missing index.html: ${REPORTS_DIR}/index.html"
    exit 2
fi

# ---------- File count / size sanity ----------
# Count regular files (not dirs/symlinks-to-dirs).
file_count="$(find "${REPORTS_DIR}" -type f | wc -l | tr -d ' ')"

# Total bytes (portable-ish: prefer GNU du --bytes; fall back to BSD du -k * 1024).
if du --version &>/dev/null; then
    total_bytes="$(du -sb "${REPORTS_DIR}" | awk '{print $1}')"
else
    total_bytes=$(( $(du -sk "${REPORTS_DIR}" | awk '{print $1}') * 1024 ))
fi

# Largest file (bytes + path).
largest_line="$(find "${REPORTS_DIR}" -type f -printf '%s %p\n' 2>/dev/null | sort -n | tail -1 || true)"
if [[ -z "${largest_line}" ]]; then
    # BSD fallback: no -printf support.
    largest_line="$(find "${REPORTS_DIR}" -type f -exec stat -f '%z %N' {} \; 2>/dev/null | sort -n | tail -1 || true)"
fi
largest_bytes=0
largest_path=""
if [[ -n "${largest_line}" ]]; then
    largest_bytes="${largest_line%% *}"
    largest_path="${largest_line#* }"
fi

# Pretty-print total size in MB (1 decimal).
total_mb="$(awk -v b="${total_bytes}" 'BEGIN{printf "%.1f", b/1048576}')"

log "Uploading ${file_count} files (${total_mb} MB) to '${CF_PROJECT_NAME}'..."

if (( file_count > MAX_FILES_WARN )); then
    warn "File count (${file_count}) exceeds Cloudflare Pages Free-plan limit of ${MAX_FILES_WARN}."
fi
if (( largest_bytes > MAX_FILE_BYTES )); then
    warn "Largest file exceeds 25 MiB Pages limit: ${largest_path} (${largest_bytes} bytes)."
fi

# ---------- Build command ----------
deploy_cmd=(
    "${WRANGLER_CMD[@]}"
    pages deploy "${REPORTS_DIR}"
    "--project-name=${CF_PROJECT_NAME}"
    "--branch=${CF_BRANCH}"
    "--commit-dirty=true"
)

# Readable representation of the command (safe to print).
cmd_display=""
for part in "${deploy_cmd[@]}"; do
    cmd_display+="$(printf '%q ' "${part}")"
done

if (( DRY_RUN == 1 )); then
    log "${C_BOLD}DRY RUN${C_RESET} — would execute:"
    log "  ${cmd_display}"
    exit 0
fi

# ---------- Execute ----------
TMP_ROOT="$(mktemp -d -t cf-pages-deploy-XXXXXX)"
out_file="${TMP_ROOT}/wrangler.out"

# Run wrangler, tee output to tmp file AND stdout.
# Use PIPESTATUS to capture wrangler's exit code (tee will succeed regardless).
set +e
"${deploy_cmd[@]}" 2>&1 | tee "${out_file}"
rc=${PIPESTATUS[0]}
set -e

if (( rc != 0 )); then
    err "wrangler pages deploy failed with exit code ${rc}."
    exit "${rc}"
fi

# ---------- Extract deployed URL ----------
# Pattern matches `https://<slug>.pages.dev` (CF-generated preview/prod URL).
deployed_url="$(grep -Eo 'https://[a-z0-9-]+\.pages\.dev' "${out_file}" | tail -1 || true)"

if [[ -z "${deployed_url}" ]]; then
    err "Could not extract a *.pages.dev URL from wrangler output."
    exit 1
fi

printf '%sDEPLOYED:%s %s\n' "${C_BOLD}${C_GREEN}" "${C_RESET}" "${deployed_url}"
