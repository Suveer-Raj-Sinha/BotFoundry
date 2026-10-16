mod engine;

use engine::{EngineInfo, EngineState};
use tauri::{Manager, RunEvent, State};

/// The only native capability the UI gets: ask where the engine is listening.
#[tauri::command]
fn get_engine_info(state: State<'_, EngineState>) -> Result<EngineInfo, String> {
    state.info()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // Release builds write the engine's output to engine.log in this folder
            // (Windows: %LOCALAPPDATA%\com.botfoundry.app).
            let log_dir = app.path().app_local_data_dir().ok();
            app.manage(engine::start(log_dir));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_engine_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            app_handle.state::<EngineState>().stop();
        }
    });
}
