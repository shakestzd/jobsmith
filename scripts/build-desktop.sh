#!/usr/bin/env bash
#
# build-desktop.sh — one-command pipeline that produces a self-contained
# Jobsmith macOS .dmg (feat-06c6055b, slice 5).
#
# Pipeline (in order):
#   1. Build the web UI            : npm --prefix web {ci|install} && run build
#   2. Stage UI for the sidecar    : web/dist -> src/jobsmith/web_dist
#   3. Build the Python sidecar     : scripts/build-sidecar.sh (PyInstaller onefile)
#   4. Bundle the Tauri .dmg        : npm run tauri build -- --bundles dmg
#
# The produced .dmg embeds the web UI + a frozen Python backend, so the app
# needs NO host Python or Node at runtime (Constraint 1).
#
# Signing (done_when #3):
#   * AD-HOC by default. tauri.conf.json sets bundle.macOS.signingIdentity "-".
#     The local .dmg runs after a one-time Gatekeeper approval — no Apple
#     Developer certificate required.
#   * To sign + NOTARIZE, export ALL FOUR of:
#       APPLE_SIGNING_IDENTITY  (Developer ID Application: Name (TEAMID))
#       APPLE_ID                (Apple account email)
#       APPLE_PASSWORD          (app-specific password from appleid.apple.com,
#                                NOT your account password)
#       APPLE_TEAM_ID           (10-char Team ID)
#     When all four are present they are passed to `tauri build` (identity via
#     an inline --config override; APPLE_ID/PASSWORD/TEAM_ID via the env that
#     Tauri reads for notarization).
#   * PARTIAL config (some but not all four) -> a clear WARNING is emitted and
#     the build falls back to AD-HOC signing. Never a silent change.
#
# Usage:
#   scripts/build-desktop.sh [extra tauri build args...]
#
# Extra args are appended to `tauri build --bundles dmg` (e.g. --target ...).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Preflight — fail fast with context if the toolchain is incomplete.
# ---------------------------------------------------------------------------
log "Preflight: checking toolchain"
command -v npm   >/dev/null 2>&1 || die "npm not found on PATH (Node toolchain required to build the web UI + run the Tauri CLI)."
command -v rustc >/dev/null 2>&1 || die "rustc not found on PATH (required by build-sidecar.sh and the Tauri release compile)."
command -v cargo >/dev/null 2>&1 || die "cargo not found on PATH (required for the Tauri release compile)."
command -v pyinstaller >/dev/null 2>&1 || die "pyinstaller not found on PATH. Install the desktop extra first: 'uv sync --extra desktop' (or 'uv pip install pyinstaller')."
[[ -f "${REPO_ROOT}/web/package.json" ]]        || die "missing ${REPO_ROOT}/web/package.json"
[[ -f "${REPO_ROOT}/package.json" ]]            || die "missing root ${REPO_ROOT}/package.json (provides the Tauri CLI)."
[[ -x "${SCRIPT_DIR}/build-sidecar.sh" ]]       || die "missing/non-executable ${SCRIPT_DIR}/build-sidecar.sh"
[[ -f "${REPO_ROOT}/src-tauri/tauri.conf.json" ]] || die "missing ${REPO_ROOT}/src-tauri/tauri.conf.json"

# npm ci when a lockfile exists (reproducible), else npm install.
npm_deps() {
  local dir="$1"
  if [[ -f "${dir}/package-lock.json" ]]; then
    npm --prefix "${dir}" ci
  else
    npm --prefix "${dir}" install
  fi
}

# ---------------------------------------------------------------------------
# 1. Build the web UI (Vite).
# ---------------------------------------------------------------------------
log "Step 1/4: build web UI (npm install + vite build)"
npm_deps "${REPO_ROOT}/web"
npm --prefix "${REPO_ROOT}/web" run build
[[ -f "${REPO_ROOT}/web/dist/index.html" ]] || die "web build did not produce web/dist/index.html"

# ---------------------------------------------------------------------------
# 2. Stage the built UI where the sidecar's server prefers it.
#    api/staticui.py:find_web_dist() serves src/jobsmith/web_dist over web/dist,
#    so the frozen sidecar must embed the UI from src/jobsmith/web_dist.
# ---------------------------------------------------------------------------
log "Step 2/4: stage web/dist -> src/jobsmith/web_dist"
rm -rf "${REPO_ROOT}/src/jobsmith/web_dist"
cp -R "${REPO_ROOT}/web/dist" "${REPO_ROOT}/src/jobsmith/web_dist"

# ---------------------------------------------------------------------------
# 3. Build the Python sidecar (PyInstaller onefile -> src-tauri/binaries/).
#    build-sidecar.sh requires the staged web_dist from step 2.
# ---------------------------------------------------------------------------
log "Step 3/4: build Python sidecar (PyInstaller onefile)"
"${SCRIPT_DIR}/build-sidecar.sh"

# ---------------------------------------------------------------------------
# 4. Determine signing mode, then bundle the .dmg with the Tauri CLI.
# ---------------------------------------------------------------------------
log "Step 4/4: bundle Tauri .dmg"

# Ensure the Tauri CLI (root @tauri-apps/cli devDependency) is available.
if [[ ! -x "${REPO_ROOT}/node_modules/.bin/tauri" ]]; then
  log "Installing root npm deps (provides @tauri-apps/cli)"
  npm_deps "${REPO_ROOT}"
fi

# --- Signing-mode detection (done_when #3: inspectable APPLE_* handling) ---
SIGN_ID="${APPLE_SIGNING_IDENTITY:-}"
A_ID="${APPLE_ID:-}"
A_PW="${APPLE_PASSWORD:-}"
A_TEAM="${APPLE_TEAM_ID:-}"

missing=()
[[ -z "${SIGN_ID}" ]] && missing+=("APPLE_SIGNING_IDENTITY")
[[ -z "${A_ID}"    ]] && missing+=("APPLE_ID")
[[ -z "${A_PW}"    ]] && missing+=("APPLE_PASSWORD")
[[ -z "${A_TEAM}"  ]] && missing+=("APPLE_TEAM_ID")
set_count=$(( 4 - ${#missing[@]} ))

TAURI_ARGS=("build" "--bundles" "dmg")

if (( set_count == 4 )); then
  # Full Developer ID signing + notarization.
  log "Signing mode: Developer ID + notarization (identity: ${SIGN_ID})"
  SIGN_DIR="$(mktemp -d)"
  trap 'rm -rf "${SIGN_DIR}"' EXIT
  SIGN_CONFIG="${SIGN_DIR}/sign.json"
  # Emit JSON safely (identity contains spaces/parens) via node, which is a
  # guaranteed build dependency. This --config override beats the ad-hoc "-"
  # baked into tauri.conf.json, deterministically (no reliance on env
  # precedence). APPLE_ID/PASSWORD/TEAM_ID are read from the env by Tauri's
  # notarization step, so export them for the build subprocess.
  node -e 'const fs=require("fs");fs.writeFileSync(process.argv[1],JSON.stringify({bundle:{macOS:{signingIdentity:process.env.APPLE_SIGNING_IDENTITY}}}));' "${SIGN_CONFIG}"
  export APPLE_SIGNING_IDENTITY APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID
  TAURI_ARGS+=("--config" "${SIGN_CONFIG}")
elif (( set_count == 0 )); then
  log "Signing mode: ad-hoc (signingIdentity \"-\" from tauri.conf.json — no Apple cert)."
  # Guarantee no half-configured Apple signing leaks into the build.
  unset APPLE_SIGNING_IDENTITY APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID 2>/dev/null || true
else
  # PARTIAL config -> loud warning + ad-hoc fallback (never silent).
  warn "Partial Apple signing config (${set_count}/4 vars set)."
  warn "Missing: ${missing[*]}"
  warn "Falling back to AD-HOC signing. The .dmg will NOT be signed with your"
  warn "Developer ID and will NOT be notarized."
  warn "To notarize, export ALL of: APPLE_SIGNING_IDENTITY, APPLE_ID,"
  warn "APPLE_PASSWORD (app-specific password), APPLE_TEAM_ID."
  unset APPLE_SIGNING_IDENTITY APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID 2>/dev/null || true
fi

# Append any caller-supplied extra args (e.g. --target aarch64-apple-darwin).
if (( $# > 0 )); then
  TAURI_ARGS+=("$@")
fi

cd "${REPO_ROOT}"
log "Running: npm run tauri -- ${TAURI_ARGS[*]}"
npm run tauri -- "${TAURI_ARGS[@]}"

# ---------------------------------------------------------------------------
# Report the produced artifact.
# ---------------------------------------------------------------------------
DMG_DIR="${REPO_ROOT}/src-tauri/target/release/bundle/dmg"
log "Build complete."
if compgen -G "${DMG_DIR}/*.dmg" >/dev/null; then
  printf 'Produced .dmg:\n'
  ls -lh "${DMG_DIR}"/*.dmg
else
  warn "No .dmg found under ${DMG_DIR} — check the Tauri build output above."
fi
