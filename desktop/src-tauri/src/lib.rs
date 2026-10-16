use serde::Serialize;
use std::{
    fs,
    fs::OpenOptions,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex, MutexGuard,
    },
    time::{Duration, Instant},
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
const DAEMON_GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(3);
const DAEMON_FORCED_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);
const DAEMON_EXIT_POLL_INTERVAL: Duration = Duration::from_millis(25);
const DAEMON_PREPARE_CONNECT_TIMEOUT: Duration = Duration::from_millis(500);
const DAEMON_PREPARE_RESPONSE_TIMEOUT: Duration = Duration::from_secs(5);
const DAEMON_PREPARE_MAX_RESPONSE_BYTES: usize = 4096;

struct ManagedDaemon {
    child: Child,
    containment: ProcessContainment,
}

#[cfg(windows)]
struct ProcessContainment {
    job: Option<std::os::windows::io::OwnedHandle>,
}

#[cfg(unix)]
struct ProcessContainment {
    process_group: i32,
}

#[cfg(not(any(unix, windows)))]
struct ProcessContainment;

struct DesktopState {
    exiting: AtomicBool,
    shortcut_registered: AtomicBool,
    voice_shortcut_registered: AtomicBool,
    daemon_starting: AtomicBool,
    daemon: Mutex<Option<ManagedDaemon>>,
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

fn window_layout(mode: &str, height: Option<f64>) -> Result<WindowDimensions, String> {
    let mut dimensions = window_dimensions(mode)?;
    if mode != "expanded" {
        if let Some(height) = height {
            if !height.is_finite() {
                return Err("window height must be finite".to_string());
            }
            dimensions.height = height.clamp(COMPACT_WINDOW_HEIGHT, 420.0);
        }
    }
    Ok(dimensions)
}

fn apply_window_mode<R: Runtime>(
    window: &WebviewWindow<R>,
    mode: &str,
    height: Option<f64>,
) -> Result<(), String> {
    let mut dimensions = window_layout(mode, height)?;
    if mode != "expanded" && height.is_some() {
        // Preserve a manually narrowed draft window while increasing its height.
        dimensions.width = window
            .inner_size()
            .map_err(|error| format!("failed to read window size: {error}"))?
            .to_logical::<f64>(window.scale_factor().map_err(|error| error.to_string())?)
            .width
            .max(COMPACT_MIN_WIDTH);
    }
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

fn daemon_guard(state: &DesktopState) -> MutexGuard<'_, Option<ManagedDaemon>> {
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

#[cfg(windows)]
impl ProcessContainment {
    fn create_before_spawn() -> Result<Self, String> {
        use std::{mem::size_of, os::windows::io::FromRawHandle, ptr};
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let raw_job = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
        if raw_job.is_null() {
            return Err(format!(
                "failed to create daemon Job Object: {}",
                std::io::Error::last_os_error()
            ));
        }
        let job = unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(raw_job) };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                raw_job,
                JobObjectExtendedLimitInformation,
                (&raw const limits).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            return Err(format!(
                "failed to configure daemon Job Object: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(Self { job: Some(job) })
    }

    fn attach(&self, child: &Child) -> Result<(), String> {
        use std::os::windows::io::{AsRawHandle, RawHandle};
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;

        let Some(job) = self.job.as_ref() else {
            return Err("daemon Job Object is already closed".to_string());
        };
        let assigned = unsafe {
            AssignProcessToJobObject(
                job.as_raw_handle() as RawHandle,
                child.as_raw_handle() as RawHandle,
            )
        };
        if assigned == 0 {
            return Err(format!(
                "failed to assign daemon to Job Object: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn request_graceful_shutdown(&self) -> Result<bool, String> {
        // A GUI process created with CREATE_NO_WINDOW has no shared console to
        // receive CTRL_BREAK. The authenticated prepare-shutdown request has
        // already released daemon-owned sidecars, but it intentionally does
        // not stop Uvicorn. Closing the Job Object is therefore the reliable
        // Windows termination operation. The boolean tells the caller not to
        // wait for a process signal that was never sent.
        Ok(false)
    }

    fn force_terminate_tree(&mut self) -> Result<(), String> {
        // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates every process still in
        // the job, including optional worker descendants that created a new
        // console process group. No PID-based taskkill fallback is used.
        drop(self.job.take());
        Ok(())
    }
}

#[cfg(unix)]
impl ProcessContainment {
    fn from_child(child: &Child) -> Result<Self, String> {
        let process_group = i32::try_from(child.id())
            .map_err(|_| "daemon PID is outside the supported Unix range".to_string())?;
        Ok(Self { process_group })
    }

    fn request_graceful_shutdown(&self) -> Result<bool, String> {
        self.signal(-self.process_group, libc::SIGTERM)?;
        Ok(true)
    }

    fn force_terminate_tree(&mut self) -> Result<(), String> {
        self.signal(-self.process_group, libc::SIGKILL)
    }

    fn signal(&self, target: i32, signal: i32) -> Result<(), String> {
        if unsafe { libc::kill(target, signal) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            return Ok(());
        }
        Err(format!(
            "failed to signal daemon process group {}: {error}",
            self.process_group
        ))
    }
}

#[cfg(not(any(unix, windows)))]
impl ProcessContainment {
    fn from_child(_child: &Child) -> Result<Self, String> {
        Ok(Self)
    }

    fn request_graceful_shutdown(&self) -> Result<bool, String> {
        Ok(false)
    }

    fn force_terminate_tree(&mut self) -> Result<(), String> {
        Ok(())
    }
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

fn take_daemon(state: &DesktopState) -> Option<ManagedDaemon> {
    daemon_guard(state).take()
}

fn request_daemon_shutdown(connection: &DaemonConnection) -> Result<(), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], connection.port));
    let mut stream = TcpStream::connect_timeout(&address, DAEMON_PREPARE_CONNECT_TIMEOUT)
        .map_err(|error| format!("failed to connect to daemon shutdown endpoint: {error}"))?;
    stream
        .set_read_timeout(Some(DAEMON_PREPARE_RESPONSE_TIMEOUT))
        .map_err(|error| format!("failed to bound daemon shutdown response: {error}"))?;
    stream
        .set_write_timeout(Some(DAEMON_PREPARE_CONNECT_TIMEOUT))
        .map_err(|error| format!("failed to bound daemon shutdown request: {error}"))?;
    let request = format!(
        "POST /lifecycle/prepare-shutdown HTTP/1.1\r\n\
         Host: 127.0.0.1:{}\r\n\
         Authorization: Bearer {}\r\n\
         Connection: close\r\n\
         Content-Length: 0\r\n\r\n",
        connection.port, connection.token
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("failed to send daemon shutdown request: {error}"))?;
    stream
        .flush()
        .map_err(|error| format!("failed to flush daemon shutdown request: {error}"))?;

    let mut response = Vec::with_capacity(512);
    let mut buffer = [0_u8; 512];
    while response.len() < DAEMON_PREPARE_MAX_RESPONSE_BYTES {
        let remaining = DAEMON_PREPARE_MAX_RESPONSE_BYTES - response.len();
        let maximum_read = remaining.min(buffer.len());
        let read = stream
            .read(&mut buffer[..maximum_read])
            .map_err(|error| format!("failed to read daemon shutdown response: {error}"))?;
        if read == 0 {
            break;
        }
        response.extend_from_slice(&buffer[..read]);
        if response.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let status_line_end = response
        .windows(2)
        .position(|window| window == b"\r\n")
        .ok_or_else(|| "daemon shutdown response was incomplete".to_string())?;
    let status_line = std::str::from_utf8(&response[..status_line_end])
        .map_err(|_| "daemon shutdown response was not valid HTTP".to_string())?;
    let status_code = status_line
        .split_ascii_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "daemon shutdown response had no status code".to_string())?;
    if status_code != 200 {
        return Err(format!(
            "daemon shutdown endpoint returned HTTP {status_code}"
        ));
    }
    Ok(())
}

fn wait_for_child_exit(child: &mut Child, timeout: Duration) -> Result<bool, std::io::Error> {
    let deadline = Instant::now() + timeout;
    loop {
        if child.try_wait()?.is_some() {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        std::thread::sleep(DAEMON_EXIT_POLL_INTERVAL);
    }
}

fn stop_daemon(mut daemon: ManagedDaemon) {
    let pid = daemon.child.id();
    match daemon.child.try_wait() {
        Ok(Some(_)) => {
            if let Err(error) = daemon.containment.force_terminate_tree() {
                eprintln!("[Jarvs/daemon] failed to close residual process tree {pid}: {error}");
            }
            return;
        }
        Ok(None) => {}
        Err(error) => eprintln!("[Jarvs/daemon] failed to query process {pid}: {error}"),
    }

    let graceful_requested = match daemon.containment.request_graceful_shutdown() {
        Ok(requested) => requested,
        Err(error) => {
            eprintln!("[Jarvs/daemon] graceful shutdown signal failed for {pid}: {error}");
            false
        }
    };
    if graceful_requested {
        match wait_for_child_exit(&mut daemon.child, DAEMON_GRACEFUL_SHUTDOWN_TIMEOUT) {
            Ok(true) => {
                if let Err(error) = daemon.containment.force_terminate_tree() {
                    eprintln!(
                        "[Jarvs/daemon] failed to close residual process tree {pid}: {error}"
                    );
                }
                return;
            }
            Ok(false) => {
                eprintln!("[Jarvs/daemon] process {pid} exceeded the graceful shutdown timeout")
            }
            Err(error) => {
                eprintln!("[Jarvs/daemon] failed while waiting for process {pid}: {error}")
            }
        }
    }

    if let Err(error) = daemon.containment.force_terminate_tree() {
        eprintln!("[Jarvs/daemon] failed to terminate process tree {pid}: {error}");
    }
    match wait_for_child_exit(&mut daemon.child, DAEMON_FORCED_SHUTDOWN_TIMEOUT) {
        Ok(true) => return,
        Ok(false) => {
            eprintln!("[Jarvs/daemon] process {pid} survived its containment shutdown");
        }
        Err(error) => eprintln!("[Jarvs/daemon] failed while reaping process {pid}: {error}"),
    }

    // This final root-only fallback is for an unexpected platform/API failure;
    // containment above remains responsible for descendants.
    if let Err(error) = daemon.child.kill() {
        eprintln!("[Jarvs/daemon] failed to stop root process {pid}: {error}");
    }
    if let Err(error) = daemon.child.wait() {
        eprintln!("[Jarvs/daemon] failed to reap root process {pid}: {error}");
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
        if let Some(daemon) = daemon {
            if let Err(error) = request_daemon_shutdown(&state.connection) {
                eprintln!("[Jarvs/daemon] {error}");
            }
            stop_daemon(daemon);
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

    ensure_runtime_settings(&runtime)?;
    Ok(runtime)
}

#[cfg(any(not(debug_assertions), test))]
fn ensure_runtime_settings(runtime: &Path) -> Result<(), String> {
    // This directory belongs to the packaged runtime. Never create or rewrite
    // a custom workspace from settings here.
    let workspace = runtime.join("workspace");
    fs::create_dir_all(&workspace).map_err(|error| {
        format!(
            "failed to create managed workspace {}: {error}",
            workspace.display()
        )
    })?;
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

    Ok(())
}

fn spawn_daemon<R: Runtime>(app: &AppHandle<R>) -> Result<ManagedDaemon, String> {
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

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    #[cfg(windows)]
    let containment = ProcessContainment::create_before_spawn()?;

    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start Python daemon: {error}"))?;

    #[cfg(windows)]
    if let Err(error) = containment.attach(&child) {
        // The daemon is never published to DesktopState before assignment, so
        // the renderer cannot submit a request during this fail-closed path.
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }

    #[cfg(unix)]
    let containment = ProcessContainment::from_child(&child)?;

    #[cfg(not(any(unix, windows)))]
    let containment = ProcessContainment::from_child(&child)?;

    Ok(ManagedDaemon { child, containment })
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
fn set_main_window_mode(
    window: WebviewWindow,
    mode: String,
    height: Option<f64>,
) -> Result<(), String> {
    if window.label() != MAIN_WINDOW_LABEL {
        return Err("window mode can only be changed for the main window".to_string());
    }
    apply_window_mode(&window, &mode, height)
}

#[tauri::command]
fn new_session(app: AppHandle, hide_after: Option<bool>) -> Result<(), String> {
    emit_new_session(&app, "frontend", hide_after.unwrap_or(false))
}

#[tauri::command]
fn daemon_status(app: AppHandle) -> DaemonStatus {
    let state = desktop_state(&app);
    let mut daemon_slot = daemon_guard(&state);
    let mut running = false;
    let mut pid = None;
    let mut exited = false;

    if let Some(daemon) = daemon_slot.as_mut() {
        match daemon.child.try_wait() {
            Ok(None) => {
                running = true;
                pid = Some(daemon.child.id());
            }
            Ok(Some(_)) => {
                exited = true;
            }
            Err(error) => {
                report_native_error(&app, "daemon", error);
                pid = Some(daemon.child.id());
            }
        }
    }
    if exited {
        if let Some(mut daemon) = daemon_slot.take() {
            if let Err(error) = daemon.containment.force_terminate_tree() {
                report_native_error(&app, "daemon", error);
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
                    Ok(daemon) => {
                        if state.exiting.load(Ordering::SeqCst) {
                            stop_daemon(daemon);
                        } else {
                            *daemon_guard(&state) = Some(daemon);
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
            if let Some(daemon) = take_daemon(&state) {
                if let Err(error) = request_daemon_shutdown(&state.connection) {
                    eprintln!("[Jarvs/daemon] {error}");
                }
                stop_daemon(daemon);
            }
            let attachment_root = take_attachment_root(&state);
            cleanup_attachment_root(attachment_root, &state.connection.run_id);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        attachment_kind, daemon_transition_guard, is_run_directory_name, request_daemon_shutdown,
        toggle_action, window_dimensions, DaemonConnection, DesktopState, ToggleAction,
    };
    use std::path::Path;
    use std::sync::{mpsc, Arc};
    use std::time::Duration;

    #[test]
    fn runtime_settings_create_and_restore_managed_workspace() {
        let root =
            std::env::temp_dir().join(format!("jarvs-runtime-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        super::ensure_runtime_settings(&root).unwrap();
        let settings = std::fs::read(root.join("settings.json")).unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&settings).unwrap();
        assert_eq!(
            parsed["workspace_path"],
            serde_json::json!(root.join("workspace"))
        );
        assert!(
            root.join("workspace").is_dir(),
            "fresh installs need a usable workspace"
        );
        std::fs::remove_dir(root.join("workspace")).unwrap();
        super::ensure_runtime_settings(&root).unwrap();
        assert!(
            root.join("workspace").is_dir(),
            "existing installs must recover the managed directory"
        );
        assert_eq!(std::fs::read(root.join("settings.json")).unwrap(), settings);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn runtime_settings_preserve_existing_custom_configuration() {
        let root =
            std::env::temp_dir().join(format!("jarvs-runtime-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let settings = br#"{"workspace_path":"custom-missing","execution_policy":"never"}"#;
        std::fs::write(root.join("settings.json"), settings).unwrap();
        super::ensure_runtime_settings(&root).unwrap();
        assert_eq!(std::fs::read(root.join("settings.json")).unwrap(), settings);
        assert!(!root.join("custom-missing").exists());
        std::fs::remove_dir_all(root).unwrap();
    }

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
    fn draft_height_grows_and_shrinks_without_opening_a_conversation() {
        use super::{window_layout, EXPANDED_WINDOW_HEIGHT};
        assert_eq!(window_layout("compact", Some(190.0)).unwrap().height, 190.0);
        assert_eq!(window_layout("compact", Some(120.0)).unwrap().height, 120.0);
        assert_eq!(window_layout("compact", Some(10.0)).unwrap().height, 120.0);
        assert_eq!(window_layout("compact", Some(900.0)).unwrap().height, 420.0);
        assert!(window_layout("compact", Some(f64::NAN)).is_err());
        assert_eq!(
            window_layout("expanded", Some(120.0)).unwrap().height,
            EXPANDED_WINDOW_HEIGHT
        );
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

    #[test]
    fn shutdown_request_is_loopback_authenticated() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind shutdown fixture");
        let port = listener
            .local_addr()
            .expect("shutdown fixture address")
            .port();
        let connection = DaemonConnection {
            http_url: format!("http://127.0.0.1:{port}"),
            ws_url: format!("ws://127.0.0.1:{port}/chat"),
            token: "fixture-token".to_string(),
            port,
            run_id: uuid::Uuid::new_v4().simple().to_string(),
        };
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().expect("accept shutdown request");
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("bound fixture read");
            let mut request = Vec::new();
            let mut buffer = [0_u8; 256];
            while request.len() < 4096 {
                let read = socket.read(&mut buffer).expect("read shutdown request");
                assert!(read > 0, "shutdown request ended before its headers");
                request.extend_from_slice(&buffer[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8(request).expect("shutdown request UTF-8");
            assert!(request.starts_with("POST /lifecycle/prepare-shutdown HTTP/1.1\r\n"));
            assert!(request.contains("\r\nAuthorization: Bearer fixture-token\r\n"));
            assert!(request.contains("\r\nContent-Length: 0\r\n"));
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\nConnection: close\r\n\r\n{\"status\":\"ready\"}",
                )
                .expect("write shutdown response");
        });

        request_daemon_shutdown(&connection).expect("authenticated shutdown request");
        server.join().expect("shutdown fixture join");
    }

    #[cfg(windows)]
    #[test]
    fn windows_job_close_terminates_daemon_descendants() {
        use super::{wait_for_child_exit, ProcessContainment};
        use std::os::windows::{
            io::{AsRawHandle, FromRawHandle, OwnedHandle},
            process::CommandExt,
        };
        use std::process::{Command, Stdio};
        use std::time::Instant;
        use windows_sys::Win32::{
            Foundation::WAIT_OBJECT_0,
            System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
        };

        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let test_root =
            std::env::temp_dir().join(format!("jarvs-job-test-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir(&test_root).expect("create job test directory");
        let pid_file = test_root.join("descendant.pid");
        let escaped_pid_file = pid_file.to_string_lossy().replace('\'', "''");
        let script = format!(
            "$child = Start-Process -FilePath $env:ComSpec \
             -ArgumentList @('/D','/C','ping -n 30 127.0.0.1 >nul') \
             -WindowStyle Hidden -PassThru; \
             [IO.File]::WriteAllText('{escaped_pid_file}', [string]$child.Id); \
             Wait-Process -Id $child.Id"
        );

        let mut containment =
            ProcessContainment::create_before_spawn().expect("create kill-on-close job");
        let mut root = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .expect("spawn daemon fixture");
        containment.attach(&root).expect("assign daemon fixture");

        let deadline = Instant::now() + Duration::from_secs(5);
        while !pid_file.is_file() && Instant::now() < deadline {
            assert!(
                root.try_wait().expect("query daemon fixture").is_none(),
                "daemon fixture exited before creating its descendant"
            );
            std::thread::sleep(Duration::from_millis(25));
        }
        let descendant_pid: u32 = std::fs::read_to_string(&pid_file)
            .expect("read descendant PID")
            .trim()
            .parse()
            .expect("parse descendant PID");
        let descendant_handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, descendant_pid) };
        assert!(
            !descendant_handle.is_null(),
            "descendant must be alive before Job Object close"
        );
        let descendant_handle = unsafe { OwnedHandle::from_raw_handle(descendant_handle) };

        containment
            .force_terminate_tree()
            .expect("close kill-on-close job");
        assert_eq!(
            unsafe { WaitForSingleObject(descendant_handle.as_raw_handle(), 3_000) },
            WAIT_OBJECT_0,
            "closing the Job Object must terminate the descendant"
        );
        assert!(
            wait_for_child_exit(&mut root, Duration::from_secs(3)).expect("reap daemon fixture"),
            "closing the Job Object must terminate the root"
        );
        let _ = std::fs::remove_dir_all(test_root);
    }
}
