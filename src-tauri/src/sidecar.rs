//! Python sidecar lifecycle for the Jobsmith desktop shell (slice-3).
//!
//! Responsibilities:
//!   * Spawn the bundled `jobsmith-sidecar` external binary with the parent's
//!     `JOBSMITH_API_TOKEN` already in its environment (the sidecar's auth
//!     cache captures the token on first read — no Python change required).
//!   * Parse the `JOBSMITH_LISTENING_PORT=<n>` sentinel the sidecar prints on
//!     stdout, and tee ALL stdout/stderr to `~/Library/Logs/Jobsmith/sidecar.log`.
//!   * Poll `GET /health` until the server answers 200, then navigate the main
//!     window to the authenticated loopback UI.
//!   * On boot failure, surface a native Retry / Open-Log dialog (never a blank
//!     window), and tear the child down cleanly on quit (no orphan process).
//!
//! The health probe is a deliberate std-only TCP/HTTP/1.0 GET rather than a
//! `reqwest`/`ureq` dependency: it is a single loopback request, keeping it
//! hermetic, unit-testable, and dependency-light.

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const SIDECAR_NAME: &str = "jobsmith-sidecar";
const MAIN_WINDOW: &str = "main";
const LOOPBACK: &str = "127.0.0.1";
const TOKEN_ENV_VAR: &str = "JOBSMITH_API_TOKEN";
const PORT_SENTINEL_PREFIX: &str = "JOBSMITH_LISTENING_PORT=";
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const CONNECT_TIMEOUT: Duration = Duration::from_millis(1000);
const DIALOG_TITLE: &str = "Jobsmith failed to start";
const RETRY_LABEL: &str = "Retry";
const OPEN_LOG_LABEL: &str = "Open Log";

/// Shared lifecycle state: the running child plus the token used to (re)spawn it.
pub struct SidecarState {
    child: Mutex<Option<CommandChild>>,
    token: String,
}

impl SidecarState {
    /// Construct state for a freshly generated per-launch token.
    pub fn new(token: String) -> Self {
        Self {
            child: Mutex::new(None),
            token,
        }
    }

    fn set_child(&self, child: CommandChild) {
        if let Ok(mut guard) = self.child.lock() {
            *guard = Some(child);
        }
    }

    fn take_child(&self) -> Option<CommandChild> {
        self.child.lock().ok().and_then(|mut guard| guard.take())
    }

    fn token(&self) -> String {
        self.token.clone()
    }

    /// Kill the sidecar if it is still running. Used on window-close so the
    /// child never outlives the shell.
    pub fn shutdown(&self) {
        if let Some(child) = self.take_child() {
            let _ = child.kill();
        }
    }
}

/// Launch the sidecar; on a synchronous spawn failure surface the boot dialog.
pub fn launch(app: AppHandle, token: String) {
    if let Err(error) = start(app.clone(), token) {
        let log_path = sidecar_log_path();
        LogTee::open(&log_path).write("sys", &format!("spawn failed: {error}"));
        report_failure(app, log_path);
    }
}

/// Spawn the sidecar and stream its output. Returns once the child is spawned
/// and stored; readiness/navigation proceed asynchronously.
fn start(app: AppHandle, token: String) -> Result<(), String> {
    let (mut rx, child) = app
        .shell()
        .sidecar(SIDECAR_NAME)
        .map_err(|e| format!("sidecar lookup failed: {e}"))?
        .env(TOKEN_ENV_VAR, token.as_str())
        .spawn()
        .map_err(|e| format!("sidecar spawn failed: {e}"))?;

    app.state::<SidecarState>().set_child(child);
    let log_path = sidecar_log_path();

    tauri::async_runtime::spawn(async move {
        let mut log = LogTee::open(&log_path);
        let mut port_handled = false;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes);
                    log.write("out", &line);
                    if !port_handled {
                        if let Some(port) = parse_listening_port(&line) {
                            port_handled = true;
                            spawn_readiness(app.clone(), port, log_path.clone());
                        }
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    log.write("err", &String::from_utf8_lossy(&bytes));
                }
                CommandEvent::Error(message) => log.write("err", &message),
                CommandEvent::Terminated(payload) => {
                    log.write("sys", &format!("sidecar terminated: {payload:?}"));
                    if !port_handled {
                        report_failure(app.clone(), log_path.clone());
                    }
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

/// Poll the sidecar's /health on a worker thread, then navigate or report.
fn spawn_readiness(app: AppHandle, port: u16, log_path: PathBuf) {
    std::thread::spawn(move || {
        if poll_health(LOOPBACK, port, HEALTH_TIMEOUT) {
            navigate_main(&app, port);
        } else {
            handle_boot_failure(&app, &log_path);
        }
    });
}

/// Run the (blocking) boot-failure dialog flow on a dedicated thread so the
/// native dialog is never shown from an async worker or the main thread.
fn report_failure(app: AppHandle, log_path: PathBuf) {
    std::thread::spawn(move || handle_boot_failure(&app, &log_path));
}

/// Point the main window at the authenticated loopback UI.
fn navigate_main(app: &AppHandle, port: u16) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    if let Ok(url) = tauri::Url::parse(&format!("http://{LOOPBACK}:{port}/")) {
        let _ = window.navigate(url);
    }
}

/// Loop the native error dialog until a relaunch succeeds. The splash page
/// stays visible behind it, so the window is never blank.
fn handle_boot_failure(app: &AppHandle, log_path: &Path) {
    loop {
        if show_failure_dialog(app, log_path) {
            // Retry: a successful relaunch hands navigation to a fresh reader.
            if restart(app) {
                return;
            }
        } else {
            // Open Log: reveal the log, then re-prompt so Retry stays reachable.
            open_log_file(log_path);
        }
    }
}

/// Show the native Retry / Open-Log error dialog. Returns true when the primary
/// (Retry) button was chosen, false for the Open-Log affordance.
fn show_failure_dialog(app: &AppHandle, log_path: &Path) -> bool {
    let message = format!(
        "Jobsmith couldn't start its local server.\n\nThe log file is at:\n{}",
        log_path.display()
    );
    app.dialog()
        .message(message)
        .title(DIALOG_TITLE)
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::OkCancelCustom(
            RETRY_LABEL.to_string(),
            OPEN_LOG_LABEL.to_string(),
        ))
        .blocking_show()
}

/// Kill any running child and respawn the sidecar. Returns whether the respawn
/// was issued successfully.
fn restart(app: &AppHandle) -> bool {
    let state = app.state::<SidecarState>();
    if let Some(child) = state.take_child() {
        let _ = child.kill();
    }
    match start(app.clone(), state.token()) {
        Ok(()) => true,
        Err(error) => {
            LogTee::open(&sidecar_log_path()).write("sys", &format!("restart failed: {error}"));
            false
        }
    }
}

/// Reveal the sidecar log in the macOS default viewer.
fn open_log_file(log_path: &Path) {
    let _ = std::process::Command::new("open").arg(log_path).spawn();
}

/// Parse a single stdout line into the sidecar's listening port, if present.
fn parse_listening_port(line: &str) -> Option<u16> {
    let value = line.trim().strip_prefix(PORT_SENTINEL_PREFIX)?;
    match value.parse::<u16>() {
        Ok(port) if port != 0 => Some(port),
        _ => None,
    }
}

/// Poll `GET http://host:port/health` until it answers 200 or the timeout
/// elapses. Blocking; intended to run on a worker thread.
fn poll_health(host: &str, port: u16, timeout: Duration) -> bool {
    let addr = format!("{host}:{port}");
    let deadline = Instant::now() + timeout;
    loop {
        if http_get_health_ok(&addr) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// One `/health` request: connect, send a minimal HTTP/1.0 GET, and check that
/// the status line reports 200.
fn http_get_health_ok(addr: &str) -> bool {
    let Some(socket) = addr.to_socket_addrs().ok().and_then(|mut it| it.next()) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&socket, CONNECT_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(CONNECT_TIMEOUT));
    let _ = stream.set_write_timeout(Some(CONNECT_TIMEOUT));
    let request = format!("GET /health HTTP/1.0\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    read_status_line(&mut stream)
        .and_then(|line| line.split_whitespace().nth(1).map(str::to_owned))
        .is_some_and(|code| code == "200")
}

/// Read bytes until the first newline and return the (trimmed) status line.
fn read_status_line(stream: &mut TcpStream) -> Option<String> {
    let mut buffer = Vec::with_capacity(128);
    let mut chunk = [0u8; 128];
    loop {
        let read = stream.read(&mut chunk).ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(pos) = buffer.iter().position(|&b| b == b'\n') {
            return Some(String::from_utf8_lossy(&buffer[..pos]).trim().to_string());
        }
        if buffer.len() > 512 {
            return None;
        }
    }
}

/// `~/Library/Logs/Jobsmith/sidecar.log` (falling back to a temp dir if `$HOME`
/// is somehow unset).
fn sidecar_log_path() -> PathBuf {
    let base = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    base.join("Library")
        .join("Logs")
        .join("Jobsmith")
        .join("sidecar.log")
}

/// Append-only tee of sidecar output to the log file (best-effort).
struct LogTee {
    file: Option<File>,
}

impl LogTee {
    fn open(path: &Path) -> Self {
        if let Some(parent) = path.parent() {
            let _ = create_dir_all(parent);
        }
        let file = OpenOptions::new().create(true).append(true).open(path).ok();
        Self { file }
    }

    fn write(&mut self, tag: &str, line: &str) {
        if let Some(file) = self.file.as_mut() {
            let _ = writeln!(file, "[{tag}] {}", line.trim_end());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn parse_listening_port_extracts_valid_port() {
        assert_eq!(
            parse_listening_port("JOBSMITH_LISTENING_PORT=54321"),
            Some(54321)
        );
        // A trailing newline (as delivered on stdout) is tolerated.
        assert_eq!(
            parse_listening_port("JOBSMITH_LISTENING_PORT=8080\n"),
            Some(8080)
        );
    }

    #[test]
    fn parse_listening_port_rejects_malformed() {
        assert_eq!(parse_listening_port(""), None);
        assert_eq!(parse_listening_port("JOBSMITH_LISTENING_PORT="), None);
        assert_eq!(
            parse_listening_port("JOBSMITH_LISTENING_PORT=notaport"),
            None
        );
        assert_eq!(parse_listening_port("INFO: uvicorn running"), None);
        assert_eq!(parse_listening_port("JOBSMITH_LISTENING_PORT=70000"), None);
        assert_eq!(parse_listening_port("JOBSMITH_LISTENING_PORT=0"), None);
    }

    #[test]
    fn poll_health_succeeds_against_200_server() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let _ = stream.read(&mut [0u8; 256]);
                let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");
            }
        });
        assert!(poll_health("127.0.0.1", port, Duration::from_secs(2)));
        let _ = server.join();
    }

    #[test]
    fn poll_health_times_out_when_nothing_listens() {
        // Bind then drop to obtain a port nothing is listening on.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let started = Instant::now();
        assert!(!poll_health("127.0.0.1", port, Duration::from_millis(300)));
        assert!(started.elapsed() >= Duration::from_millis(250));
    }

    #[test]
    fn http_get_health_ok_rejects_non_200() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let _ = stream.read(&mut [0u8; 256]);
                let _ = stream.write_all(b"HTTP/1.1 503 Service Unavailable\r\n\r\n");
            }
        });
        assert!(!http_get_health_ok(&format!("127.0.0.1:{port}")));
        let _ = server.join();
    }
}
