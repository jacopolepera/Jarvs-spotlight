use serde::Serialize;
use std::{
    fs,
    fs::OpenOptions,
    io::Write,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex, MutexGuard,
    },
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, LogicalSize, Manager, Runtime, WebviewWindow, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use uuid::Uuid;

const MAIN_WINDOW_LABEL: &str = "main";
const NEW_SESSION_EVENT: &str = "jarvs://new-session";
const VOICE_RECORDING_EVENT: &str = "jarvs://voice-recording";
const NATIVE_ERROR_EVENT: &str = "jarvs://native-error";
const TRAY_OPEN_ID: &str = "jarvs-open";
const TRAY_NEW_SESSION_ID: &str = "jarvs-new-session";
const TRAY_QUIT_ID: &str = "jarvs-quit";
const COMPACT_WINDOW_WIDTH: f64 = 600.0;
const COMPACT_WINDOW_HEIGHT: f64 = 120.0;
const COMPACT_TALL_WINDOW_HEIGHT: f64 = 168.0;
const COMPACT_MIN_WIDTH: f64 = 300.0;
const EXPANDED_WINDOW_WIDTH: f64 = 600.0;
const EXPANDED_WINDOW_HEIGHT: f64 = 500.0;
const EXPANDED_MIN_WIDTH: f64 = 300.0;
const EXPANDED_MIN_HEIGHT: f64 = 200.0;

struct DesktopState {
    exiting: AtomicBool,
    shortcut_registered: AtomicBool,
    voice_shortcut_registered: AtomicBool,
    daemon_starting: AtomicBool,
    daemon: Mutex<Option<Child>>,
    daemon_transition: Mutex<()>,
    connection: DaemonConnection,
    attachment_root: Mutex<Option<PathBuf>>,
}

impl DesktopState {
    fn new(connection: DaemonConnection) -> Self {
        Self {
            exiting: AtomicBool::new(false),
            shortcut_registered: AtomicBool::new(false),
            voice_shortcut_registered: AtomicBool::new(false),
            daemon_starting: AtomicBool::new(true),
            daemon: Mutex::new(None),
            daemon_transition: Mutex::new(()),
            connection,
            attachment_root: Mutex::new(None),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DaemonConnection {
    http_url: String,
    ws_url: String,
    token: String,
    #[serde(skip)]
    port: u16,
    #[serde(skip)]
    run_id: String,
}

impl DaemonConnection {
    fn create() -> Result<Self, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|error| format!("failed to reserve daemon port: {error}"))?;
        let port = listener
            .local_addr()
            .map_err(|error| format!("failed to read daemon port: {error}"))?
            .port();
        drop(listener);
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        Ok(Self {
            http_url: format!("http://127.0.0.1:{port}"),
            ws_url: format!("ws://127.0.0.1:{port}/chat"),
            token,
            port,
            run_id: Uuid::new_v4().simple().to_string(),
        })
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct StagedAttachment {
    attachment_id: String,
    display_name: String,
    kind: &'static str,
    size: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ToggleAction {
    Hide,
    ShowAndFocus,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct WindowDimensions {
    width: f64,
    height: f64,
    min_width: f64,
    min_height: f64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct NewSessionRequest {
    source: &'static str,
    hide_after: bool,
}

#[derive(Clone, Serialize)]
struct VoiceRecordingRequest {
    action: &'static str,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct NativeError {
    component: &'static str,
    message: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DaemonStatus {
    running: bool,
    starting: bool,
    pid: Option<u32>,
    shortcut_registered: bool,
    voice_shortcut_registered: bool,
}

fn toggle_action(is_visible: bool, is_minimized: bool) -> ToggleAction {
    if is_visible && !is_minimized {
        ToggleAction::Hide
    } else {
        ToggleAction::ShowAndFocus
    }
}

fn window_dimensions(mode: &str) -> Result<WindowDimensions, String> {
    match mode {
        "compact" => Ok(WindowDimensions {
            width: COMPACT_WINDOW_WIDTH,
            height: COMPACT_WINDOW_HEIGHT,
            min_width: COMPACT_MIN_WIDTH,
            min_height: COMPACT_WINDOW_HEIGHT,
        }),
        "compactTall" => Ok(WindowDimensions {
            width: COMPACT_WINDOW_WIDTH,
            height: COMPACT_TALL_WINDOW_HEIGHT,
            min_width: COMPACT_MIN_WIDTH,
            min_height: COMPACT_WINDOW_HEIGHT,
        }),
        "expanded" => Ok(WindowDimensions {
            width: EXPANDED_WINDOW_WIDTH,
            height: EXPANDED_WINDOW_HEIGHT,
            min_width: EXPANDED_MIN_WIDTH,
            min_height: EXPANDED_MIN_HEIGHT,
        }),
        _ => Err(format!("unsupported window mode: {mode}")),
    }
}

fn apply_window_mode<R: Runtime>(window: &WebviewWindow<R>, mode: &str) -> Result<(), String> {
    let dimensions = window_dimensions(mode)?;
    window
        .set_min_size(Some(LogicalSize::new(
            dimensions.min_width,
            dimensions.min_height,
        )))
        .map_err(|error| format!("failed to set {mode} window minimum size: {error}"))?;
    window
        .set_size(LogicalSize::new(dimensions.width, dimensions.height))
        .map_err(|error| format!("failed to set {mode} window size: {error}"))
}

fn report_native_error<R: Runtime>(
    app: &AppHandle<R>,
    component: &'static str,
    error: impl std::fmt::Display,
) {
    let payload = NativeError {
        component,
        message: error.to_string(),
    };
    eprintln!("[Jarvs/{component}] {}", payload.message);

    if let Ok(log_dir) = app.path().app_log_dir() {
        if fs::create_dir_all(&log_dir).is_ok() {
            if let Ok(mut log) = OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_dir.join("native.log"))
            {
                let _ = writeln!(log, "[{component}] {}", payload.message);
            }
        }
    }

    let _ = app.emit(NATIVE_ERROR_EVENT, payload);
}

fn main_window<R: Runtime>(app: &AppHandle<R>) -> Result<WebviewWindow<R>, String> {
    app.get_webview_window(MAIN_WINDOW_LABEL)
        .ok_or_else(|| "main window is not available".to_string())
}

fn show_and_focus_main<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let window = main_window(app)?;

    // Calling unminimize unconditionally also covers windows minimized through
    // the taskbar or OS shortcuts, not only windows hidden by Jarvs itself.
    window
        .unminimize()
        .map_err(|error| format!("failed to restore main window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("failed to show main window: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("failed to focus main window: {error}"))
}

fn hide_main<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    main_window(app)?
        .hide()
        .map_err(|error| format!("failed to hide main window: {error}"))
}

fn toggle_main<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let window = main_window(app)?;

    // If querying either state fails, prefer showing the app. That keeps the
    // global shortcut useful instead of potentially hiding an unknown state.
    let is_visible = match window.is_visible() {
        Ok(value) => value,
        Err(error) => {
            report_native_error(app, "window-state", error);
            false
        }
    };
    let is_minimized = match window.is_minimized() {
        Ok(value) => value,
        Err(error) => {
            report_native_error(app, "window-state", error);
            true
        }
    };

    match toggle_action(is_visible, is_minimized) {
        ToggleAction::Hide => window
            .hide()
            .map_err(|error| format!("failed to hide main window: {error}")),
        ToggleAction::ShowAndFocus => show_and_focus_main(app),
    }
}

fn emit_new_session<R: Runtime>(
    app: &AppHandle<R>,
    source: &'static str,
    hide_after: bool,
) -> Result<(), String> {
    app.emit(NEW_SESSION_EVENT, NewSessionRequest { source, hide_after })
        .map_err(|error| format!("failed to emit new-session request: {error}"))
}

fn emit_voice_recording<R: Runtime>(
    app: &AppHandle<R>,
    action: &'static str,
) -> Result<(), String> {
    app.emit(VOICE_RECORDING_EVENT, VoiceRecordingRequest { action })
        .map_err(|error| format!("failed to emit voice-recording request: {error}"))
}

fn desktop_state<R: Runtime>(app: &AppHandle<R>) -> tauri::State<'_, DesktopState> {
    app.state::<DesktopState>()
}

fn daemon_guard(state: &DesktopState) -> MutexGuard<'_, Option<Child>> {
    state
        .daemon
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn daemon_transition_guard(state: &DesktopState) -> MutexGuard<'_, ()> {
    state
        .daemon_transition
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn is_run_directory_name(name: &str) -> bool {
    name.len() == 32 && name.bytes().all(|value| value.is_ascii_hexdigit())
}

fn prune_stale_attachment_roots(base: &Path, current_run_id: &str) -> Result<(), String> {
    let entries = fs::read_dir(base).map_err(|error| {
        format!(
            "failed to inspect attachment cache {}: {error}",
            base.display()
        )
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            format!(
                "failed to inspect an attachment cache entry in {}: {error}",
                base.display()
            )
        })?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if name == current_run_id || !is_run_directory_name(name) {
            continue;
        }
        let file_type = entry.file_type().map_err(|error| {
            format!(
                "failed to inspect attachment cache {}: {error}",
                entry.path().display()
            )
        })?;
        if file_type.is_dir() {
            fs::remove_dir_all(entry.path()).map_err(|error| {
                format!(
                    "failed to remove stale attachment cache {}: {error}",
                    entry.path().display()
                )
            })?;
        }
    }
    Ok(())
}

fn ensure_attachment_root<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let state = desktop_state(app);
    let mut configured = state
        .attachment_root
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(path) = configured.as_ref() {
        return Ok(path.clone());
    }
    let base = app
        .path()
        .app_cache_dir()
        .map_err(|error| format!("failed to resolve attachment cache: {error}"))?
        .join("attachments");
    fs::create_dir_all(&base).map_err(|error| {
        format!(
            "failed to create attachment cache {}: {error}",
            base.display()
        )
    })?;
    if let Err(error) = prune_stale_attachment_roots(&base, &state.connection.run_id) {
        eprintln!("[Jarvs/attachments] {error}");
    }
    let path = base.join(&state.connection.run_id);
    fs::create_dir_all(&path).map_err(|error| {
        format!(
            "failed to create attachment cache {}: {error}",
            path.display()
        )
    })?;
    *configured = Some(path.clone());
    Ok(path)
}

fn take_attachment_root(state: &DesktopState) -> Option<PathBuf> {
    state
        .attachment_root
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .take()
}

fn cleanup_attachment_root(path: Option<PathBuf>, run_id: &str) {
    let Some(path) = path else {
        return;
    };
    let managed = is_run_directory_name(run_id)
        && path.file_name().and_then(|value| value.to_str()) == Some(run_id)
        && path
            .parent()
            .and_then(Path::file_name)
            .and_then(|value| value.to_str())
            == Some("attachments");
    if !managed {
        eprintln!(
            "[Jarvs/attachments] refused to remove unmanaged path {}",
            path.display()
        );
        return;
    }
    match fs::remove_dir_all(&path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => eprintln!(
            "[Jarvs/attachments] failed to remove {}: {error}",
            path.display()
        ),
    }
}

fn take_daemon(state: &DesktopState) -> Option<Child> {
    daemon_guard(state).take()
}

fn stop_child(mut child: Child) {
    let pid = child.id();
    match child.try_wait() {
        Ok(Some(_)) => return,
        Ok(None) => {}
        Err(error) => eprintln!("[Jarvs/daemon] failed to query process {pid}: {error}"),
    }

    if let Err(error) = child.kill() {
        eprintln!("[Jarvs/daemon] failed to stop process {pid}: {error}");
        return;
    }
    if let Err(error) = child.wait() {
        eprintln!("[Jarvs/daemon] failed to reap process {pid}: {error}");
    }
}

fn begin_quit<R: Runtime>(app: AppHandle<R>) {
    let state = desktop_state(&app);
    if state.exiting.swap(true, Ordering::SeqCst) {
        return;
    }

    std::thread::spawn(move || {
        let state = desktop_state(&app);
        let transition = daemon_transition_guard(&state);
        let daemon = take_daemon(&state);
        let attachment_root = take_attachment_root(&state);
        let run_id = state.connection.run_id.clone();
        if let Some(child) = daemon {
            stop_child(child);
        }
        cleanup_attachment_root(attachment_root, &run_id);
        state.daemon_starting.store(false, Ordering::SeqCst);
        drop(transition);
        app.exit(0);
    });
}

#[cfg(not(debug_assertions))]
fn copy_directory(source: &std::path::Path, destination: &std::path::Path) -> Result<(), String> {
    fs::create_dir_all(destination).map_err(|error| {
        format!(
            "failed to create runtime directory {}: {error}",
            destination.display()
        )
    })?;

    let entries = fs::read_dir(source)
        .map_err(|error| format!("failed to read resource {}: {error}", source.display()))?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            format!(
                "failed to read an entry from resource {}: {error}",
                source.display()
            )
        })?;
        let source_path = entry.path();
        let file_name = entry.file_name();
        if file_name == "__pycache__" || source_path.extension().is_some_and(|ext| ext == "pyc") {
            continue;
        }
        let destination_path = destination.join(file_name);
        let file_type = entry.file_type().map_err(|error| {
            format!(
                "failed to inspect resource {}: {error}",
                source_path.display()
            )
        })?;

        if file_type.is_dir() {
            copy_directory(&source_path, &destination_path)?;
        } else if file_type.is_file() {
            fs::copy(&source_path, &destination_path).map_err(|error| {
                format!(
                    "failed to copy {} to {}: {error}",
                    source_path.display(),
                    destination_path.display()
                )
            })?;
        }
    }

    Ok(())
}

#[cfg(not(debug_assertions))]
fn copy_resource_file(
    source: &std::path::Path,
    destination: &std::path::Path,
) -> Result<(), String> {
    fs::copy(source, destination).map_err(|error| {
        format!(
            "failed to copy resource {} to {}: {error}",
            source.display(),
            destination.display()
        )
    })?;
    Ok(())
}

#[cfg(debug_assertions)]
fn daemon_runtime_root<R: Runtime>(_app: &AppHandle<R>) -> Result<PathBuf, String> {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(|error| format!("failed to resolve development runtime root: {error}"))
}

#[cfg(not(debug_assertions))]
fn daemon_runtime_root<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    use tauri::path::BaseDirectory;

    let bundled = app
        .path()
        .resolve("python", BaseDirectory::Resource)
        .map_err(|error| format!("failed to resolve bundled Python resources: {error}"))?;
    let runtime = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?
        .join("python-runtime");

    fs::create_dir_all(&runtime).map_err(|error| {
        format!(
            "failed to create Python runtime {}: {error}",
            runtime.display()
        )
    })?;
    // The package version alone is not sufficient for resource invalidation:
    // Python resources can change between builds without a Cargo version bump.
    let resource_version = env!("JARVS_RESOURCE_VERSION");
    let marker = runtime.join(".resource-version");
    let resources_current = fs::read_to_string(&marker)
        .map(|value| value.trim() == resource_version)
        .unwrap_or(false);
    if !resources_current {
        let core_runtime = runtime.join("core");
        if core_runtime.exists() {
            fs::remove_dir_all(&core_runtime).map_err(|error| {
                format!(
                    "failed to replace managed Python core {}: {error}",
                    core_runtime.display()
                )
            })?;
        }
        copy_directory(&bundled.join("core"), &core_runtime)?;
        copy_resource_file(&bundled.join("models.json"), &runtime.join("models.json"))?;
        copy_resource_file(
            &bundled.join("system_prompt.txt"),
            &runtime.join("system_prompt.txt"),
        )?;
        fs::write(&marker, resource_version).map_err(|error| {
            format!(
                "failed to record Python resource version {}: {error}",
                marker.display()
            )
        })?;
    }

    let settings_path = runtime.join("settings.json");
    if !settings_path.exists() {
        let settings = serde_json::json!({
            "theme": "Light",
            "workspace_path": runtime.join("workspace"),
            "model": "lmstudio-default",
            "window_size": [600, 120],
            "execution_policy": "always_ask"
        });
        let settings_json = serde_json::to_vec_pretty(&settings)
            .map_err(|error| format!("failed to serialize default settings: {error}"))?;
        fs::write(&settings_path, settings_json).map_err(|error| {
            format!(
                "failed to create default settings {}: {error}",
                settings_path.display()
            )
        })?;
    }

    Ok(runtime)
}

fn spawn_daemon<R: Runtime>(app: &AppHandle<R>) -> Result<Child, String> {
    let runtime_root = daemon_runtime_root(app)?;
    let attachment_root = ensure_attachment_root(app)?;
    let state = desktop_state(app);
    let connection = state.connection.clone();
    let python = std::env::var_os("JARVS_PYTHON").unwrap_or_else(|| "python".into());
    let mut command = Command::new(python);
    let port = connection.port.to_string();
    command
        .current_dir(&runtime_root)
        .args([
            "-m",
            "uvicorn",
            "core.daemon.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            &port,
            "--ws-max-size",
            "1048576",
            "--no-access-log",
        ])
        .env("JARVS_DAEMON_TOKEN", &connection.token)
        .env("JARVS_DAEMON_PORT", &port)
        .env(
            "JARVS_ALLOWED_ORIGINS",
            "http://localhost:1420,http://127.0.0.1:1420,http://tauri.localhost,tauri://localhost",
        )
        .env("JARVS_ATTACHMENT_ROOT", attachment_root)
        .stdin(Stdio::null());

    let log_file = app.path().app_log_dir().ok().and_then(|log_dir| {
        fs::create_dir_all(&log_dir).ok()?;
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_dir.join("daemon.log"))
            .ok()
    });
    if let Some(stdout) = log_file {
        if let Ok(stderr) = stdout.try_clone() {
            command
                .stdout(Stdio::from(stdout))
                .stderr(Stdio::from(stderr));
        } else {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }
    } else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    command
        .spawn()
        .map_err(|error| format!("failed to start Python daemon: {error}"))
}

fn register_shortcuts<R: Runtime>(app: &AppHandle<R>) {
    // Windows RegisterHotKey requires a non-modifier key. Keep the existing
    // Win+Alt+Space combination until a native hook is intentionally added.
    let shortcut = Shortcut::new(Some(Modifiers::SUPER | Modifiers::ALT), Code::Space);
    match app.global_shortcut().register(shortcut) {
        Ok(()) => {
            desktop_state(app)
                .shortcut_registered
                .store(true, Ordering::SeqCst);
        }
        Err(error) => {
            report_native_error(
                app,
                "global-shortcut",
                format!(
                    "Win+Alt+Space could not be registered ({error}); use the tray icon or reopen Jarvs Spotlight"
                ),
            );
        }
    }

    let voice_shortcut = Shortcut::new(None, Code::F4);
    match app.global_shortcut().register(voice_shortcut) {
        Ok(()) => {
            desktop_state(app)
                .voice_shortcut_registered
                .store(true, Ordering::SeqCst);
        }
        Err(error) => {
            report_native_error(
                app,
                "voice-shortcut",
                format!("F4 could not be registered ({error}); use the microphone button"),
            );
        }
    }
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, TRAY_OPEN_ID, "Open", true, None::<&str>)?;
    let new_session_item =
        MenuItem::with_id(app, TRAY_NEW_SESSION_ID, "New chat", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit_item = MenuItem::with_id(app, TRAY_QUIT_ID, "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&open_item, &new_session_item, &separator, &quit_item],
    )?;

    let mut tray = TrayIconBuilder::with_id("jarvs-tray")
        .tooltip("Jarvs Spotlight")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            TRAY_OPEN_ID => {
                if let Err(error) = show_and_focus_main(app) {
                    report_native_error(app, "tray", error);
                }
            }
            TRAY_NEW_SESSION_ID => {
                if let Err(error) = show_and_focus_main(app) {
                    report_native_error(app, "tray", error);
                }
                if let Err(error) = emit_new_session(app, "tray", false) {
                    report_native_error(app, "tray", error);
                }
            }
            TRAY_QUIT_ID => begin_quit(app.clone()),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;

    Ok(())
}

#[tauri::command]
fn show_main_window(app: AppHandle) -> Result<(), String> {
    show_and_focus_main(&app)
}

#[tauri::command]
fn hide_main_window(app: AppHandle) -> Result<(), String> {
    hide_main(&app)
}

#[tauri::command]
fn toggle_main_window(app: AppHandle) -> Result<(), String> {
    toggle_main(&app)
}

#[tauri::command]
fn set_main_window_mode(window: WebviewWindow, mode: String) -> Result<(), String> {
    if window.label() != MAIN_WINDOW_LABEL {
        return Err("window mode can only be changed for the main window".to_string());
    }
    apply_window_mode(&window, &mode)
}

#[tauri::command]
fn new_session(app: AppHandle, hide_after: Option<bool>) -> Result<(), String> {
    emit_new_session(&app, "frontend", hide_after.unwrap_or(false))
}

#[tauri::command]
fn daemon_status(app: AppHandle) -> DaemonStatus {
    let state = desktop_state(&app);
    let mut daemon = daemon_guard(&state);
    let mut running = false;
    let mut pid = None;

    if let Some(child) = daemon.as_mut() {
        match child.try_wait() {
            Ok(None) => {
                running = true;
                pid = Some(child.id());
            }
            Ok(Some(_)) => {
                *daemon = None;
            }
            Err(error) => {
                report_native_error(&app, "daemon", error);
                pid = Some(child.id());
            }
        }
    }

    DaemonStatus {
        running,
        starting: state.daemon_starting.load(Ordering::SeqCst),
        pid,
        shortcut_registered: state.shortcut_registered.load(Ordering::SeqCst),
        voice_shortcut_registered: state.voice_shortcut_registered.load(Ordering::SeqCst),
    }
}

#[tauri::command]
fn daemon_connection(window: WebviewWindow, app: AppHandle) -> Result<DaemonConnection, String> {
    if window.label() != MAIN_WINDOW_LABEL {
        return Err("daemon connection is only available to the main window".to_string());
    }
    Ok(desktop_state(&app).connection.clone())
}

fn attachment_kind(path: &Path) -> Option<&'static str> {
    let extension = path.extension()?.to_string_lossy().to_ascii_lowercase();
    match extension.as_str() {
        "png" | "jpg" | "jpeg" | "webp" | "heic" => Some("image"),
        "pdf" | "txt" | "csv" | "json" | "py" | "md" | "html" | "xml" | "yaml" | "yml" => {
            Some("file")
        }
        _ => None,
    }
}

#[tauri::command]
async fn pick_attachment(
    window: WebviewWindow,
    app: AppHandle,
) -> Result<Option<StagedAttachment>, String> {
    if window.label() != MAIN_WINDOW_LABEL {
        return Err("attachments can only be selected by the main window".to_string());
    }
    let attachment_root = ensure_attachment_root(&app)?;
    let dialog_app = app.clone();
    let selected = tauri::async_runtime::spawn_blocking(move || {
        dialog_app
            .dialog()
            .file()
            .add_filter(
                "Supported files",
                &[
                    "png", "jpg", "jpeg", "webp", "heic", "pdf", "txt", "csv", "json", "py", "md",
                    "html", "xml", "yaml", "yml",
                ],
            )
            .blocking_pick_file()
    })
    .await
    .map_err(|error| format!("attachment dialog failed: {error}"))?;

    let Some(selected) = selected else {
        return Ok(None);
    };
    let source = selected
        .into_path()
        .map_err(|error| format!("selected attachment is not a local file: {error}"))?;
    let kind = attachment_kind(&source)
        .ok_or_else(|| "selected file type is not supported".to_string())?;
    let metadata = fs::metadata(&source)
        .map_err(|error| format!("failed to inspect selected attachment: {error}"))?;
    if !metadata.is_file() {
        return Err("selected attachment is not a regular file".to_string());
    }
    let maximum_size = if kind == "image" {
        10 * 1024 * 1024
    } else {
        20 * 1024 * 1024
    };
    if metadata.len() > maximum_size {
        return Err(format!(
            "selected attachment exceeds the {} MiB limit",
            maximum_size / 1024 / 1024
        ));
    }

    let display_name = source
        .file_name()
        .ok_or_else(|| "selected attachment has no file name".to_string())?
        .to_string_lossy()
        .into_owned();
    let extension = source
        .extension()
        .map(|value| value.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    let attachment_id = format!("{}.{}", Uuid::new_v4().simple(), extension);
    let destination = attachment_root.join(&attachment_id);
    fs::copy(&source, &destination)
        .map_err(|error| format!("failed to stage selected attachment: {error}"))?;

    Ok(Some(StagedAttachment {
        attachment_id,
        display_name,
        kind,
        size: metadata.len(),
    }))
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    begin_quit(app);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let connection = match DaemonConnection::create() {
        Ok(connection) => connection,
        Err(error) => {
            eprintln!("[Jarvs/startup] {error}");
            return;
        }
    };
    let shortcut_for_handler = Shortcut::new(Some(Modifiers::SUPER | Modifiers::ALT), Code::Space);
    let voice_shortcut_for_handler = Shortcut::new(None, Code::F4);
    let shortcut_plugin = tauri_plugin_global_shortcut::Builder::new()
        .with_handler(move |app, shortcut, event| {
            if shortcut == &shortcut_for_handler && event.state() == ShortcutState::Pressed {
                if let Err(error) = toggle_main(app) {
                    report_native_error(app, "global-shortcut", error);
                }
            } else if shortcut == &voice_shortcut_for_handler {
                let action = if event.state() == ShortcutState::Pressed {
                    if let Err(error) = show_and_focus_main(app) {
                        report_native_error(app, "voice-shortcut", error);
                    }
                    "start"
                } else {
                    "stop"
                };
                if let Err(error) = emit_voice_recording(app, action) {
                    report_native_error(app, "voice-shortcut", error);
                }
            }
        })
        .build();

    let builder = tauri::Builder::default();

    // The single-instance plugin must be the first plugin registered.
    #[cfg(any(target_os = "windows", target_os = "linux", target_os = "macos"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(
        |app, _arguments, _working_directory| {
            if let Err(error) = show_and_focus_main(app) {
                report_native_error(app, "single-instance", error);
            }
        },
    ));

    let app = builder
        .manage(DesktopState::new(connection))
        .plugin(shortcut_plugin)
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            show_main_window,
            hide_main_window,
            toggle_main_window,
            set_main_window_mode,
            new_session,
            daemon_status,
            daemon_connection,
            pick_attachment,
            quit_app
        ])
        .on_window_event(|window, event| {
            if window.label() != MAIN_WINDOW_LABEL {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                let state = desktop_state(app);
                if state.exiting.load(Ordering::SeqCst) {
                    return;
                }

                api.prevent_close();
                if let Err(error) = emit_new_session(app, "window-close", true) {
                    report_native_error(app, "window-close", error);
                }
                if let Err(error) = hide_main(app) {
                    report_native_error(app, "window-close", error);
                }
            }
        })
        .setup(|app| {
            if let Some(icon) = app.default_window_icon() {
                if let Ok(window) = main_window(app.handle()) {
                    if let Err(error) = window.set_icon(icon.clone()) {
                        report_native_error(app.handle(), "window-icon", error);
                    }
                }
            }
            if let Err(error) = setup_tray(app) {
                report_native_error(app.handle(), "tray", error);
            }
            register_shortcuts(app.handle());

            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let state = desktop_state(&app_handle);
                let _transition = daemon_transition_guard(&state);
                if state.exiting.load(Ordering::SeqCst) {
                    state.daemon_starting.store(false, Ordering::SeqCst);
                    return;
                }
                match spawn_daemon(&app_handle) {
                    Ok(child) => {
                        if state.exiting.load(Ordering::SeqCst) {
                            stop_child(child);
                        } else {
                            *daemon_guard(&state) = Some(child);
                        }
                    }
                    Err(error) => report_native_error(&app_handle, "daemon", error),
                }
                state.daemon_starting.store(false, Ordering::SeqCst);
            });

            Ok(())
        })
        .build(tauri::generate_context!());

    let app = match app {
        Ok(app) => app,
        Err(error) => {
            eprintln!("[Jarvs/startup] failed to build application: {error}");
            return;
        }
    };

    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            let state = desktop_state(app);
            let _transition = daemon_transition_guard(&state);
            if let Some(child) = take_daemon(&state) {
                stop_child(child);
            }
            let attachment_root = take_attachment_root(&state);
            cleanup_attachment_root(attachment_root, &state.connection.run_id);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        attachment_kind, daemon_transition_guard, is_run_directory_name, toggle_action,
        window_dimensions, DaemonConnection, DesktopState, ToggleAction,
    };
    use std::path::Path;
    use std::sync::{mpsc, Arc};
    use std::time::Duration;

    #[test]
    fn visible_window_is_hidden() {
        assert_eq!(toggle_action(true, false), ToggleAction::Hide);
    }

    #[test]
    fn hidden_window_is_shown() {
        assert_eq!(toggle_action(false, false), ToggleAction::ShowAndFocus);
    }

    #[test]
    fn minimized_window_is_restored_even_if_reported_visible() {
        assert_eq!(toggle_action(true, true), ToggleAction::ShowAndFocus);
    }

    #[test]
    fn window_modes_use_compact_and_expanded_dimensions() {
        let compact = window_dimensions("compact").expect("compact dimensions");
        assert_eq!((compact.width, compact.height), (600.0, 120.0));
        assert_eq!((compact.min_width, compact.min_height), (300.0, 120.0));

        let expanded = window_dimensions("expanded").expect("expanded dimensions");
        assert_eq!((expanded.width, expanded.height), (600.0, 500.0));
        assert_eq!((expanded.min_width, expanded.min_height), (300.0, 200.0));
        assert!(window_dimensions("unknown").is_err());
    }

    #[test]
    fn daemon_connection_uses_ephemeral_port_and_secret() {
        let first = DaemonConnection::create().expect("first connection");
        let second = DaemonConnection::create().expect("second connection");
        assert!(first.http_url.starts_with("http://127.0.0.1:"));
        assert_eq!(first.ws_url, format!("ws://127.0.0.1:{}/chat", first.port));
        assert_eq!(first.token.len(), 64);
        assert_ne!(first.token, second.token);
    }

    #[test]
    fn attachment_extensions_are_allowlisted() {
        assert_eq!(attachment_kind(Path::new("image.png")), Some("image"));
        assert_eq!(attachment_kind(Path::new("notes.md")), Some("file"));
        assert_eq!(attachment_kind(Path::new("program.exe")), None);
    }

    #[test]
    fn attachment_cleanup_only_recognizes_generated_run_directories() {
        assert!(is_run_directory_name("0123456789abcdef0123456789abcdef"));
        assert!(!is_run_directory_name("../attachments"));
        assert!(!is_run_directory_name("not-a-run"));
    }

    #[test]
    fn quit_waits_for_in_flight_daemon_start_transition() {
        let state = Arc::new(DesktopState::new(
            DaemonConnection::create().expect("connection"),
        ));
        let (startup_entered_tx, startup_entered_rx) = mpsc::channel();
        let (release_startup_tx, release_startup_rx) = mpsc::channel();
        let startup_state = Arc::clone(&state);
        let startup = std::thread::spawn(move || {
            let _transition = daemon_transition_guard(&startup_state);
            startup_entered_tx.send(()).expect("signal startup");
            release_startup_rx.recv().expect("release startup");
        });
        startup_entered_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("startup entered");

        let (quit_attempted_tx, quit_attempted_rx) = mpsc::channel();
        let (quit_entered_tx, quit_entered_rx) = mpsc::channel();
        let quit_state = Arc::clone(&state);
        let quit = std::thread::spawn(move || {
            quit_attempted_tx.send(()).expect("signal quit attempt");
            let _transition = daemon_transition_guard(&quit_state);
            quit_entered_tx.send(()).expect("signal quit");
        });

        quit_attempted_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("quit attempted transition");
        assert!(quit_entered_rx
            .recv_timeout(Duration::from_millis(25))
            .is_err());
        release_startup_tx.send(()).expect("release startup");
        quit_entered_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("quit acquired transition");
        startup.join().expect("startup join");
        quit.join().expect("quit join");
    }
}
