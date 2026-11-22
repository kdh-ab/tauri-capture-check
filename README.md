# tauri-capture-check

Tauri 앱의 `contentProtected: true`가 Windows의 캡처·녹화 경로에서 실제로 창을 빼는지 GitHub Actions에서 잰다.
Windows에서 이 설정은 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`로 동작한다.

- `app/`: 빨간 보호 창(PROTECTED)과 초록 비교 창(CONTROL)을 띄우는 최소 Tauri 앱
- `capture_check.py`: 캡처 경로마다 화면을 받아 빨강·초록 점을 세고 판정한다
  - 초록이 찍히고 빨강이 없으면 차단됨, 둘 다 찍히면 차단 안 됨, 초록도 없으면 판정 불가
  - 경로: GDI BitBlt, PrintScreen 키, PrintWindow, DXGI 데스크톱 복제(스크린샷·녹화), Windows.Graphics.Capture(화면·창 녹화)
- `.github/workflows/measure.yml`: Windows Server 2022·2025와 Windows 11(ARM)에서 실행

결과는 실행 요약(Summary)과 아티팩트(캡처 사진, `results.json`, 테스트 앱 exe)로 남는다.
아티팩트의 exe를 실제 Windows 10·11 PC에서 띄우면 캡처 도구(Win+Shift+S)나 게임 바 녹화도 직접 확인할 수 있다.
