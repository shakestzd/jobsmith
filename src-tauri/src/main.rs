// Jobsmith desktop shell — Tauri v2 skeleton (slice-2).
//
// Scope of THIS slice: a compiling Rust shell that registers the
// single-instance + shell plugins and opens a window on the static splash
// page. The Python sidecar is NOT spawned here — that wiring lands in slice-3.
//
// Prevents a spare console window on Windows release builds (no-op on macOS).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

fn main() {
    let builder = tauri::Builder::default();

    // single-instance must be registered FIRST (plugins run in registration
    // order). A second launch focuses the existing window instead of spawning
    // a duplicate process. Gated to desktop targets — mirrors the dependency
    // gate in Cargo.toml (the plugin is unavailable on android/ios).
    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.unminimize();
            let _ = window.set_focus();
        }
    }));

    builder
        // The shell plugin is registered here; the actual sidecar spawn (scoped
        // by capabilities/default.json -> shell:allow-spawn) happens in slice-3.
        .plugin(tauri_plugin_shell::init())
        .setup(|_app| {
            // slice-3: spawn sidecar, read JOBSMITH_LISTENING_PORT, navigate
            // the "main" window from the splash to the React UI once the
            // backend reports its listening port.
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Jobsmith desktop application");
}
