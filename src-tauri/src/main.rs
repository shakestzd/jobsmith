// Jobsmith desktop shell — Tauri v2 (slice-3).
//
// This shell spawns the Python sidecar, discovers its dynamically chosen
// loopback port, polls /health, then navigates the window from the static
// splash to the authenticated React UI. Closing the window is an explicit quit
// that tears the sidecar down so nothing is orphaned.
//
// Prevents a spare console window on Windows release builds (no-op on macOS).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod sidecar;

use tauri::{Manager, WindowEvent};

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
        // shell: spawn the bundled sidecar (scoped by capabilities/default.json
        // -> shell:allow-spawn). dialog: native boot-failure dialog.
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // The Tauri shell owns the API token: generate a fresh random one
            // per launch and hand it to the sidecar via its environment. The
            // sidecar's auth cache captures it on first read, so the injected
            // localhost shim and the spawned server agree with zero Python
            // changes.
            let token = uuid::Uuid::new_v4().to_string();
            app.manage(sidecar::SidecarState::new(token.clone()));
            sidecar::launch(app.handle().clone(), token);
            Ok(())
        })
        // Closing the main window is an explicit quit: kill the sidecar so it is
        // never orphaned, then exit immediately (avoids a UI-less macOS dock
        // icon lingering after the window is gone).
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::CloseRequested { .. }) && window.label() == "main" {
                if let Some(state) = window.try_state::<sidecar::SidecarState>() {
                    state.shutdown();
                }
                std::process::exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Jobsmith desktop application");
}
