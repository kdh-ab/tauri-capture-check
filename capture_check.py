"""
Windows 캡처 차단 실측.

보호 창(PROTECTED, 빨강, contentProtected: true)과 비교 창(CONTROL, 초록)을 띄운 상태에서
캡처·녹화 경로마다 화면을 받아 빨강·초록 점을 센다.

- 초록이 찍히고 빨강이 없으면: 그 경로에서 보호 창이 빠졌다(차단됨).
- 둘 다 찍히면: 보호 창이 찍혔다(차단 안 됨).
- 초록도 없으면: 그 경로로 화면을 받지 못해 판정할 수 없다.

사용: python capture_check.py <결과 폴더>
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import platform
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageGrab

PROTECTED_TITLE = "PROTECTED"
CONTROL_TITLE = "CONTROL"
# 창 하나(420×320)가 보이면 약 10만 점이 잡힌다. 배경화면·아이콘의 붉은 점(수천 개)과 구분되도록 넉넉히 잡는다.
VISIBLE_MIN_PIXELS = 20000
WINDOW_WAIT_SECONDS = 90
PAINT_SETTLE_SECONDS = 5
STREAM_SECONDS = 3.0
STREAM_MAX_FRAMES = 60
CLIPBOARD_WAIT_SECONDS = 5
WDA_NAMES = {0x0: "WDA_NONE", 0x1: "WDA_MONITOR", 0x11: "WDA_EXCLUDEFROMCAPTURE"}
VK_SNAPSHOT = 0x2C
KEYEVENTF_KEYUP = 0x2
PW_RENDERFULLCONTENT = 0x2
SM_CXSCREEN, SM_CYSCREEN, SM_REMOTESESSION = 0, 1, 0x1000

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64비트에서 핸들이 잘리지 않도록 인자·반환 형식을 모두 지정한다.
user32.SetProcessDPIAware.restype = wt.BOOL
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.GetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowDisplayAffinity.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
kernel32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
kernel32.ProcessIdToSessionId.restype = wt.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


@dataclass(frozen=True)
class Counts:
    red: int
    green: int


@dataclass
class Result:
    key: str
    label: str
    verdict: str = ""
    red: int = 0
    green: int = 0
    frames: int = 0
    frames_with_red: int = 0
    frames_with_green: int = 0
    images: list[str] = field(default_factory=list)
    error: str = ""


def count_colors(rgb: np.ndarray) -> Counts:
    """보호 창 빨강(#d61f1f)과 비교 창 초록(#1f9d3a)에 가까운 점을 센다."""
    r, g, b = (rgb[..., i].astype(np.int16) for i in range(3))
    red = (r > 170) & (g < 90) & (b < 90)
    green = (g > 120) & (r < 90) & (b < 110) & (g - r > 60)
    return Counts(int(red.sum()), int(green.sum()))


def judge(red: int, green: int) -> str:
    if green < VISIBLE_MIN_PIXELS:
        return "판정 불가(비교 창도 안 찍힘)"
    return "차단 안 됨(보호 창이 찍힘)" if red >= VISIBLE_MIN_PIXELS else "차단됨(보호 창이 빠짐)"


def judge_stream(result: Result) -> str:
    if result.frames_with_green == 0:
        return "판정 불가(비교 창도 안 찍힘)"
    if result.frames_with_red > 0:
        return f"차단 안 됨({result.frames}프레임 중 {result.frames_with_red}개에 보호 창)"
    return f"차단됨({result.frames}프레임 모두 보호 창 없음)"


def save(rgb: np.ndarray, out: Path, name: str) -> str:
    path = out / f"{name}.png"
    Image.fromarray(np.ascontiguousarray(rgb[..., :3]).astype(np.uint8)).save(path)
    return path.name


# ---- 창 정보 -------------------------------------------------------------

def wait_for_window(title: str) -> int:
    deadline = time.monotonic() + WINDOW_WAIT_SECONDS
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    raise RuntimeError(f"{title} 창을 {WINDOW_WAIT_SECONDS}초 안에 찾지 못했습니다.")


def window_info(hwnd: int) -> dict[str, object]:
    affinity = wt.DWORD()
    ok = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "affinity": WDA_NAMES.get(affinity.value, hex(affinity.value)) if ok else f"조회 실패({ctypes.get_last_error()})",
        "rect": [rect.left, rect.top, rect.right, rect.bottom],
    }


def environment() -> dict[str, object]:
    session = wt.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session))
    versions = {}
    for package in ("mss", "dxcam", "windows-capture", "numpy", "pillow"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "없음"
    return {
        "os": platform.platform(),
        "build": sys.getwindowsversion().build,
        "screen": [user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)],
        "session_id": session.value,
        "remote_session": bool(user32.GetSystemMetrics(SM_REMOTESESSION)),
        "packages": versions,
    }


# ---- 캡처 경로 -----------------------------------------------------------

def gdi_screen() -> np.ndarray:
    """화면 DC를 BitBlt로 복사한다(오래된 캡처 도구·원격 지원 도구 일부)."""
    import mss

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return np.asarray(shot)[..., [2, 1, 0]]


def printscreen_key() -> np.ndarray:
    """PrintScreen 키를 눌러 클립보드에 들어온 화면을 읽는다."""
    if user32.OpenClipboard(None):
        user32.EmptyClipboard()
        user32.CloseClipboard()
    user32.keybd_event(VK_SNAPSHOT, 0, 0, 0)
    user32.keybd_event(VK_SNAPSHOT, 0, KEYEVENTF_KEYUP, 0)
    deadline = time.monotonic() + CLIPBOARD_WAIT_SECONDS
    while time.monotonic() < deadline:
        image = ImageGrab.grabclipboard()
        if isinstance(image, Image.Image):
            return np.asarray(image.convert("RGB"))
        time.sleep(0.2)
    raise RuntimeError("PrintScreen을 눌렀지만 클립보드에 화면이 들어오지 않았습니다.")


def print_window(hwnd: int) -> np.ndarray:
    """창 하나를 PrintWindow로 그려 받는다(창 단위 캡처)."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    try:
        gdi32.SelectObject(memory_dc, bitmap)
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            raise RuntimeError(f"PrintWindow 실패({ctypes.get_last_error()})")
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            raise RuntimeError("GetDIBits 실패")
        return np.frombuffer(buffer, np.uint8).reshape(height, width, 4)[..., [2, 1, 0]]
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def dxgi_camera():
    import dxcam

    camera = dxcam.create(output_color="RGB")
    if camera is None:
        raise RuntimeError("DXGI 데스크톱 복제 장치를 만들지 못했습니다.")
    return camera


def dxgi_screen(camera) -> np.ndarray:
    """DXGI 데스크톱 복제로 한 장 받는다(OBS 디스플레이 캡처 등)."""
    for _ in range(30):
        frame = camera.grab()
        if frame is not None:
            return frame
        time.sleep(0.1)
    raise RuntimeError("DXGI에서 프레임이 오지 않았습니다.")


def dxgi_stream(camera, on_frame: Callable[[np.ndarray], None]) -> None:
    """DXGI 데스크톱 복제로 STREAM_SECONDS 동안 녹화한다."""
    camera.start(target_fps=20, video_mode=True)
    try:
        deadline = time.monotonic() + STREAM_SECONDS
        frames = 0
        while time.monotonic() < deadline and frames < STREAM_MAX_FRAMES:
            frame = camera.get_latest_frame()
            if frame is not None:
                on_frame(frame)
                frames += 1
    finally:
        camera.stop()


def wgc_stream(on_frame: Callable[[np.ndarray], None], window_hwnd: int | None = None) -> None:
    """Windows.Graphics.Capture로 화면(또는 지정한 창)을 STREAM_SECONDS 동안 녹화한다."""
    from windows_capture import WindowsCapture

    capture = WindowsCapture(cursor_capture=None, draw_border=None, monitor_index=None, window_hwnd=window_hwnd)
    started = time.monotonic()
    state: dict[str, object] = {"frames": 0, "error": None}

    @capture.event
    def on_frame_arrived(frame, capture_control):
        on_frame(np.asarray(frame.frame_buffer)[..., [2, 1, 0]])
        state["frames"] = int(state["frames"]) + 1
        if time.monotonic() - started > STREAM_SECONDS or int(state["frames"]) >= STREAM_MAX_FRAMES:
            capture_control.stop()

    @capture.event
    def on_closed():
        return None

    def run() -> None:
        try:
            capture.start()
        except Exception as error:  # noqa: BLE001 - 측정 결과로 남긴다
            state["error"] = repr(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(STREAM_SECONDS + 15)
    if state["error"]:
        raise RuntimeError(str(state["error"]))
    if thread.is_alive():
        raise RuntimeError("녹화가 끝나지 않아 시간 초과로 멈췄습니다.")


# ---- 측정 ---------------------------------------------------------------

def measure_still(out: Path, key: str, label: str, grab: Callable[[], np.ndarray]) -> Result:
    result = Result(key, label)
    try:
        rgb = grab()
        counts = count_colors(rgb)
        result.red, result.green = counts.red, counts.green
        result.images.append(save(rgb, out, key))
        result.verdict = judge(counts.red, counts.green)
    except Exception:  # noqa: BLE001 - 경로마다 실패를 결과로 남긴다
        result.error = traceback.format_exc(limit=3)
        result.verdict = "오류"
    return result


def measure_per_window(out: Path, key: str, label: str, grab: Callable[[int], np.ndarray], hwnds: dict[str, int]) -> Result:
    """보호 창과 비교 창을 각각 따로 찍는다. 빨강은 보호 창 사진에서, 초록은 비교 창 사진에서 센다."""
    result = Result(key, label)
    try:
        protected = grab(hwnds[PROTECTED_TITLE])
        control = grab(hwnds[CONTROL_TITLE])
        result.red = count_colors(protected).red
        result.green = count_colors(control).green
        result.images += [save(protected, out, f"{key}_protected"), save(control, out, f"{key}_control")]
        result.verdict = judge(result.red, result.green)
    except Exception:  # noqa: BLE001
        result.error = traceback.format_exc(limit=3)
        result.verdict = "오류"
    return result


def measure_stream(out: Path, key: str, label: str, record: Callable[[Callable[[np.ndarray], None]], None]) -> Result:
    result = Result(key, label)
    first: list[np.ndarray] = []

    def on_frame(rgb: np.ndarray) -> None:
        counts = count_colors(rgb)
        result.frames += 1
        result.red = max(result.red, counts.red)
        result.green = max(result.green, counts.green)
        result.frames_with_red += counts.red >= VISIBLE_MIN_PIXELS
        result.frames_with_green += counts.green >= VISIBLE_MIN_PIXELS
        if not first:
            first.append(np.array(rgb, copy=True))

    try:
        record(on_frame)
        if first:
            result.images.append(save(first[0], out, f"{key}_first"))
        result.verdict = judge_stream(result) if result.frames else "판정 불가(프레임 없음)"
    except Exception:  # noqa: BLE001
        result.error = traceback.format_exc(limit=3)
        result.verdict = "오류"
    return result


def measure_window_streams(out: Path, hwnds: dict[str, int]) -> Result:
    """WGC로 보호 창과 비교 창을 각각 지정해 녹화한다."""
    protected = measure_stream(out, "wgc_window_protected", "", lambda f: wgc_stream(f, hwnds[PROTECTED_TITLE]))
    control = measure_stream(out, "wgc_window_control", "", lambda f: wgc_stream(f, hwnds[CONTROL_TITLE]))
    result = Result("wgc_window", "Windows.Graphics.Capture 창 지정 녹화")
    result.frames = protected.frames
    result.frames_with_red = protected.frames_with_red
    result.frames_with_green = control.frames_with_green
    result.red, result.green = protected.red, control.green
    result.images = protected.images + control.images
    result.error = "\n".join(e for e in (protected.error, control.error) if e)
    if protected.error or control.error:
        result.verdict = "오류"
    elif control.frames_with_green == 0:
        result.verdict = "판정 불가(비교 창도 안 찍힘)"
    elif protected.frames_with_red > 0:
        result.verdict = f"차단 안 됨({protected.frames}프레임 중 {protected.frames_with_red}개에 보호 창)"
    else:
        result.verdict = f"차단됨(보호 창 {protected.frames}프레임 모두 빨강 없음)"
    return result


def summary_markdown(env: dict[str, object], windows: dict[str, dict[str, object]], results: list[Result]) -> str:
    lines = [
        f"### Windows 캡처 차단 실측 — {env['os']} (build {env['build']})",
        "",
        f"- 화면 {env['screen'][0]}×{env['screen'][1]}, 세션 {env['session_id']}, 원격 세션 {env['remote_session']}",
        f"- 보호 창 display affinity: **{windows[PROTECTED_TITLE]['affinity']}**, 비교 창: {windows[CONTROL_TITLE]['affinity']}",
        "",
        "| 캡처 경로 | 보호 창(빨강 점) | 비교 창(초록 점) | 판정 |",
        "|---|---:|---:|---|",
    ]
    for r in results:
        lines.append(f"| {r.label} | {r.red:,} | {r.green:,} | {r.verdict} |")
    errors = [r for r in results if r.error]
    if errors:
        lines += ["", "<details><summary>오류 내용</summary>", ""]
        for r in errors:
            lines += [f"**{r.label}**", "```", r.error.strip(), "```"]
        lines += ["</details>"]
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("사용: python capture_check.py <결과 폴더>", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    user32.SetProcessDPIAware()

    env = environment()
    hwnds = {title: wait_for_window(title) for title in (PROTECTED_TITLE, CONTROL_TITLE)}
    time.sleep(PAINT_SETTLE_SECONDS)
    windows = {title: window_info(hwnd) for title, hwnd in hwnds.items()}

    results = [
        measure_still(out, "gdi_screen", "화면 전체 복사(GDI BitBlt)", gdi_screen),
        measure_still(out, "printscreen", "PrintScreen 키(클립보드)", printscreen_key),
        measure_per_window(out, "print_window", "창 단위 캡처(PrintWindow)", print_window, hwnds),
    ]
    try:
        camera = dxgi_camera()
        results.append(measure_still(out, "dxgi_screen", "데스크톱 복제(DXGI) 스크린샷", lambda: dxgi_screen(camera)))
        results.append(measure_stream(out, "dxgi_stream", "데스크톱 복제(DXGI) 녹화 3초", lambda f: dxgi_stream(camera, f)))
    except Exception:  # noqa: BLE001
        failed = Result("dxgi", "데스크톱 복제(DXGI)", verdict="오류", error=traceback.format_exc(limit=3))
        results.append(failed)
    results.append(measure_stream(out, "wgc_monitor", "Windows.Graphics.Capture 화면 녹화 3초", wgc_stream))
    results.append(measure_window_streams(out, hwnds))

    report = {"environment": env, "windows": windows, "results": [asdict(r) for r in results]}
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summary_markdown(env, windows, results)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
