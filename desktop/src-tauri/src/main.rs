// Tray / menu-bar app for Wyze Tunnel. The Wyze camera library only exists for Linux,
// so this sets up and supervises a Linux side (a Lima VM on macOS, a WSL2 distro on
// Windows) using the repo's installer scripts. The cameras themselves are in the bridge's web UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

#[cfg(target_os = "macos")]
const PORT: u16 = 5050; // macOS AirPlay holds 5000; mac/wyze-tunnel.yaml forwards to 5050
#[cfg(not(target_os = "macos"))]
const PORT: u16 = 5000;
const RELEASES: &str = "https://github.com/itskiwi/Wyze-Tunnel/releases";

static STOPPED: AtomicBool = AtomicBool::new(false);
static BUSY: AtomicBool = AtomicBool::new(false); // an install/update is running; don't supervise

fn cmd(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut c = Command::new(program);
    c.env("WSL_UTF8", "1").stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

#[cfg(not(windows))]
mod vm {
    use super::cmd;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    // Same paths as mac/install.sh. LIMA_HOME is short because macOS caps socket paths at 104 bytes.
    fn home() -> PathBuf {
        PathBuf::from(std::env::var("HOME").unwrap_or_default())
    }
    fn limactl(args: &[&str]) -> Command {
        let mut c = cmd(home().join("Library/Application Support/Wyze Tunnel/lima/bin/limactl"));
        c.env("LIMA_HOME", home().join(".wyze-tunnel")).args(args);
        c
    }
    pub fn installed() -> bool {
        home().join(".wyze-tunnel/wyze-tunnel").exists()
    }
    pub fn install_cmd(res: &Path) -> Command {
        let mut c = cmd("/bin/bash");
        c.arg(res.join("mac/install.sh")).env("WT_NO_OPEN", "1");
        c
    }
    pub fn ensure_running() {
        let out = limactl(&["list", "--format", "{{.Status}}", "wyze-tunnel"]).output();
        if !out.is_ok_and(|o| o.stdout.starts_with(b"Running")) {
            let _ = limactl(&["start", "--tty=false", "wyze-tunnel"]).output();
        }
    }
    pub fn stop() {
        let _ = limactl(&["stop", "wyze-tunnel"]).output();
    }
    pub fn as_root(args: &[&str]) -> Command {
        let mut c = limactl(&["shell", "--workdir", "/", "wyze-tunnel", "sudo"]);
        c.args(args);
        c
    }
    pub fn open_url(url: &str) {
        let _ = cmd("open").arg(url).spawn();
    }
}

#[cfg(windows)]
mod vm {
    use super::cmd;
    use std::path::Path;
    use std::process::{Child, Command};
    use std::sync::Mutex;

    const DISTRO: &str = "WyzeTunnel";
    // WSL shuts a distro down once no wsl.exe process is attached, so we keep one open.
    static KEEPALIVE: Mutex<Option<Child>> = Mutex::new(None);

    pub fn installed() -> bool {
        cmd("wsl.exe").args(["-l", "-q"]).output()
            // NUL stripping: older inbox wsl.exe ignores WSL_UTF8 and prints UTF-16.
            .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).replace('\0', "").lines().any(|l| l.trim() == DISTRO))
    }
    pub fn install_cmd(res: &Path) -> Command {
        let mut c = cmd("powershell.exe");
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]).arg(res.join("windows/install.ps1"));
        c
    }
    pub fn ensure_running() {
        let mut k = KEEPALIVE.lock().unwrap();
        if k.as_mut().map_or(true, |c| !matches!(c.try_wait(), Ok(None))) {
            *k = cmd("wsl.exe").args(["-d", DISTRO, "-u", "root", "--exec", "sleep", "infinity"]).spawn().ok();
        }
    }
    pub fn stop() {
        if let Some(mut c) = KEEPALIVE.lock().unwrap().take() {
            let _ = c.kill();
        }
        let _ = cmd("wsl.exe").args(["--terminate", DISTRO]).output();
    }
    pub fn as_root(args: &[&str]) -> Command {
        let mut c = cmd("wsl.exe");
        c.args(["-d", DISTRO, "-u", "root", "--exec"]).args(args);
        c
    }
    pub fn open_url(url: &str) {
        let _ = cmd("explorer.exe").arg(url).spawn();
    }
}

fn bridge_up() -> bool {
    TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], PORT)), Duration::from_secs(1)).is_ok()
}

/// Tag of the newest desktop release, if it differs from this build.
fn newer_release() -> Option<String> {
    // shortcut: curl ships with macOS and Windows 10+, so no HTTP crate; treats any different tag as newer.
    // Bridge releases (v*) share the repo, so look for the newest desktop-v* tag.
    let out = cmd("curl").args(["-fsSL", "https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases?per_page=30"]).output().ok()?;
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    let tag = json.as_array()?.iter().filter_map(|r| r["tag_name"].as_str()).find(|t| t.starts_with("desktop-v"))?;
    (tag != concat!("desktop-v", env!("CARGO_PKG_VERSION"))).then(|| tag.to_string())
}

fn url() -> String {
    format!("http://localhost:{PORT}")
}

#[tauri::command]
fn info() -> serde_json::Value {
    serde_json::json!({ "installed": vm::installed(), "url": url() })
}

/// The web UI's login password, written by native/install.sh.
#[tauri::command]
async fn password() -> String {
    tauri::async_runtime::spawn_blocking(|| {
        let out = vm::as_root(&[
            "sh",
            "-c",
            "[ -f /var/lib/wyze-tunnel/tokens/wb_login ] || sed -n 's/^WB_PASSWORD=//p' /etc/wyze-tunnel/wyze-tunnel.env",
        ]).output();
        out.map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default()
    })
    .await
    .unwrap_or_default()
}

/// The web UI opens in the browser: it uses HTTP basic auth, which macOS's webview can't prompt for.
#[tauri::command]
fn open_ui() {
    vm::open_url(&url());
}

/// Runs the platform installer (first setup or update), streaming its output as "log" events.
/// Returns the exit code; 3 means Windows needs a restart to finish turning on WSL.
#[tauri::command]
async fn install(app: AppHandle) -> Result<i32, String> {
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || run_installer(&app, &res)).await.map_err(|e| e.to_string())?
}

fn run_installer(app: &AppHandle, res: &Path) -> Result<i32, String> {
    if BUSY.swap(true, Ordering::SeqCst) {
        return Err("Setup is already running.".into());
    }
    let result = (|| {
        let mut child = vm::install_cmd(res).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
        let stderr = child.stderr.take().unwrap();
        let app2 = app.clone();
        let errs = std::thread::spawn(move || {
            BufReader::new(stderr).lines().map_while(Result::ok).for_each(|l| drop(app2.emit("log", l)))
        });
        BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok).for_each(|l| drop(app.emit("log", l)));
        let _ = errs.join();
        child.wait().map(|s| s.code().unwrap_or(1)).map_err(|e| e.to_string())
    })();
    BUSY.store(false, Ordering::SeqCst);
    if result == Ok(0) {
        STOPPED.store(false, Ordering::SeqCst);
        let _ = app.autolaunch().enable();
    }
    result
}

fn open_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let _ = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Wyze Tunnel")
        .inner_size(640.0, 560.0)
        .build();
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| open_window(app)))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .invoke_handler(tauri::generate_handler![info, install, password, open_ui])
        .setup(|app| {
            let item = |id: &str, text: &str| MenuItem::with_id(app, id, text, true, None::<&str>);
            let update = item("update", "Check for updates")?;
            let menu = Menu::with_items(app, &[
                &item("open", "Open Wyze Tunnel")?,
                &item("status", "Status and password")?,
                &item("start", "Start")?,
                &item("stop", "Stop")?,
                &update,
                &item("quit", "Quit")?,
            ])?;
            let tray = TrayIconBuilder::with_id("tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Wyze Tunnel")
                .menu(&menu)
                .on_menu_event(|app, e| match e.id.as_ref() {
                    "open" if bridge_up() => open_ui(),
                    "open" => open_window(app),
                    "status" => open_window(app),
                    "start" => {
                        STOPPED.store(false, Ordering::SeqCst);
                        std::thread::spawn(vm::ensure_running);
                    }
                    "stop" => {
                        STOPPED.store(true, Ordering::SeqCst);
                        std::thread::spawn(vm::stop);
                    }
                    "update" => {
                        // Reinstalling updates the Linux side; a newer app goes to the releases page.
                        if let Some(tag) = newer_release() {
                            vm::open_url(&format!("{RELEASES}/tag/{tag}"));
                        } else if let Ok(res) = app.path().resource_dir() {
                            let app = app.clone();
                            std::thread::spawn(move || run_installer(&app, &res));
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            if !std::env::args().any(|a| a == "--hidden") {
                open_window(app.handle());
            }

            let update_item = update.clone();
            std::thread::spawn(move || {
                if let Some(tag) = newer_release() {
                    let _ = update_item.set_text(format!("Update available ({tag})"));
                }
            });
            std::thread::spawn(move || loop {
                let busy = BUSY.load(Ordering::SeqCst);
                let stopped = STOPPED.load(Ordering::SeqCst);
                let installed = busy || vm::installed();
                if !busy && !stopped && installed {
                    vm::ensure_running();
                }
                let state = match (busy, bridge_up(), stopped, installed) {
                    (true, ..) => "updating",
                    (_, true, ..) => "running",
                    (_, _, true, _) => "stopped",
                    (.., false) => "not set up",
                    _ => "starting",
                };
                let _ = tray.set_tooltip(Some(format!("Wyze Tunnel: {state}")));
                std::thread::sleep(Duration::from_secs(15));
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Wyze Tunnel")
        .run(|_, event| {
            // Closing the window keeps the tray app (and the cameras) running.
            if let RunEvent::ExitRequested { code: None, api, .. } = event {
                api.prevent_exit();
            }
        });
}
