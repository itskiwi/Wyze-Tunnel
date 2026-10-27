// Tray / menu-bar app for Wyze Tunnel. The Wyze camera library only exists for Linux, so on macOS
// this sets up and supervises a Lima VM. On Windows the bridge runs natively and reaches cameras
// through go2rtc; a WSL2 distro with the Wyze library is the opt-in "full camera support".
// Setup is the repo's installer scripts. The cameras themselves are in the bridge's web UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
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
#[cfg(windows)]
static FIREWALL_BLOCKED: AtomicBool = AtomicBool::new(false); // shows the tray's firewall fix

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
    pub fn store_app() -> bool {
        false
    }
    pub fn open_url(url: &str) {
        let _ = cmd("open").arg(url).spawn();
    }
    /// The bridge's log and snapshots live in the VM, so the startup screen shows only its steps.
    pub fn data_dir() -> Option<PathBuf> {
        None
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
    // shortcut: in WSL mode these are a native bridge's leftovers; good enough for the startup screen.
    pub fn data_dir() -> Option<PathBuf> {
        Some(data())
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
            .arg(res.join(if full { "windows/install.ps1" } else { "windows/install-native.ps1" }))
            .env("WT_TRAY", "1"); // the tray asks for the firewall prompt itself (fix_firewall)
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
    /// A bridge an earlier tray left behind (an app update or restart kills the tray, not its children) would keep
    /// port 5000, go2rtc and the camera connections, and Windows lets a second server bind the same port.
    fn stop_strays() {
        let native = format!("{}\\*", dir().join("native").display()).replace('\'', "''");
        let script = format!("Get-Process | Where-Object {{ $_.Path -like '{native}' }} | Stop-Process -Force");
        let _ = cmd("powershell.exe").args(["-NoProfile", "-Command", &script]).output();
    }
    /// The same environment the Linux systemd unit sets up, with the settings file on top.
    fn start_native() -> Option<Child> {
        stop_strays();
        let (n, d) = (native(), data());
        for sub in ["tokens", "img", "record", "logs"] {
            fs::create_dir_all(d.join(sub)).ok()?;
        }
        fs::copy(n.join("mediamtx.yml"), d.join("mediamtx.yml")).ok()?; // fresh each start; the bridge edits it
        // Kept for the diagnostics report: after a crash the supervisor restarts the bridge, and the cause was in this log.
        let _ = fs::rename(d.join("bridge.log"), d.join("bridge.previous.log"));
        seal_password();
        pick_record_drive();
        let log = fs::File::create(d.join("bridge.log")).ok()?;
        let path = std::env::var("PATH").unwrap_or_default();
        // Tailscale only (Health page): Wyze Tunnel's own Tailscale reaches the web page from this PC, nothing else can.
        let host = if d.join(r"tokens\tailscale-only").exists() { "127.0.0.1" } else { "0.0.0.0" };
        let mut c = cmd(n.join(r"python\python.exe"));
        c.args(["-m", "flask", "run", &format!("--host={host}")])
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
            .map(|(k, v)| match v.strip_prefix(SEALED) {
                Some(hex) => (k, unseal(hex).unwrap_or_default()),
                None => (k, v),
            })
            .collect()
    }
    /// A new install records to the internal drive with the most room when this PC's own drive is short on it
    /// (a small C: next to a big D:). Once only: after the first clip, or a RECORD_PATH set by hand, it stays put.
    fn pick_record_drive() {
        use windows_sys::Win32::Storage::FileSystem::{GetDiskFreeSpaceExW, GetDriveTypeW, GetLogicalDrives};
        const DRIVE_FIXED: u32 = 3;
        const GB: u64 = 1 << 30;
        if settings().iter().any(|(k, _)| k == "RECORD_PATH") || fs::read_dir(data().join("record")).is_ok_and(|mut d| d.next().is_some()) {
            return;
        }
        let free = |root: &str| {
            let wide: Vec<u16> = root.encode_utf16().chain([0]).collect();
            let mut bytes = 0u64;
            let fixed = unsafe { GetDriveTypeW(wide.as_ptr()) } == DRIVE_FIXED;
            let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut bytes, std::ptr::null_mut(), std::ptr::null_mut()) } != 0;
            (fixed && ok).then_some(bytes)
        };
        let home = dir().to_string_lossy().chars().next().unwrap_or('C');
        let Some(home_free) = free(&format!("{home}:\\")) else { return };
        let mask = unsafe { GetLogicalDrives() };
        let best = (0..26u8)
            .filter(|i| mask & (1 << i) != 0)
            .map(|i| (b'A' + i) as char)
            .filter(|&c| !c.eq_ignore_ascii_case(&home))
            .filter_map(|c| Some((c, free(&format!("{c}:\\"))?)))
            .max_by_key(|&(_, f)| f);
        // Only worth moving when this drive is tight and the other has far more room.
        if let Some((drive, bytes)) = best.filter(|&(_, f)| home_free < 100 * GB && f >= 100 * GB && f > 2 * home_free) {
            let line = format!("RECORD_PATH={drive}:\\Wyze Tunnel recordings\\%path\\%Y-%m-%d_%H-%M-%S-%f");
            let note = format!("# Recordings go to {drive}: ({} GB free when Wyze Tunnel was set up). Change or remove this line to move them.", bytes / GB);
            let file = dir().join("wyze-tunnel.env");
            let text = fs::read_to_string(&file).unwrap_or_default();
            let _ = fs::write(&file, format!("{}\r\n{note}\r\n{line}\r\n", text.trim_end()));
        }
    }
    /// The web password is kept in wyze-tunnel.env sealed with DPAPI for this Windows user ("dpapi:" + hex), so a
    /// copied file is useless elsewhere. Install and hand edits write it plain; the next start seals it.
    const SEALED: &str = "dpapi:";
    fn seal_password() {
        let file = dir().join("wyze-tunnel.env");
        let Ok(text) = fs::read_to_string(&file) else { return };
        let mut changed = false;
        let lines: Vec<String> = text
            .lines()
            .map(|l| match l.trim().strip_prefix("WB_PASSWORD=").map(str::trim) {
                Some(v) if !v.is_empty() && !v.starts_with(SEALED) => match dpapi(v.as_bytes(), true) {
                    Some(blob) => {
                        changed = true;
                        format!("WB_PASSWORD={SEALED}{}", blob.iter().map(|b| format!("{b:02x}")).collect::<String>())
                    }
                    None => l.to_string(),
                },
                _ => l.to_string(),
            })
            .collect();
        if changed {
            let _ = fs::write(&file, lines.join("\r\n") + "\r\n");
        }
    }
    fn unseal(hex: &str) -> Option<String> {
        let bytes = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(hex.get(i..i + 2)?, 16).ok()).collect::<Option<Vec<u8>>>()?;
        String::from_utf8(dpapi(&bytes, false)?).ok()
    }
    fn dpapi(data: &[u8], seal: bool) -> Option<Vec<u8>> {
        use std::ptr::{null, null_mut};
        use windows_sys::Win32::Foundation::LocalFree;
        use windows_sys::Win32::Security::Cryptography::{CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB};
        let input = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        let ok = unsafe {
            if seal {
                CryptProtectData(&input, null(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            } else {
                CryptUnprotectData(&input, null_mut(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            }
        };
        if ok == 0 {
            return None;
        }
        let v = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec() };
        unsafe { LocalFree(out.pbData as _) };
        Some(v)
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
    /// Installed from the Microsoft Store (MSIX): the Store updates the app, and its startup task starts it at sign-in.
    pub fn store_app() -> bool {
        use windows_sys::Win32::Storage::Packaging::Appx::GetCurrentPackageFullName;
        let mut len = 0u32;
        unsafe { GetCurrentPackageFullName(&mut len, std::ptr::null_mut()) != 15700 } // APPMODEL_ERROR_NO_PACKAGE
    }
    /// The Store closes the app to update it; this has Windows start it again afterwards.
    pub fn restart_after_update() {
        use windows_sys::Win32::System::Recovery::RegisterApplicationRestart;
        let args: Vec<u16> = "--hidden".encode_utf16().chain([0]).collect();
        unsafe { RegisterApplicationRestart(args.as_ptr(), 0) };
    }
    /// Opens the default browser. explorer.exe garbles links with a query string and opens Documents instead.
    pub fn open_url(url: &str) {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
        let wide = |s: &str| std::ffi::OsStr::new(s).encode_wide().chain([0]).collect::<Vec<u16>>();
        let (verb, file) = (wide("open"), wide(url));
        unsafe { ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), file.as_ptr(), std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL) };
    }
    /// The bridge counts cameras that need the Wyze SDK (away from home, old firmware) next to WT_UPDATE_FILE.
    pub fn needs_sdk() -> bool {
        let n = fs::read_to_string(data().join("needs-sdk")).unwrap_or_default();
        n.trim().parse::<u32>().is_ok_and(|n| n > 0)
    }
    fn native_script(res: &Path, arg: &str) -> Command {
        let mut c = cmd("powershell.exe");
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]).arg(res.join("windows/install-native.ps1")).arg(arg);
        c
    }
    /// Native mode: a firewall rule for the bridge's programs is missing or blocking (read-only check).
    /// Mirrored to a "firewall-blocked" flag next to WT_UPDATE_FILE so the web UI can show a tip.
    pub fn firewall_blocked(res: &Path) -> bool {
        let blocked = !wsl()
            && native().join(r"python\python.exe").exists()
            && native_script(res, "-CheckFirewall").status().is_ok_and(|s| s.code() == Some(2));
        let flag = data().join("firewall-blocked");
        let _ = if blocked { fs::write(flag, "1") } else { fs::remove_file(flag) };
        blocked
    }
    /// Puts back the version the last update replaced; true when it did. Native mode only: Linux undoes its own.
    pub fn rollback(res: &Path) -> bool {
        if wsl() {
            return false;
        }
        stop();
        native_script(res, "-Rollback").status().is_ok_and(|s| s.success())
    }
    /// Checking for updates by hand tries a version that failed before again.
    pub fn forget_failed_update() {
        let _ = fs::remove_file(dir().join("failed-version"));
    }
    /// Adds the firewall rules behind one Windows permission prompt.
    /// Asked from this process, right after a click: a prompt asked from a windowless child opens
    /// behind other windows, flashes in the taskbar unseen and is cancelled after two minutes.
    pub fn fix_firewall(res: &Path) {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::System::Threading::{WaitForSingleObject, INFINITE};
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::UI::Shell::{ShellExecuteExW, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};
        use windows_sys::Win32::UI::WindowsAndMessaging::SW_HIDE;
        let wide = |s: &std::ffi::OsStr| s.encode_wide().chain([0]).collect::<Vec<u16>>();
        let script = res.join(r"windows\install-native.ps1");
        let args = format!("-NoProfile -ExecutionPolicy Bypass -File \"{}\" -FirewallOnly", script.display());
        let (verb, file, args) = (wide("runas".as_ref()), wide("powershell.exe".as_ref()), wide(args.as_ref()));
        let mut info: SHELLEXECUTEINFOW = unsafe { std::mem::zeroed() };
        info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS;
        info.lpVerb = verb.as_ptr();
        info.lpFile = file.as_ptr();
        info.lpParameters = args.as_ptr();
        info.nShow = SW_HIDE;
        // Declined or failed: the tray item stays, so the user can try again.
        unsafe {
            if ShellExecuteExW(&mut info) != 0 && !info.hProcess.is_null() {
                WaitForSingleObject(info.hProcess, INFINITE);
                CloseHandle(info.hProcess);
            }
        }
    }
}

/// Runs the firewall check in the background; skipped while an install replaces the programs.
#[cfg(windows)]
fn check_firewall(app: AppHandle) {
    std::thread::spawn(move || {
        if let (false, Ok(res)) = (BUSY.load(Ordering::SeqCst), app.path().resource_dir()) {
            FIREWALL_BLOCKED.store(vm::firewall_blocked(&res), Ordering::SeqCst);
        }
    });
}

/// The Store version starts from its package's startup task, which can't be turned on from here.
fn enable_autostart(app: &AppHandle) {
    if !vm::store_app() {
        let _ = app.autolaunch().enable();
    }
}

fn bridge_up() -> bool {
    TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], PORT)), Duration::from_secs(1)).is_ok()
}

/// Tag of the newest desktop release, if it differs from this build.
fn newer_release() -> Option<String> {
    if vm::store_app() {
        return None; // the Store updates it
    }
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

/// The startup screen's status: the end of the bridge's log, and the cameras' pictures from last time.
#[tauri::command]
async fn progress() -> serde_json::Value {
    tauri::async_runtime::spawn_blocking(|| {
        let dir = vm::data_dir();
        let log = dir.as_ref().and_then(|d| std::fs::read(d.join("bridge.log")).ok()).unwrap_or_default();
        let log = String::from_utf8_lossy(&log[log.len().saturating_sub(16_000)..]).into_owned();
        let mut snaps: Vec<_> = dir
            .and_then(|d| std::fs::read_dir(d.join("img")).ok())
            .into_iter()
            .flatten()
            .filter_map(|e| {
                let e = e.ok()?;
                let name = e.file_name().to_str()?.strip_suffix(".jpg")?.to_string();
                let age = e.metadata().ok()?.modified().ok()?.elapsed().unwrap_or_default().as_secs();
                snap_name_ok(&name).then_some((age, name))
            })
            .collect();
        snaps.sort();
        snaps.truncate(6);
        serde_json::json!({ "up": bridge_up(), "stopped": STOPPED.load(Ordering::SeqCst), "log": log, "snaps": snaps })
    })
    .await
    .unwrap_or_default()
}

/// Camera pictures are named after the camera's address, like "front-door"; anything else (event clips, paths) is left out.
fn snap_name_ok(name: &str) -> bool {
    !name.is_empty() && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
}

#[tauri::command]
fn snapshot(name: String) -> tauri::ipc::Response {
    let bytes = vm::data_dir()
        .filter(|_| snap_name_ok(&name))
        .and_then(|d| std::fs::read(d.join("img").join(format!("{name}.jpg"))).ok());
    tauri::ipc::Response::new(bytes.unwrap_or_default())
}

/// The startup screen's Start and Restart buttons.
#[tauri::command]
async fn restart() {
    STOPPED.store(false, Ordering::SeqCst);
    let _ = tauri::async_runtime::spawn_blocking(|| {
        vm::stop();
        vm::ensure_running();
    })
    .await;
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

#[tauri::command]
async fn open_ui(app: AppHandle) {
    open_web(&app);
}

/// macOS opens the browser: the web UI uses HTTP basic auth, which its webview can't prompt for.
#[cfg(not(windows))]
fn open_web(_: &AppHandle) {
    vm::open_url(&url());
}

/// Windows shows the web UI in its own window, signed in with the generated password.
#[cfg(windows)]
fn open_web(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("web") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let built = WebviewWindowBuilder::new(app, "web", WebviewUrl::External("about:blank".parse().unwrap()))
        .title("Wyze Tunnel")
        .inner_size(1200.0, 800.0)
        // Other websites and new-tab links go to the browser; the window stays on the web UI.
        .on_navigation(|u| {
            let web = matches!(u.scheme(), "http" | "https");
            if web && u.host_str() != Some("localhost") {
                vm::open_url(u.as_str());
            }
            !web || u.host_str() == Some("localhost")
        })
        .on_new_window(|u, _| {
            vm::open_url(u.as_str());
            tauri::webview::NewWindowResponse::Deny
        })
        .build();
    if let Ok(w) = built {
        sign_in(&w, vm::password(), url());
    }
}

/// Answers the web UI's login with the generated password, then loads it. The handler goes in before
/// the first request so WebView2's own sign-in box never flashes up.
#[cfg(windows)]
fn sign_in(w: &tauri::WebviewWindow, password: String, home: String) {
    use webview2_com::{BasicAuthenticationRequestedEventHandler, Microsoft::Web::WebView2::Win32::ICoreWebView2_10};
    use windows::core::{Interface, HSTRING};
    let _ = w.with_webview(move |wv| unsafe {
        let Ok(core) = wv.controller().CoreWebView2() else { return };
        // An empty password means a login chosen in the web UI: WebView2 asks for it instead.
        if let (false, Ok(core10)) = (password.is_empty(), core.cast::<ICoreWebView2_10>()) {
            let tried = AtomicBool::new(false);
            let handler = BasicAuthenticationRequestedEventHandler::create(Box::new(move |_, args| {
                // Once only, so a password that stopped working shows the sign-in box rather than looping.
                if let (Some(args), false) = (args, tried.swap(true, Ordering::SeqCst)) {
                    let r = args.Response()?;
                    r.SetUserName(&HSTRING::from("wbadmin"))?;
                    r.SetPassword(&HSTRING::from(password.as_str()))?;
                }
                Ok(())
            }));
            let mut token = 0;
            let _ = core10.add_BasicAuthenticationRequested(&handler, &mut token);
        }
        let _ = core.Navigate(&HSTRING::from(home));
    });
}

/// Runs the platform installer (first setup or update), streaming its output as "log" events.
/// Returns the exit code; 3 means Windows needs a restart to finish turning on WSL, 4 that
/// virtualization is off in the BIOS. `full` picks the WSL setup on Windows.
#[tauri::command]
async fn install(app: AppHandle, full: bool) -> Result<i32, String> {
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || {
        let code = run_installer(&app, &res, full);
        // Set up was clicked in the window, so the prompt can come up in front now.
        #[cfg(windows)]
        if code == Ok(0) && !full && vm::firewall_blocked(&res) {
            vm::fix_firewall(&res);
            check_firewall(app);
        }
        code
    })
    .await
    .map_err(|e| e.to_string())?
}

fn run_installer(app: &AppHandle, res: &Path, full: bool) -> Result<i32, String> {
    if BUSY.swap(true, Ordering::SeqCst) {
        return Err("Setup is already running.".into());
    }
    let result = (|| {
        let mut child = vm::install_cmd(res, full).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
        let stderr = child.stderr.take().unwrap();
        // Also kept in a file: an update from the tray has no window, so its output (a skipped firewall prompt, say) was lost.
        let file = std::fs::File::create(std::env::temp_dir().join("wyze-tunnel-install.log")).map_err(|e| e.to_string())?;
        let (app2, mut file2) = (app.clone(), file.try_clone().map_err(|e| e.to_string())?);
        let errs = std::thread::spawn(move || {
            BufReader::new(stderr).lines().map_while(Result::ok).for_each(|l| {
                let _ = writeln!(file2, "{l}");
                let _ = app2.emit("log", l);
            })
        });
        let mut file = file;
        BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok).for_each(|l| {
            let _ = writeln!(file, "{l}");
            let _ = app.emit("log", l);
        });
        let _ = errs.join();
        let code = child.wait().map(|s| s.code().unwrap_or(1)).map_err(|e| e.to_string());
        let _ = writeln!(file, "exit code: {code:?}");
        code
    })();
    BUSY.store(false, Ordering::SeqCst);
    if result == Ok(0) {
        if full {
            vm::stop(); // hand the ports over from the native bridge to the WSL one
        }
        STOPPED.store(false, Ordering::SeqCst);
        enable_autostart(app);
        #[cfg(windows)]
        check_firewall(app.clone());
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
        .invoke_handler(tauri::generate_handler![info, install, password, open_ui, progress, snapshot, restart])
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
            // The web UI opens in the app on Windows; this keeps a browser tab one click away.
            #[cfg(windows)]
            menu.insert(&item("browser", "Open in browser")?, 1)?;
            // Shown only while a camera needs it; everything else runs natively.
            #[cfg(windows)]
            let (full, mut full_shown) = (item("full", "Full camera support (WSL)…")?, false);
            // Shown only while Windows Firewall blocks phones or cameras.
            #[cfg(windows)]
            let (firewall, mut firewall_shown) = (item("firewall", "Fix phone and camera access")?, false);
            let tray = TrayIconBuilder::with_id("tray")
                .icon(tauri::include_image!("icons/tray.png")) // bare mark: the app tile is mush at 16px
                .tooltip("Wyze Tunnel")
                .menu(&menu)
                .on_menu_event(|app, e| match e.id.as_ref() {
                    "open" if bridge_up() => open_web(app),
                    "browser" => vm::open_url(&url()),
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
                    #[cfg(windows)]
                    "firewall" => {
                        let app = app.clone();
                        std::thread::spawn(move || {
                            if let Ok(res) = app.path().resource_dir() {
                                vm::fix_firewall(&res);
                            }
                            check_firewall(app);
                        });
                    }
                    "quit" => {
                        vm::quit();
                        app.exit(0)
                    }
                    _ => {}
                })
                .build(app)?;

            #[cfg(windows)]
            if vm::store_app() {
                vm::restart_after_update();
            }
            if !std::env::args().any(|a| a == "--hidden") {
                open_window(app.handle());
            }

            let handle = app.handle().clone();
            let mut last_update = Instant::now();
            // Update states, like the web UI's: available, updating, then a notification once the version changes.
            let (mut version, mut latest, mut desktop) = (String::new(), None::<String>, None::<String>);
            let mut updating: Option<Instant> = None;
            // Set when an update changed the version: the new bridge must answer within 3 minutes or it is undone.
            #[cfg(windows)]
            let mut verify: Option<Instant> = None;
            let mut shown = String::new();
            let mut tick = 0u32;
            std::thread::spawn(move || loop {
                let daily = last_update.elapsed() > Duration::from_secs(24 * 3600) && !vm::watching();
                let manual = UPDATE_NOW.swap(false, Ordering::SeqCst) || vm::update_requested();
                if (manual || daily) && vm::installed() {
                    #[cfg(windows)]
                    if manual {
                        vm::forget_failed_update();
                    }
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
                            #[cfg(windows)]
                            {
                                verify = Some(Instant::now());
                            }
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
                    if vm::installed() {
                        // No off switch in the app, so re-assert it in case a cleanup or update dropped it.
                        enable_autostart(&handle);
                    }
                    #[cfg(windows)]
                    check_firewall(handle.clone());
                }
                #[cfg(windows)]
                if tick.is_multiple_of(4) && vm::needs_sdk() != full_shown {
                    full_shown = !full_shown;
                    let _ = if full_shown { menu.insert(&full, 5) } else { menu.remove(&full) };
                }
                #[cfg(windows)]
                if FIREWALL_BLOCKED.load(Ordering::SeqCst) != firewall_shown {
                    firewall_shown = !firewall_shown;
                    let _ = if firewall_shown { menu.insert(&firewall, 5) } else { menu.remove(&firewall) };
                }
                #[cfg(windows)]
                if let Some(t) = verify {
                    if bridge_up() || STOPPED.load(Ordering::SeqCst) {
                        verify = None;
                    } else if t.elapsed() > Duration::from_secs(180) && !BUSY.load(Ordering::SeqCst) {
                        verify = None;
                        if handle.path().resource_dir().is_ok_and(|res| vm::rollback(&res)) {
                            version = vm::version(); // not announced as an update
                            let body = "The new version didn't start, so Wyze Tunnel went back to the one before. It will skip that update.";
                            let _ = handle.notification().builder().title("Update undone").body(body).show();
                        }
                    }
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
