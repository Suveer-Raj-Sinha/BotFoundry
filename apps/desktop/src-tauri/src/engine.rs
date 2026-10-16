//! Starts and stops the local Python engine.
//!
//! Debug builds (`tauri dev`): run `python -m botfoundry` from the engine's virtual environment,
//! with output going to the terminal.
//! Release builds: run the frozen `botfoundry-engine` sidecar that Tauri places next to the app
//! executable, with output going to `engine.log` in the app's local data folder.
//!
//! Either way: a random free loopback port, a random per-launch token, `--parent-pid` so the
//! engine stops itself if this app dies, and the whole process tree is killed on exit.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::Serialize;

/// What the React app needs in order to talk to the engine.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineInfo {
    pub base_url: String,
    pub token: String,
}

/// Managed Tauri state: the connection info (or the reason startup failed) plus the child process.
pub struct EngineState {
    info: Result<EngineInfo, String>,
    child: Mutex<Option<Child>>,
}

impl EngineState {
    pub fn info(&self) -> Result<EngineInfo, String> {
        self.info.clone()
    }

    /// Stop the engine and everything it spawned. Safe to call more than once.
    pub fn stop(&self) {
        let mut guard = match self.child.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Some(mut child) = guard.take() {
            kill_tree(child.id());
            let _ = child.kill(); // no-op if already dead; fallback on non-Windows
            let _ = child.wait();
        }
    }
}

/// `log_dir` is where release builds write `engine.log` (None = discard output).
pub fn start(log_dir: Option<PathBuf>) -> EngineState {
    match try_start(log_dir.as_deref()) {
        Ok((info, child)) => EngineState {
            info: Ok(info),
            child: Mutex::new(Some(child)),
        },
        Err(message) => EngineState {
            info: Err(message),
            child: Mutex::new(None),
        },
    }
}

/// The program to run, its fixed leading arguments, and an optional working directory.
struct EngineCommand {
    program: PathBuf,
    args: Vec<String>,
    cwd: Option<PathBuf>,
}

#[cfg(debug_assertions)]
fn engine_command() -> Result<EngineCommand, String> {
    // src-tauri -> desktop -> apps -> botfoundry, then engine/
    let engine_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("engine");

    #[cfg(windows)]
    let python = engine_dir.join(".venv").join("Scripts").join("python.exe");
    #[cfg(not(windows))]
    let python = engine_dir.join(".venv").join("bin").join("python");

    if !python.exists() {
        return Err(format!(
            "Engine Python not found at {}. Create the engine virtual environment first.",
            python.display()
        ));
    }
    Ok(EngineCommand {
        program: python,
        args: vec!["-m".into(), "botfoundry".into()],
        cwd: Some(engine_dir),
    })
}

#[cfg(not(debug_assertions))]
fn engine_command() -> Result<EngineCommand, String> {
    // Tauri copies the `externalBin` sidecar next to the app executable, without the target triple.
    let exe = std::env::current_exe().map_err(|e| format!("Cannot locate the app executable: {e}"))?;
    let dir = exe.parent().ok_or("The app executable has no parent folder.")?;
    let name = if cfg!(windows) { "botfoundry-engine.exe" } else { "botfoundry-engine" };
    let program = dir.join(name);
    if !program.exists() {
        return Err(format!(
            "The engine executable is missing: {}. Rebuild with scripts/build_engine.py first.",
            program.display()
        ));
    }
    Ok(EngineCommand { program, args: Vec::new(), cwd: None })
}

#[cfg(debug_assertions)]
fn configure_output(command: &mut Command, _log_dir: Option<&Path>) -> Result<(), String> {
    command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    Ok(())
}

#[cfg(not(debug_assertions))]
fn configure_output(command: &mut Command, log_dir: Option<&Path>) -> Result<(), String> {
    use std::fs::File;

    // A packaged GUI app has no console, so send engine output to a log file instead.
    let Some(dir) = log_dir else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
        return Ok(());
    };
    std::fs::create_dir_all(dir).map_err(|e| format!("Cannot create log folder {}: {e}", dir.display()))?;
    let path = dir.join("engine.log"); // overwritten on each launch
    let file = File::create(&path).map_err(|e| format!("Cannot create {}: {e}", path.display()))?;
    let second = file.try_clone().map_err(|e| format!("Cannot open log file: {e}"))?;
    command.stdout(Stdio::from(file)).stderr(Stdio::from(second));
    Ok(())
}

fn try_start(log_dir: Option<&Path>) -> Result<(EngineInfo, Child), String> {
    let engine = engine_command()?;

    // Ask the OS for a free loopback port. (Tiny race window; acceptable for a local app.)
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|e| format!("Could not find a free port: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("Could not read the chosen port: {e}"))?
        .port();
    drop(listener);

    let token = random_token()?;

    let mut command = Command::new(&engine.program);
    command
        .args(&engine.args)
        .args([
            "--port",
            &port.to_string(),
            // Lets the engine shut itself down if this app dies without running cleanup.
            "--parent-pid",
            &std::process::id().to_string(),
        ])
        .env("BOTFOUNDRY_TOKEN", &token)
        .stdin(Stdio::null());
    if let Some(cwd) = &engine.cwd {
        command.current_dir(cwd);
    }
    configure_output(&mut command, log_dir)?;
    hide_console(&mut command);
    isolate_process_group(&mut command);

    let child = command
        .spawn()
        .map_err(|e| format!("Could not start the engine ({}): {e}", engine.program.display()))?;

    Ok((
        EngineInfo {
            base_url: format!("http://127.0.0.1:{port}"),
            token,
        },
        child,
    ))
}

fn random_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|e| format!("Could not generate a token: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// On Unix, start the engine in its own process group so the whole tree can be killed at once.
#[cfg(unix)]
fn isolate_process_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(not(unix))]
fn isolate_process_group(_command: &mut Command) {}

/// Prevent a console window from flashing up for the child process on Windows.
#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

/// The frozen engine (and a venv's python.exe) can be a launcher that starts the real process as a
/// child, so killing only the launcher would orphan the server. `taskkill /T` ends the whole tree.
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }
    #[cfg(not(windows))]
    {
        // The engine leads its own process group (see isolate_process_group); kill the group.
        // `--` stops `kill` from reading the negative id as an option. Never signal pid 0/1.
        if pid > 1 {
            let _ = Command::new("kill").args(["-KILL", "--", &format!("-{pid}")]).status();
        }
    }
}
