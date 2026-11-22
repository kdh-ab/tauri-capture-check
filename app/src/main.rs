// 캡처 차단 실측용 최소 Tauri 앱: 보호 창(content protected)과 비교용 일반 창을 띄운다.
// Windows 릴리스 빌드에서는 콘솔 창을 따로 띄우지 않는다.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("failed to run capture-poc");
}
