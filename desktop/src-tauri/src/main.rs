// Modwen desktop app: a small shell around the local Modwen engine (Python), which it starts, watches and stops.
//
// Bundled inside the app: `uv`, the engine's source (modwen/, uv.lock, the built web UI, the macOS audio helper).
// First start: `uv` installs Modwen's private Python and packages into the data folder (never into the app, which
// stays read-only and signed). Every start: the engine runs on a free 127.0.0.1 port with a random token, and the
// main window shows its UI. Closing the window keeps Modwen in the menu bar; Quit stops the engine safely.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::hash_map::{DefaultHasher, RandomState};
use std::fs::{self, OpenOptions};
use std::hash::{BuildHasher, Hash, Hasher};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::image::Image;
use tauri::menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};

const READY_TIMEOUT: Duration = Duration::from_secs(180);

#[derive(Clone, Serialize, Default)]
struct EngineState {
    phase: String, // "setup" | "starting" | "ready" | "error"
    message: String,
    first_run: bool,
    url: Option<String>,
}

#[derive(Default)]
struct Engine {
    child: Mutex<Option<Child>>,
    state: Mutex<EngineState>,
    conn: Mutex<Option<(u16, String)>>, // port and token of the running engine
    update: Mutex<Option<UpdateInfo>>,   // a newer version found on GitHub Releases
}

#[derive(Clone, Serialize)]
struct UpdateInfo {
    version: String,
    notes: String,
}

struct Paths {
    engine: PathBuf, // bundled source (read-only)
    uv: PathBuf,
    runtime: PathBuf, // Modwen's private Python, packages, caches
    logs: PathBuf,
}

fn paths(app: &AppHandle) -> Result<Paths, String> {
    let res = app.path().resource_dir().map_err(|e| e.to_string())?;
    // Same folder the engine uses for meetings (platformdirs user_data_dir("Modwen")), or the engine's own
    // MODWEN_DATA_DIR override (tests), so a test run never touches the real data folder.
    let data = match std::env::var_os("MODWEN_DATA_DIR") {
        Some(d) => PathBuf::from(d),
        None => app.path().local_data_dir().map_err(|e| e.to_string())?.join("Modwen"),
    };
    let uv = res.join("bin").join(if cfg!(windows) { "uv.exe" } else { "uv" });
    Ok(Paths { engine: res.join("engine"), uv, runtime: data.join("runtime"), logs: data.join("logs") })
}

fn set_state(app: &AppHandle, phase: &str, message: &str, first_run: bool, url: Option<String>) {
    let st = EngineState { phase: phase.into(), message: message.into(), first_run, url };
    if let Some(engine) = app.try_state::<Engine>() {
        *engine.state.lock().unwrap() = st.clone();
    }
    let _ = app.emit("engine-state", st);
}

fn log_line(app: &AppHandle, logs: &Path, line: &str) {
    let _ = fs::create_dir_all(logs);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(logs.join("app-engine.log")) {
        let _ = writeln!(f, "{line}");
    }
    let _ = app.emit("engine-log", line.to_string());
}

fn random_token() -> String {
    // 128 bits from the OS's random hash keys (no extra dependency).
    (0..2)
        .map(|i| {
            let mut h = RandomState::new().build_hasher();
            h.write_u64(i);
            format!("{:016x}", h.finish())
        })
        .collect()
}

fn free_port() -> Result<u16, String> {
    let l = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    Ok(l.local_addr().map_err(|e| e.to_string())?.port())
}

fn python(runtime: &Path) -> PathBuf {
    if cfg!(windows) {
        runtime.join("venv").join("Scripts").join("python.exe")
    } else {
        runtime.join("venv").join("bin").join("python3")
    }
}

/// Windows: start console programs (uv, Python) without a console window popping up.
fn no_console(c: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    #[cfg(not(windows))]
    let _ = c;
}

fn uv_command(p: &Paths) -> Command {
    let mut c = Command::new(&p.uv);
    no_console(&mut c);
    c.env("UV_PROJECT_ENVIRONMENT", p.runtime.join("venv"))
        .env("UV_CACHE_DIR", p.runtime.join("uv-cache"))
        .env("UV_PYTHON_INSTALL_DIR", p.runtime.join("python"))
        .env("UV_PYTHON_PREFERENCE", "only-managed") // never the system's Python
        .env("UV_NO_CONFIG", "1")
        .env_remove("VIRTUAL_ENV")
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME");
    c
}

/// What the installed environment was built from; a new app version or lockfile reinstalls it.
fn env_stamp(p: &Paths, version: &str) -> String {
    let mut h = DefaultHasher::new();
    version.hash(&mut h);
    fs::read(p.engine.join("uv.lock")).unwrap_or_default().hash(&mut h);
    format!("{:016x}", h.finish())
}

/// Run a command, forwarding its output lines to the log and the splash screen. Ok(()) when it exits with 0.
fn run_logged(app: &AppHandle, logs: &Path, mut cmd: Command) -> Result<(), String> {
    let mut child = cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    let (a, b) = (app.clone(), logs.to_path_buf());
    let t = thread::spawn(move || {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            log_line(&a, &b, &line);
        }
    });
    for line in BufReader::new(err).lines().map_while(Result::ok) {
        log_line(app, logs, &line);
    }
    let _ = t.join();
    let status = child.wait().map_err(|e| e.to_string())?;
    if status.success() { Ok(()) } else { Err(format!("exited with {status}")) }
}

fn ensure_environment(app: &AppHandle, p: &Paths) -> Result<(), String> {
    let version = app.package_info().version.to_string();
    let stamp_file = p.runtime.join("venv-stamp");
    let stamp = env_stamp(p, &version);
    if python(&p.runtime).exists() && fs::read_to_string(&stamp_file).ok().as_deref() == Some(stamp.as_str()) {
        return Ok(());
    }
    let first = !python(&p.runtime).exists();
    set_state(app, "setup", if first { "Setting up Modwen for the first time…" } else { "Updating Modwen's environment…" }, first, None);
    fs::create_dir_all(&p.runtime).map_err(|e| e.to_string())?;
    let mut cmd = uv_command(p);
    cmd.arg("sync").arg("--frozen").arg("--no-dev").arg("--no-install-project")
        .arg("--project").arg(&p.engine)
        .args(["--extra", "stt", "--extra", "desktop"]);
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        cmd.args(["--extra", "mac"]);
    }
    if cfg!(windows) {
        cmd.args(["--extra", "windows"]);
    }
    run_logged(app, &p.logs, cmd).map_err(|e| format!("Setting up Modwen's Python failed ({e}). Check the internet connection and try again."))?;
    fs::write(&stamp_file, stamp).map_err(|e| e.to_string())?;
    Ok(())
}

fn answers_http(port: u16) -> bool {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    if s.write_all(format!("GET / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 12];
    matches!(s.read(&mut buf), Ok(n) if n >= 8 && buf.starts_with(b"HTTP/1."))
}

/// macOS: Modwen's encryption key from the Keychain, read by the app (one prompt, in Modwen's name, that
/// "Always Allow" keeps across updates) instead of by the engine's Python (a prompt per program, again after an
/// update). Only an existing key is read: the app never creates one, the engine does (it knows where keys live).
#[cfg(target_os = "macos")]
fn keychain_secrets() -> Option<String> {
    use security_framework::passwords::get_generic_password;
    if std::env::var("MODWEN_KEYSTORE").as_deref() == Ok("file") {
        return None; // tests and file-key setups: the engine reads its own file
    }
    for service in ["modwen", "blue-bird"] { // "blue-bird": the name before 0.2 (the engine migrates it)
        if let Ok(bytes) = get_generic_password(service, "master-key") {
            let key = String::from_utf8(bytes).ok()?;
            if !key.is_empty() && key.chars().all(|c| c.is_ascii_alphanumeric() || "+/=".contains(c)) {
                return Some(format!("{{\"master-key\": \"{key}\"}}"));
            }
        }
    }
    None
}

#[cfg(not(target_os = "macos"))]
fn keychain_secrets() -> Option<String> {
    None // Windows' Credential Manager doesn't prompt per program: the engine reads it directly
}

fn start_engine(app: &AppHandle) -> Result<String, String> {
    let p = paths(app)?;
    if !p.uv.exists() || !p.engine.join("modwen").exists() {
        return Err(format!("This copy of Modwen is incomplete (missing {}).", p.engine.display()));
    }
    ensure_environment(app, &p)?;
    set_state(app, "starting", "Starting Modwen…", false, None);
    let port = free_port()?;
    let token = random_token();
    let mut cmd = Command::new(python(&p.runtime));
    no_console(&mut cmd);
    cmd.args(["-m", "modwen", "serve", "--no-browser", "--no-tray", "--port", &port.to_string()])
        .current_dir(&p.engine) // the engine runs from the bundled source, not installed
        .env("MODWEN_TOKEN", &token)
        .env("MODWEN_PARENT_PID", std::process::id().to_string())
        .env("MODWEN_DESKTOP", "1")
        .env("PYTHONPYCACHEPREFIX", p.runtime.join("pycache")) // never write inside the signed app
        .env("PYTHONUNBUFFERED", "1")
        // Modwen isn't installed into its environment (the app stays read-only), so every Python it starts
        // (the AI-model watchdog, worker processes) finds it here, whatever their working folder.
        .env("PYTHONPATH", &p.engine)
        .env_remove("PYTHONHOME")
        .env_remove("VIRTUAL_ENV")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let secrets = keychain_secrets();
    if secrets.is_some() {
        cmd.env("MODWEN_SECRETS_ON_STDIN", "1").stdin(Stdio::piped()); // never in the environment or a file
    } else {
        cmd.stdin(Stdio::null());
    }
    let mut child = cmd.spawn().map_err(|e| format!("Could not start the engine: {e}"))?;
    if let (Some(s), Some(mut stdin)) = (secrets, child.stdin.take()) {
        let _ = writeln!(stdin, "{s}"); // one line, then the pipe closes (stdin dropped here)
    }
    for pipe in [child.stdout.take().map(|o| Box::new(o) as Box<dyn Read + Send>),
                 child.stderr.take().map(|e| Box::new(e) as Box<dyn Read + Send>)].into_iter().flatten() {
        let (a, logs) = (app.clone(), p.logs.clone());
        thread::spawn(move || {
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                log_line(&a, &logs, &line);
            }
        });
    }
    let engine = app.state::<Engine>();
    *engine.child.lock().unwrap() = Some(child);
    let t0 = Instant::now();
    loop {
        if answers_http(port) {
            break;
        }
        if let Some(c) = engine.child.lock().unwrap().as_mut() {
            if let Ok(Some(status)) = c.try_wait() {
                return Err(format!("The engine stopped while starting ({status}). Details are below and in logs/app-engine.log."));
            }
        }
        if t0.elapsed() > READY_TIMEOUT {
            return Err("The engine did not answer within 3 minutes.".into());
        }
        thread::sleep(Duration::from_millis(300));
    }
    *engine.conn.lock().unwrap() = Some((port, token.clone()));
    Ok(format!("http://127.0.0.1:{port}/?token={token}"))
}

fn boot(app: AppHandle) {
    thread::spawn(move || match start_engine(&app) {
        Ok(url) => {
            set_state(&app, "ready", "Ready", false, Some(url.clone()));
            if let (Some(win), Ok(u)) = (app.get_webview_window("main"), url.parse::<Url>()) {
                let _ = win.navigate(u);
            }
            if std::env::var_os("MODWEN_APP_SELFTEST").is_some() {
                self_test(&app);
            }
        }
        Err(e) => {
            stop_engine(&app);
            set_state(&app, "error", &e, false, None);
        }
    });
}

/// Ask the engine to stop (it saves the last audio of a recording first), then make sure it's gone.
fn stop_engine(app: &AppHandle) {
    let Some(engine) = app.try_state::<Engine>() else { return };
    let asked = engine_api(app, "POST", "/api/app/quit").is_some(); // works on Windows too (no SIGTERM there)
    *engine.conn.lock().unwrap() = None;
    let Some(mut child) = engine.child.lock().unwrap().take() else { return };
    let exited = |child: &mut Child, secs: u64| {
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(secs) {
            if let Ok(Some(_)) = child.try_wait() {
                return true;
            }
            thread::sleep(Duration::from_millis(100));
        }
        false
    };
    if asked && exited(&mut child, 15) {
        return;
    }
    #[cfg(unix)]
    {
        let _ = Command::new("kill").arg("-TERM").arg(child.id().to_string()).status();
        if exited(&mut child, 10) {
            return;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// MODWEN_APP_SELFTEST: check from inside the main window that the UI reaches the app (bridge) and that the
/// pop-out opens a real window; results go to logs/app-engine.log as "SELFTEST …" lines.
fn self_test(app: &AppHandle) {
    let app = app.clone();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(4));
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.eval(r#"(function () {
              const t = window.__TAURI__;
              const where = location.href.split("?")[0];
              const i = window.__TAURI_INTERNALS__;
              window.open(location.origin + "/#/selftest/TAURI=" + typeof t + "/INTERNALS=" + typeof i, "st");
              if (t) t.core.invoke("selftest", { report: "bridge ok on " + where })
                .catch((e) => window.open(location.origin + "/#/selftest/invoke-error=" + encodeURIComponent(String(e)), "st2"));
              window.open(location.origin + "/#/pill", "modwen-pill");
            })();"#);
        }
        thread::sleep(Duration::from_secs(4));
        if let Ok(p) = paths(&app) {
            let pill = app.get_webview_window("pill").is_some();
            log_line(&app, &p.logs, &format!("SELFTEST pill window {}", if pill { "present" } else { "MISSING" }));
            if let Some(pw) = app.get_webview_window("pill") {  // the pill's own close button goes through the bridge
                let _ = pw.eval(r#"window.__TAURI__.core.invoke("close_pill")"#);
                thread::sleep(Duration::from_secs(2));
                let gone = app.get_webview_window("pill").is_none();
                log_line(&app, &p.logs, &format!("SELFTEST pill closes itself {}", if gone { "ok" } else { "FAILED" }));
            }
            let tray = engine_api(&app, "GET", "/api/tray").is_some();
            log_line(&app, &p.logs, &format!("SELFTEST tray endpoint {}", if tray { "ok" } else { "FAILED" }));
            if std::env::var("MODWEN_APP_SELFTEST").as_deref() == Ok("update") {
                let v = app.package_info().version.to_string();
                log_line(&app, &p.logs, &format!("SELFTEST running version {v}"));
                match check_for_update(&app) {
                    Ok(Some(u)) => {
                        log_line(&app, &p.logs, &format!("SELFTEST update found {}", u.version));
                        if let Err(e) = install_update_now(&app) {
                            log_line(&app, &p.logs, &format!("SELFTEST update install FAILED: {e}"));
                        }
                    }
                    Ok(None) => log_line(&app, &p.logs, "SELFTEST up to date"),
                    Err(e) => log_line(&app, &p.logs, &format!("SELFTEST update check FAILED: {e}")),
                }
            }
        }
    });
}

fn show_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

// ----- commands the splash screen and the UI call -----

#[tauri::command]
fn engine_state(engine: State<Engine>) -> EngineState {
    engine.state.lock().unwrap().clone()
}

#[tauri::command]
fn restart_engine(app: AppHandle) {
    stop_engine(&app);
    boot(app);
}

#[tauri::command]
fn open_pill(app: AppHandle) -> Result<(), String> {
    open_pill_window(&app)
}

#[tauri::command]
fn close_pill(app: AppHandle) {
    if let Some(w) = app.get_webview_window("pill") {
        let _ = w.close();
    }
}

#[tauri::command]
fn show_main(app: AppHandle, hash: String) {
    go_to(&app, &hash);
}

/// The test run (MODWEN_APP_SELFTEST) reports what the UI can reach.
#[tauri::command]
fn selftest(app: AppHandle, report: String) {
    if let Ok(p) = paths(&app) {
        log_line(&app, &p.logs, &format!("SELFTEST {report}"));
    }
}

const PILL_W: f64 = 470.0;
const PILL_H: f64 = 150.0;

fn open_pill_window(app: &AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("pill") {
        let _ = w.show();
        return w.set_focus().map_err(|e| e.to_string());
    }
    let base = app.state::<Engine>().state.lock().unwrap().url.clone().ok_or("Modwen is still starting")?;
    let mut url: Url = base.parse().map_err(|e| format!("{e}"))?;
    url.set_fragment(Some("/pill"));
    let handle = app.clone();
    WebviewWindowBuilder::new(app, "pill", WebviewUrl::External(url))
        .title("Modwen")
        .inner_size(PILL_W, PILL_H)
        .min_inner_size(300.0, 64.0)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .on_new_window(move |u, _| handle_new_window(&handle, u))
        .build()
        .map(|_| {
            if let Ok(p) = paths(app) {
                log_line(app, &p.logs, "pill window opened");
            }
        })
        .map_err(|e| e.to_string())
}

/// Show the main window on a page of the UI ("#/meetings/…").
fn go_to(app: &AppHandle, hash: &str) {
    show_main_window(app);
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(&format!("location.hash = {hash:?}"));
    }
}

/// The UI's window.open: the pop-out pill becomes a real always-on-top window, another Modwen page opens in the
/// main window, and any other link opens in the default browser (never inside the app).
fn handle_new_window(app: &AppHandle, url: Url) -> NewWindowResponse<Wry> {
    let app = app.clone();
    thread::spawn(move || {
        let local = url.host_str() == Some("127.0.0.1");
        match url.fragment() {
            Some(f) if local && f.starts_with("/selftest") => {
                if let Ok(p) = paths(&app) {
                    log_line(&app, &p.logs, &format!("SELFTEST page says {f}"));
                }
            }
            Some(f) if local && f.starts_with("/pill") => {
                let _ = open_pill_window(&app);
            }
            Some(f) if local => go_to(&app, &format!("#{f}")),
            _ if local => show_main_window(&app),
            _ if matches!(url.scheme(), "http" | "https" | "mailto") => open_in_browser(url.as_str()),
            // Settings → Notifications: "Open macOS notification settings" (only that pane)
            _ if url.as_str().starts_with("x-apple.systempreferences:com.apple.Notifications-Settings") => open_in_browser(url.as_str()),
            _ => {}
        }
    });
    NewWindowResponse::Deny
}

fn open_in_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let _ = Command::new("open").arg(url).spawn();
    #[cfg(windows)]
    let _ = Command::new("cmd").args(["/C", "start", "", url]).spawn();
}

// ----- updates (docs/planning/04-desktop-updates.md) -----

/// The updater, reading latest.json from GitHub Releases (tauri.conf.json), or from MODWEN_UPDATE_ENDPOINT when
/// set: a local test server, to try a whole update before publishing it.
fn updater(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    use tauri_plugin_updater::UpdaterExt;
    let mut b = app.updater_builder();
    if let Ok(e) = std::env::var("MODWEN_UPDATE_ENDPOINT") {
        let url: Url = e.parse().map_err(|e| format!("{e}"))?;
        b = b.endpoints(vec![url]).map_err(|e| e.to_string())?;
    }
    b.build().map_err(|e| e.to_string())
}

/// Look for a newer version on GitHub Releases; remember it and tell the UI. The request carries nothing from
/// meetings: it's a plain download of latest.json.
fn check_for_update(app: &AppHandle) -> Result<Option<UpdateInfo>, String> {
    let found = tauri::async_runtime::block_on(async { updater(app)?.check().await.map_err(|e| e.to_string()) })?;
    let info = found.map(|u| UpdateInfo { version: u.version.clone(), notes: u.body.clone().unwrap_or_default() });
    *app.state::<Engine>().update.lock().unwrap() = info.clone();
    if let Some(i) = &info {
        let _ = app.emit("update-available", i.clone());
    }
    Ok(info)
}

/// Download, verify (our updater key) and install, then restart. Never while recording or writing notes.
fn install_update_now(app: &AppHandle) -> Result<(), String> {
    if let Some(info) = engine_api(app, "GET", "/api/tray").and_then(|b| serde_json::from_str::<TrayInfo>(&b).ok()) {
        if info.recording || info.processing > 0 {
            return Err("Modwen is recording or writing notes. Install the update when it has finished.".into());
        }
    }
    tauri::async_runtime::block_on(async {
        let update = updater(app)?.check().await.map_err(|e| e.to_string())?.ok_or("Modwen is up to date")?;
        update.download_and_install(|_, _| {}, || {}).await.map_err(|e| e.to_string())
    })?;
    stop_engine(app);
    app.restart();
}

/// At start (once the engine runs) and every 6 hours, if Settings → Updates allows it.
fn update_checker(app: AppHandle) {
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(30));
        loop {
            let allowed = engine_api(&app, "GET", "/api/tray")
                .and_then(|b| serde_json::from_str::<TrayInfo>(&b).ok())
                .map(|i| i.check_updates);
            match allowed {
                Some(true) => {
                    if let Err(e) = check_for_update(&app) {
                        if let Ok(p) = paths(&app) {
                            log_line(&app, &p.logs, &format!("update check failed: {e}"));
                        }
                    }
                }
                Some(false) => {}
                None => {
                    thread::sleep(Duration::from_secs(60)); // engine not up yet
                    continue;
                }
            }
            thread::sleep(Duration::from_secs(6 * 3600));
        }
    });
}

#[tauri::command]
fn update_info(engine: State<Engine>) -> Option<UpdateInfo> {
    engine.update.lock().unwrap().clone()
}

#[tauri::command]
async fn check_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    tauri::async_runtime::spawn_blocking(move || check_for_update(&app)).await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || install_update_now(&app)).await.map_err(|e| e.to_string())?
}

// ----- the menu-bar menu, fed by the engine's /api/tray -----

#[derive(Deserialize, Default, Clone, PartialEq)]
struct ReadyMeeting {
    id: String,
    title: String,
}

#[derive(Deserialize, Default, Clone, PartialEq)]
struct LlmInfo {
    active: String,
    online: bool,
}

#[derive(Deserialize, Default, Clone, PartialEq)]
struct AppNotification {
    title: String,
    body: String,
}

#[derive(Deserialize, Default, Clone, PartialEq)]
struct TrayInfo {
    recording: bool,
    paused: bool,
    elapsed_ms: u64,
    meeting_id: Option<String>,
    title: Option<String>,
    processing: u32,
    ready: Vec<ReadyMeeting>,
    llm: LlmInfo,
    #[serde(default)]
    notifications: Vec<AppNotification>,
    #[serde(default)]
    check_updates: bool,
}

impl TrayInfo {
    /// What the menu shows, without the ticking timer (which goes next to the icon instead, so an open menu
    /// isn't rebuilt every second).
    fn shape(&self) -> TrayInfo {
        TrayInfo { elapsed_ms: 0, notifications: Vec::new(), check_updates: false, ..self.clone() }
    }
}

/// A small HTTP/1.0 request to the engine with the session token. Returns the body of a 2xx response.
fn engine_api(app: &AppHandle, method: &str, path: &str) -> Option<String> {
    let (port, token) = app.state::<Engine>().conn.lock().unwrap().clone()?;
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_secs(1)).ok()?;
    let _ = s.set_read_timeout(Some(Duration::from_secs(15)));
    let body = if method == "GET" { "" } else { "{}" };
    write!(s, "{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nx-modwen-token: {token}\r\n\
               Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len()).ok()?;
    let mut resp = String::new();
    s.read_to_string(&mut resp).ok()?;
    let (head, body) = resp.split_once("\r\n\r\n")?;
    let code: u16 = head.split_whitespace().nth(1)?.parse().ok()?;
    (200..300).contains(&code).then(|| body.to_string())
}

fn fmt_elapsed(ms: u64) -> String {
    let s = ms / 1000;
    if s >= 3600 { format!("{}:{:02}:{:02}", s / 3600, s % 3600 / 60, s % 60) } else { format!("{:02}:{:02}", s / 60, s % 60) }
}

fn build_menu(app: &AppHandle, info: &TrayInfo, ready: bool) -> tauri::Result<Menu<Wry>> {
    let item = |id: &str, text: &str, enabled: bool| MenuItem::with_id(app, id, text, enabled, None::<&str>);
    let sep = || PredefinedMenuItem::separator(app);
    let mut items: Vec<Box<dyn IsMenuItem<Wry>>> = Vec::new();
    let header = if !ready {
        "Modwen · starting…".to_string()
    } else if info.recording {
        format!("{} · {}", if info.paused { "Paused" } else { "Recording" }, info.title.clone().unwrap_or_default())
    } else if info.processing > 0 {
        format!("Modwen · writing notes for {} meeting{}", info.processing, if info.processing == 1 { "" } else { "s" })
    } else {
        "Modwen · ready".to_string()
    };
    items.push(Box::new(item("header", &header, false)?));
    items.push(Box::new(sep()?));
    if ready && info.recording {
        items.push(Box::new(item("stop", "Stop and write notes", true)?));
        items.push(Box::new(item(if info.paused { "resume" } else { "pause" }, if info.paused { "Resume" } else { "Pause" }, true)?));
        items.push(Box::new(item("mark", "Mark this moment", true)?));
        items.push(Box::new(sep()?));
        items.push(Box::new(item("live", "Live captions", true)?));
        items.push(Box::new(item("pill", "Pop out recording controls", true)?));
    } else if ready {
        items.push(Box::new(item("start", "Record a meeting…", true)?));
        items.push(Box::new(item("start_room", "Record a meeting in the room…", true)?));
    }
    if ready && !info.ready.is_empty() {
        items.push(Box::new(sep()?));
        let notes: Vec<MenuItem<Wry>> = info.ready.iter()
            .map(|m| MenuItem::with_id(app, format!("meeting:{}", m.id), &m.title, true, None::<&str>))
            .collect::<tauri::Result<_>>()?;
        let refs: Vec<&dyn IsMenuItem<Wry>> = notes.iter().map(|m| m as &dyn IsMenuItem<Wry>).collect();
        items.push(Box::new(Submenu::with_items(app, format!("Notes ready ({})", info.ready.len()), true, &refs)?));
    }
    items.push(Box::new(sep()?));
    if let Some(u) = app.state::<Engine>().update.lock().unwrap().as_ref() {
        items.push(Box::new(item("update", &format!("Install Modwen {}…", u.version), true)?));
    }
    items.push(Box::new(item("open", "Open Modwen", true)?));
    if ready {
        items.push(Box::new(item("settings", "Settings…", true)?));
        let llm = format!("AI model: {} · {}", info.llm.active, if info.llm.online { "online" } else { "offline" });
        items.push(Box::new(item("llm", &llm, false)?));
    }
    items.push(Box::new(item("local", "Local only: nothing leaves this computer", false)?));
    items.push(Box::new(sep()?));
    items.push(Box::new(item("quit", "Quit Modwen", true)?));
    let refs: Vec<&dyn IsMenuItem<Wry>> = items.iter().map(|b| b.as_ref()).collect();
    Menu::with_items(app, &refs)
}

fn tray_icon_for(info: &TrayInfo) -> (Image<'static>, bool) {
    let (bytes, template): (&'static [u8], bool) = if info.recording {
        (include_bytes!("../icons/tray-recording.png"), false)
    } else if info.processing > 0 {
        (include_bytes!("../icons/tray-processing.png"), false)
    } else if !info.ready.is_empty() {
        (include_bytes!("../icons/tray-ready.png"), false)
    } else {
        (include_bytes!("../icons/tray.png"), true) // macOS tints a template icon for light / dark menu bars
    };
    (Image::from_bytes(bytes).expect("bundled tray icon"), template)
}

fn on_tray_menu(app: &AppHandle, id: &str) {
    let app = app.clone();
    let id = id.to_string();
    thread::spawn(move || match id.as_str() {
        "open" => show_main_window(&app),
        "settings" => go_to(&app, "#/settings"),
        "start" => go_to(&app, "#/start"),
        "start_room" => go_to(&app, "#/start/in_person"),
        "live" => go_to(&app, "#/live"),
        "pill" => {
            let _ = open_pill_window(&app);
        }
        "pause" => {
            engine_api(&app, "POST", "/api/recorder/pause");
        }
        "resume" => {
            engine_api(&app, "POST", "/api/recorder/resume");
        }
        "mark" => {
            engine_api(&app, "POST", "/api/recorder/mark");
        }
        "stop" => {
            let body = engine_api(&app, "POST", "/api/recorder/stop").unwrap_or_default();
            let mid = body.split("\"meeting_id\"").nth(1).and_then(|r| r.split('"').nth(1)).map(str::to_string);
            go_to(&app, &mid.map(|m| format!("#/meetings/{m}")).unwrap_or_else(|| "#/".into()));
        }
        "quit" => app.exit(0),
        "update" => {
            if let Err(e) = install_update_now(&app) {
                use tauri_plugin_notification::NotificationExt;
                let _ = app.notification().builder().title("Modwen").body(&e).show();
            }
        }
        other => {
            if let Some(m) = other.strip_prefix("meeting:") {
                go_to(&app, &format!("#/meetings/{m}"));
            }
        }
    });
}

/// Keeps the menu, the icon colour and the recording timer next to the icon in step with the engine.
fn tray_refresher(app: AppHandle) {
    thread::spawn(move || {
        let mut shown: Option<(TrayInfo, bool, Option<String>)> = None;
        loop {
            thread::sleep(Duration::from_millis(1500));
            let ready = app.state::<Engine>().conn.lock().unwrap().is_some();
            let info: TrayInfo = if ready {
                match engine_api(&app, "GET", "/api/tray").and_then(|b| serde_json::from_str(&b).ok()) {
                    Some(i) => i,
                    None => continue,
                }
            } else {
                TrayInfo::default()
            };
            for n in &info.notifications {
                use tauri_plugin_notification::NotificationExt;
                let _ = app.notification().builder().title(&n.title).body(&n.body).show();
            }
            let Some(tray) = app.tray_by_id("modwen") else { continue };
            #[cfg(target_os = "macos")]
            {
                let title = info.recording.then(|| format!("{} {}", if info.paused { "❚❚" } else { "●" }, fmt_elapsed(info.elapsed_ms)));
                let _ = tray.set_title(title.as_deref());
            }
            let upd = app.state::<Engine>().update.lock().unwrap().as_ref().map(|u| u.version.clone());
            let key = (info.shape(), ready, upd);
            if shown.as_ref() != Some(&key) {
                if let Ok(menu) = build_menu(&app, &info, ready) {
                    let _ = tray.set_menu(Some(menu));
                }
                let (icon, template) = tray_icon_for(&info);
                let _ = tray.set_icon(Some(icon));
                let _ = tray.set_icon_as_template(template);
                let _ = tray.set_tooltip(Some(if info.recording { "Modwen · recording" } else { "Modwen" }));
                shown = Some(key);
            }
        }
    });
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main_window(app)))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Engine::default())
        .invoke_handler(tauri::generate_handler![engine_state, restart_engine, open_pill, close_pill, show_main, selftest,
                                                 update_info, check_update, install_update])
        .setup(|app| {
            let handle = app.handle().clone();
            // Created here (not in tauri.conf.json) to catch the UI's window.open.
            let h = handle.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Modwen")
                .inner_size(1320.0, 860.0)
                .min_inner_size(380.0, 520.0)
                .center()
                .on_new_window(move |u, _| handle_new_window(&h, u))
                .build()?;
            TrayIconBuilder::with_id("modwen")
                .icon(Image::from_bytes(include_bytes!("../icons/tray.png"))?)
                .icon_as_template(true)
                .tooltip("Modwen")
                .menu(&build_menu(&handle, &TrayInfo::default(), false)?)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| on_tray_menu(app, event.id.as_ref()))
                .build(app)?;
            set_state(&handle, "starting", "Starting Modwen…", false, None);
            tray_refresher(handle.clone());
            update_checker(handle.clone());
            boot(handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the main window keeps Modwen running in the menu bar (a recording may be going on).
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Modwen")
        .run(|app, event| match event {
            RunEvent::Exit => stop_engine(app),
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => show_main_window(app),
            _ => {}
        });
}
