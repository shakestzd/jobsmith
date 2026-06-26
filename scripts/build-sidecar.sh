#!/usr/bin/env bash
#
# build-sidecar.sh — build the jobsmith desktop sidecar as a PyInstaller
# onefile binary and stage it for Tauri (feat-b621a4ab, slice 1).
#
# Contract:
#   * REQUIRES the built web UI pre-staged at src/jobsmith/web_dist/index.html.
#     This script does NOT run npm — stage the UI first (e.g. `npm run build`
#     in web/ then copy web/dist/ -> src/jobsmith/web_dist/).
#   * Runs `pyinstaller` against packaging/jobsmith-sidecar.spec (onefile).
#   * Renames the output to src-tauri/binaries/jobsmith-sidecar-<triple>, where
#     <triple> is the rustc host triple (Tauri's external-binary naming).
#   * Does NOT touch the committed src-tauri/ tree beyond creating binaries/.
#
# Usage: scripts/build-sidecar.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INDEX_HTML="${REPO_ROOT}/src/jobsmith/web_dist/index.html"
SPEC="${REPO_ROOT}/packaging/jobsmith-sidecar.spec"

# 1. Require the pre-staged web UI.
if [[ ! -f "${INDEX_HTML}" ]]; then
  echo "ERROR: ${INDEX_HTML} not found." >&2
  echo "       Stage the built web UI first (this script does not run npm):" >&2
  echo "         (cd web && npm run build) && rm -rf src/jobsmith/web_dist \\" >&2
  echo "           && cp -R web/dist src/jobsmith/web_dist" >&2
  exit 1
fi

if [[ ! -f "${SPEC}" ]]; then
  echo "ERROR: PyInstaller spec not found at ${SPEC}" >&2
  exit 1
fi

# 2. Derive the rustc host triple robustly (works on all rustc versions).
if ! command -v rustc >/dev/null 2>&1; then
  echo "ERROR: rustc not found on PATH — required to derive the host triple." >&2
  exit 1
fi
TRIPLE="$(rustc -vV | sed -n 's/host: //p')"
if [[ -z "${TRIPLE}" ]]; then
  echo "ERROR: could not determine host triple from 'rustc -vV'." >&2
  exit 1
fi

# 3. Build (onefile) into a scratch dir so the repo root stays clean. Only the
#    final renamed binary lands under src-tauri/binaries (a gitignored artifact).
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "Building jobsmith sidecar (triple=${TRIPLE}) ..."
# Ensure the jobsmith package is importable during spec evaluation
# (collect_submodules imports it) even in a src-only / non-installed env.
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
( cd "${REPO_ROOT}" && pyinstaller \
    --noconfirm \
    --clean \
    --distpath "${WORK_DIR}/dist" \
    --workpath "${WORK_DIR}/build" \
    "${SPEC}" )

BUILT="${WORK_DIR}/dist/jobsmith-sidecar"
if [[ ! -f "${BUILT}" ]]; then
  echo "ERROR: expected onefile binary not produced at ${BUILT}" >&2
  exit 1
fi

# 4. Stage into src-tauri/binaries with Tauri's <name>-<triple> convention.
DEST_DIR="${REPO_ROOT}/src-tauri/binaries"
mkdir -p "${DEST_DIR}"
DEST="${DEST_DIR}/jobsmith-sidecar-${TRIPLE}"
mv -f "${BUILT}" "${DEST}"
chmod +x "${DEST}"

echo "Built sidecar: ${DEST}"
