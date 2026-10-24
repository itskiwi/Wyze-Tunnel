// Tray / menu-bar app for Wyze Tunnel. The Wyze camera library only exists for Linux, so on macOS
// this sets up and supervises a Lima VM. On Windows the bridge runs natively and reaches cameras
// through go2rtc; a WSL2 distro with the Wyze library is the opt-in "full camera support".
// Setup is the repo's installer scripts. The cameras themselves are in the bridge's web UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

#[cfg(target_os = "macos")]
const PORT: u16 = 5050; // macOS AirPlay holds 5000; mac/wyze-tunnel.yaml forwards to 5050
#[cfg(not(target_os = "macos"))]
const PORT: u16 = 5000;
const RELEASES: &str = "https://github.com/itskiwi/Wyze-Tunnel/releases";

static STOPPED: AtomicBool = AtomicBool::new(false);
static BUSY: AtomicBool = AtomicBool::new(false); // an install/update is running; don't supervise
static UPDATE_NOW: AtomicBool = AtomicBool::new(false); // the tray's update item was clicked

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
    pub fn install_cmd(res: &Path, _full: bool) -> Command {
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
    /// The VM updates itself (wyze-tunnel-update runs daily); true once that's been kicked off.
    pub fn self_update() -> bool {
        as_root(&["systemctl", "start", "--no-block", "wyze-tunnel-update"]).output().is_ok()
    }
    pub fn update_requested() -> bool {
        false // the VM's systemd path unit handles the web UI's update button
    }
    pub fn password() -> String {
        super::stdout_of(as_root(&["sh", "-c", super::LINUX_PASSWORD]))
    }
    pub fn version() -> String {
        super::stdout_of(as_root(super::LINUX_VERSION))
    }
    pub fn watching() -> bool {
        false // the VM's own updater waits for viewers
    }
    pub fn quit() {}
    pub fn open_url(url: &str) {
        let _ = cmd("open").arg(url).spawn();
    }
}

#[cfg(windows)]
mod vm {
    use super::cmd;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command};
    use std::sync::Mutex;

    const DISTRO: &str = "WyzeTunnel";
    // The native bridge, or in WSL mode a wsl.exe kept open: WSL shuts a distro down once none is attached.
    static CHILD: Mutex<Option<Child>> = Mutex::new(None);

    fn dir() -> PathBuf {
        PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default()).join("Wyze Tunnel")
    }
    fn native() -> PathBuf {
        dir().join(r"native\wyze-tunnel")
    }
    fn data() -> PathBuf {
        dir().join("data")
    }
    fn wsl() -> bool {
        cmd("wsl.exe").args(["-l", "-q"]).output()
            // NUL stripping: older inbox wsl.exe ignores WSL_UTF8 and prints UTF-16.
            .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).replace('\0', "").lines().any(|l| l.trim() == DISTRO))
    }
    pub fn installed() -> bool {
        native().join(r"python\python.exe").exists() || wsl()
    }
    /// Native setup by default (no permission prompt or restart); `full` (or an existing distro) is the WSL setup.
    pub fn install_cmd(res: &Path, full: bool) -> Command {
        let full = full || wsl();
        if !full {
            stop(); // the update replaces files the running bridge holds open
        }
        let mut c = cmd("powershell.exe");
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
            .arg(res.join(if full { "windows/install.ps1" } else { "windows/install-native.ps1" }));
        c
    }
    pub fn ensure_running() {
        let mut child = CHILD.lock().unwrap();
        if !child.as_mut().is_some_and(|c| matches!(c.try_wait(), Ok(None))) {
            *child = if wsl() {
                cmd("wsl.exe").args(["-d", DISTRO, "-u", "root", "--exec", "sleep", "infinity"]).spawn().ok()
            } else {
                start_native()
            };
        }
    }
    /// The same environment the Linux systemd unit sets up, with the settings file on top.
    fn start_native() -> Option<Child> {
        let (n, d) = (native(), data());
        for sub in ["tokens", "img", "record", "logs"] {
            fs::create_dir_all(d.join(sub)).ok()?;
        }
        fs::copy(n.join("mediamtx.yml"), d.join("mediamtx.yml")).ok()?; // fresh each start; the bridge edits it
        let log = fs::File::create(d.join("bridge.log")).ok()?;
        let path = std::env::var("PATH").unwrap_or_default();
        let mut c = cmd(n.join(r"python\python.exe"));
        c.args(["-m", "flask", "run", "--host=0.0.0.0"])
            .current_dir(n.join("app"))
            .env("FLASK_APP", "frontend")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONUTF8", "1") // the logs print emoji; the default cp1252 console encoding crashes on them
            .env("PATH", format!("{};{path}", n.join("bin").display()))
            .env("TOKEN_PATH", d.join("tokens"))
            .env("IMG_DIR", d.join("img"))
            .env("RECORD_PATH", d.join(r"record\%path\%Y-%m-%d_%H-%M-%S-%f"))
            .env("LOG_PATH", d.join("logs"))
            .env("MEDIAMTX_BIN", n.join("mediamtx.exe"))
            .env("MEDIAMTX_CONFIG", d.join("mediamtx.yml"))
            .env("GO2RTC_BIN", n.join("go2rtc.exe"))
            .env("GO2RTC_CONFIG", d.join("go2rtc.yaml"))
            .env("TUTK_LIB", n.join("no-wyze-sdk")) // missing on purpose: cameras connect through go2rtc
            .env("WT_UPDATE_FILE", d.join("update-now"))
            .envs(settings())
            .stdout(log.try_clone().ok()?)
            .stderr(log);
        c.spawn().ok()
    }
    fn settings() -> Vec<(String, String)> {
        let text = fs::read_to_string(dir().join("wyze-tunnel.env")).unwrap_or_default();
        text.lines()
            .map(str::trim)
            .filter(|l| !l.starts_with('#'))
            .filter_map(|l| l.split_once('='))
            .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
            .collect()
    }
    pub fn stop() {
        if let Some(mut c) = CHILD.lock().unwrap().take() {
            // /T: the native bridge's MediaMTX, go2rtc and ffmpeg children go too.
            let _ = cmd("taskkill").args(["/T", "/F", "/PID", &c.id().to_string()]).output();
            let _ = c.wait();
        }
        if wsl() {
            let _ = cmd("wsl.exe").args(["--terminate", DISTRO]).output();
        }
    }
    /// Quitting the tray app stops a native bridge (it would hold its ports); a WSL distro keeps running.
    pub fn quit() {
        if !wsl() {
            stop();
        }
    }
    fn as_root(args: &[&str]) -> Command {
        let mut c = cmd("wsl.exe");
        c.args(["-d", DISTRO, "-u", "root", "--exec"]).args(args);
        c
    }
    pub fn self_update() -> bool {
        wsl() && as_root(&["systemctl", "start", "--no-block", "wyze-tunnel-update"]).output().is_ok()
    }
    /// The web UI's update button touches WT_UPDATE_FILE; on Linux systemd watches it, here the tray does.
    pub fn update_requested() -> bool {
        fs::remove_file(data().join("update-now")).is_ok()
    }
    pub fn password() -> String {
        if wsl() {
            return super::stdout_of(as_root(&["sh", "-c", super::LINUX_PASSWORD]));
        }
        if data().join(r"tokens\wb_login").exists() {
            return String::new(); // a login chosen in the web UI replaces the generated one
        }
        settings().into_iter().find(|(k, _)| k == "WB_PASSWORD").map(|(_, v)| v).unwrap_or_default()
    }
    pub fn version() -> String {
        if wsl() {
            return super::stdout_of(as_root(super::LINUX_VERSION));
        }
        let env = fs::read_to_string(native().join(r"app\.env")).unwrap_or_default();
        env.lines().find_map(|l| l.strip_prefix("VERSION=")).unwrap_or_default().trim().to_string()
    }
    /// Someone is watching a stream (the bridge counts them next to WT_UPDATE_FILE); the daily update waits.
    /// In WSL mode the Linux updater does this itself.
    pub fn watching() -> bool {
        let n = fs::read_to_string(data().join("viewers")).unwrap_or_default();
        !wsl() && n.trim().parse::<u32>().is_ok_and(|n| n > 0)
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

/// Newest bridge release, like "2.13.0".
fn latest_bridge() -> Option<String> {
    let out = cmd("curl").args(["-fsSL", "https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases/latest"]).output().ok()?;
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    Some(json["tag_name"].as_str()?.trim_start_matches('v').to_string())
}

fn url() -> String {
    format!("http://localhost:{PORT}")
}

#[tauri::command]
fn info() -> serde_json::Value {
    serde_json::json!({ "installed": vm::installed(), "url": url() })
}

/// Generated by native/install.sh; empty once a login was chosen in the web UI.
const LINUX_PASSWORD: &str =
    "[ -f /var/lib/wyze-tunnel/tokens/wb_login ] || sed -n 's/^WB_PASSWORD=//p' /etc/wyze-tunnel/wyze-tunnel.env";

const LINUX_VERSION: &[&str] = &["sed", "-n", "s/^VERSION=//p", "/opt/wyze-tunnel/app/.env"];

fn stdout_of(mut c: Command) -> String {
    c.output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default()
}

/// The web UI's generated login password.
#[tauri::command]
async fn password() -> String {
    tauri::async_runtime::spawn_blocking(vm::password).await.unwrap_or_default()
}

/// The web UI opens in the browser: it uses HTTP basic auth, which macOS's webview can't prompt for.
#[tauri::command]
fn open_ui() {
    vm::open_url(&url());
}

/// Runs the platform installer (first setup or update), streaming its output as "log" events.
/// Returns the exit code; 3 means Windows needs a restart to finish turning on WSL, 4 that
/// virtualization is off in the BIOS. `full` picks the WSL setup on Windows.
#[tauri::command]
async fn install(app: AppHandle, full: bool) -> Result<i32, String> {
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || run_installer(&app, &res, full)).await.map_err(|e| e.to_string())?
}

fn run_installer(app: &AppHandle, res: &Path, full: bool) -> Result<i32, String> {
    if BUSY.swap(true, Ordering::SeqCst) {
        return Err("Setup is already running.".into());
    }
    let result = (|| {
        let mut child = vm::install_cmd(res, full).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
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
        if full {
            vm::stop(); // hand the ports over from the native bridge to the WSL one
        }
        STOPPED.store(false, Ordering::SeqCst);
        let _ = app.autolaunch().enable();
    }
    result
}

/// Runs the installer again for an update; true when it ran (it skips when nothing is newer).
fn reinstall(app: &AppHandle, full: bool) -> bool {
    app.path().resource_dir().is_ok_and(|res| run_installer(app, &res, full).is_ok())
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
        .plugin(tauri_plugin_notification::init())
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
            #[cfg(windows)]
            menu.insert(&item("full", "Full camera support (WSL)…")?, 4)?;
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
                        // A newer app goes to the releases page; the bridge update runs in the supervisor loop.
                        if let Some(tag) = newer_release() {
                            vm::open_url(&format!("{RELEASES}/tag/{tag}"));
                        } else {
                            UPDATE_NOW.store(true, Ordering::SeqCst);
                        }
                    }
                    "full" => {
                        open_window(app);
                        let _ = app.emit("full-setup", ());
                    }
                    "quit" => {
                        vm::quit();
                        app.exit(0)
                    }
                    _ => {}
                })
                .build(app)?;

            if !std::env::args().any(|a| a == "--hidden") {
                open_window(app.handle());
            }

            let handle = app.handle().clone();
            let mut last_update = Instant::now();
            // Update states, like the web UI's: available, updating, then a notification once the version changes.
            let (mut version, mut latest, mut desktop) = (String::new(), None::<String>, None::<String>);
            let mut updating: Option<Instant> = None;
            let mut shown = String::new();
            let mut tick = 0u32;
            std::thread::spawn(move || loop {
                let daily = last_update.elapsed() > Duration::from_secs(24 * 3600) && !vm::watching();
                if (UPDATE_NOW.swap(false, Ordering::SeqCst) || vm::update_requested() || daily) && vm::installed() {
                    last_update = Instant::now();
                    updating = Some(Instant::now());
                    let _ = update.set_text("Updating…");
                    let _ = update.set_enabled(false);
                    if !vm::self_update() {
                        reinstall(&handle, false); // native Windows: a no-op when nothing is newer
                    }
                }
                if tick.is_multiple_of(4) || updating.is_some() {
                    let v = vm::version();
                    if !v.is_empty() && v != version {
                        if !version.is_empty() {
                            let n = handle.notification().builder().title("Wyze Tunnel updated").body(format!("Now running v{v}."));
                            let _ = n.show();
                        }
                        version = v;
                        updating = None;
                    }
                    // A Linux update that found nothing newer (or rolled back) never changes the version.
                    if updating.is_some_and(|t| t.elapsed() > Duration::from_secs(300)) {
                        updating = None;
                    }
                }
                if tick.is_multiple_of(4 * 60 * 6) {
                    (latest, desktop) = (latest_bridge(), newer_release()); // every 6 hours
                }
                tick = tick.wrapping_add(1);
                let text = match (&updating, &desktop, &latest) {
                    (Some(_), ..) => "Updating…".to_string(),
                    (_, Some(tag), _) => format!("Update available ({tag})"),
                    (_, _, Some(l)) if !version.is_empty() && *l != version => format!("Update available (v{l})"),
                    _ => "Check for updates".to_string(),
                };
                if text != shown {
                    let _ = update.set_text(&text);
                    let _ = update.set_enabled(updating.is_none());
                    shown = text;
                }
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
