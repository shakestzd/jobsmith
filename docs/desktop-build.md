# Desktop build (macOS .dmg)

`scripts/build-desktop.sh` produces a self-contained Jobsmith `.dmg`. The
bundle embeds the React UI **and** a frozen Python backend (PyInstaller
onefile), so the installed app needs **no host Python or Node at runtime**.

## What the script does

One command runs the whole pipeline:

```bash
scripts/build-desktop.sh
```

| Step | Action |
|---|---|
| 1 | Build the web UI: `npm --prefix web {ci\|install}` then `npm --prefix web run build` (Vite). |
| 2 | Stage `web/dist/` → `src/jobsmith/web_dist/` (the sidecar server prefers this path; see `api/staticui.py:find_web_dist`). |
| 3 | Build the Python sidecar via `scripts/build-sidecar.sh` (PyInstaller **onefile** → `src-tauri/binaries/jobsmith-sidecar-<host-triple>`). |
| 4 | Bundle the Tauri app: `npm run tauri -- build --bundles dmg`. |

Output: `src-tauri/target/release/bundle/dmg/Jobsmith_<version>_<arch>.dmg`
(the `.app` is at `src-tauri/target/release/bundle/macos/Jobsmith.app`).

## Prerequisites (build host only — NOT the install host)

- **Node** (npm) — web build + Tauri CLI.
- **Rust** (`rustc`, `cargo`) — Tauri release compile + host-triple derivation.
- **PyInstaller** — install the desktop extra:
  ```bash
  uv sync --extra desktop   # or: uv pip install pyinstaller
  ```
- A working **Xcode Command Line Tools** install (`codesign`, `xcrun`).

The Tauri CLI itself is the root `@tauri-apps/cli` devDependency; the script
runs `npm install` at the repo root if `node_modules/.bin/tauri` is absent.

## Local build (ad-hoc signing — default, no Apple Developer account)

```bash
uv sync --extra desktop
scripts/build-desktop.sh
```

`tauri.conf.json` sets `bundle.macOS.signingIdentity: "-"`, so the app is
**ad-hoc signed**. The resulting `.dmg` runs after a **one-time Gatekeeper
approval**:

1. Open the `.dmg`, drag **Jobsmith** to `/Applications`.
2. First launch: macOS may block it ("unidentified developer"). Either
   right-click → **Open** → **Open**, or approve it in
   **System Settings → Privacy & Security → Open Anyway**.

No Apple Developer certificate is needed for this path.

## Notarized build (for distribution to other Macs)

Notarization removes the Gatekeeper warning on machines that have never seen
the app. Export **all four** variables, then run the same script:

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID123)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="abcd-efgh-ijkl-mnop"   # app-specific password — see below
export APPLE_TEAM_ID="TEAMID123"
scripts/build-desktop.sh
```

> **`APPLE_PASSWORD` is an _app-specific password_**, generated at
> <https://appleid.apple.com> → **Sign-In and Security → App-Specific
> Passwords**. It is **NOT** your Apple ID account password. Using the account
> password will fail notarization.

- `APPLE_SIGNING_IDENTITY` — your **Developer ID Application** certificate name
  (must be installed in the login keychain). The script passes it to
  `tauri build` via an inline `--config` override, replacing the ad-hoc `"-"`.
- `APPLE_ID` / `APPLE_PASSWORD` / `APPLE_TEAM_ID` — read from the environment by
  Tauri's notarization step.

**Partial config is never silent:** if some — but not all four — variables are
set, the script prints a `WARNING`, lists the missing ones, and **falls back to
ad-hoc signing** (no Developer-ID signing, no notarization).

> App Store Connect API key notarization (`APPLE_API_ISSUER` / `APPLE_API_KEY`
> / `APPLE_API_KEY_PATH`) is also supported by Tauri but is **not** wired into
> this script's detection logic; use the Apple ID method above.

## Verifying the bundle

```bash
# Ad-hoc build should report "Signature=adhoc":
codesign -dv --verbose=4 src-tauri/target/release/bundle/macos/Jobsmith.app

# Notarized build:
spctl -a -vvv -t install src-tauri/target/release/bundle/macos/Jobsmith.app
xcrun stapler validate src-tauri/target/release/bundle/dmg/Jobsmith_*.dmg
```

## Clean-host install check (the real Constraint-1 test)

The "no Python/Node at runtime" guarantee can only be proven on a host without
the build toolchain:

1. Copy the `.dmg` to a clean macOS machine (or a fresh VM) that has **no**
   Homebrew Python, no system `python3` on PATH for Jobsmith's use, and no Node.
2. Install + launch as above. The Tauri shell spawns the bundled
   `jobsmith-sidecar` (frozen Python) — it must start and serve the UI with no
   external interpreter.

This step is **manual** and is the canonical acceptance test for the desktop
bundle.

## CI

`.github/workflows/desktop-build.yml` runs this pipeline on a macOS ARM runner
and uploads the `.dmg` as a build artifact. Notarization secrets
(`APPLE_*`) are optional repo secrets; without them the CI build is ad-hoc.

## References

- Tauri v2 macOS signing & notarization:
  <https://v2.tauri.app/distribute/sign/macos/>
