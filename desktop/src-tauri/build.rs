fn main() {
    // Declare the app's own commands, so capabilities can grant them one by one ("allow-open-pill", ...).
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "engine_state",
        "restart_engine",
        "open_pill",
        "close_pill",
        "show_main",
        "selftest",
        "update_info",
        "check_update",
        "install_update",
    ])))
    .expect("failed to run tauri-build");
}
