#!/usr/bin/env python3
"""
scrcpy_touch_macro.py

Windows-oriented non-root Android touch macro recorder/replayer using:
- scrcpy for live streaming preview (read-only mode)
- adb shell input motionevent for injection/replay

This records gestures from a transparent overlay placed above the scrcpy window.
It does NOT passively capture physical touches made directly on the phone.

Features:
- Start/Stop Recording button in a small control window
- Replay speed control
- Optional position jitter and timing jitter on replay
- Replay interval, random interval range, multi-run replay, infinite replay, and auto-quit timer
- Named macro/profile save and load
- Optional auto-reconnect and replay restart after disconnect
- Overlay is tied to scrcpy; controls remain a normal window for compatibility
- Overlay is hidden while paused and shown as a semi-transparent always-on-top layer only during recording

Requirements:
- Windows
- Python 3.9+
- scrcpy installed (or scrcpy.exe path provided)
- adb available (or adb.exe path provided)
- USB debugging enabled and device authorized

Wi-Fi notes:
- `record-scrcpy` and `replay` also work over Wi-Fi if you pass `--serial IP:PORT`.
- This build adds `wifi-pair` and `wifi-connect` helper commands.

Stability notes:
- adb subprocess text output is decoded as UTF-8 with replacement to avoid Windows code-page crashes.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import random
import queue
import threading
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import ttk
except Exception as exc:
    print("Tkinter is required:", exc, file=sys.stderr)
    tk = None  # type: ignore

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    user32 = ctypes.windll.user32
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_NOOWNERZORDER = 0x0200
    GWL_HWNDPARENT = -8

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", wintypes.LONG),
            ("y", wintypes.LONG),
        ]


def _find_window_exact(title: str) -> int:
    if not IS_WINDOWS:
        raise RuntimeError("This recorder is Windows-only")
    hwnd = user32.FindWindowW(None, title)
    return int(hwnd)


def _client_rect_screen(hwnd: int) -> Tuple[int, int, int, int]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    pt = POINT(rect.left, rect.top)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        raise ctypes.WinError()
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return pt.x, pt.y, width, height


def _is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def _place_window_above(hwnd: int, below_hwnd: int) -> None:
    if not IS_WINDOWS:
        return
    if not hwnd or not below_hwnd:
        return
    try:
        user32.SetWindowPos(hwnd, below_hwnd, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception:
        pass


def _set_owner(hwnd: int, owner_hwnd: int) -> None:
    if not IS_WINDOWS:
        return
    if not hwnd or not owner_hwnd:
        return
    try:
        setter = getattr(user32, "SetWindowLongPtrW", None)
        if setter is None:
            setter = user32.SetWindowLongW
        setter(hwnd, GWL_HWNDPARENT, owner_hwnd)
        user32.SetWindowPos(
            hwnd,
            owner_hwnd,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )
    except Exception:
        pass


@dataclass
class MotionPoint:
    dt_ms: int
    action: str
    x: int
    y: int


def clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))



APP_STATE_DIR = Path.home() / ".scrcpy_touch_macro"
MACROS_DIR = APP_STATE_DIR / "macros"
PROFILES_DIR = APP_STATE_DIR / "profiles"


def ensure_app_dirs() -> None:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    MACROS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", name.strip())
    value = value.strip(" .")
    if not value:
        raise ValueError("Please enter a name")
    return value


def macro_path_for_name(name: str) -> Path:
    ensure_app_dirs()
    safe = sanitize_name(name)
    return MACROS_DIR / f"{safe}.json"


def profile_path_for_name(name: str) -> Path:
    ensure_app_dirs()
    safe = sanitize_name(name)
    return PROFILES_DIR / f"{safe}.json"


def list_saved_macro_names() -> List[str]:
    ensure_app_dirs()
    names: List[str] = []
    for path in sorted(MACROS_DIR.glob("*.json"), key=lambda p: p.name.lower()):
        names.append(path.stem)
    return names


def list_saved_profile_names() -> List[str]:
    ensure_app_dirs()
    names: List[str] = []
    for path in sorted(PROFILES_DIR.glob("*.json"), key=lambda p: p.name.lower()):
        names.append(path.stem)
    return names


def save_profile_data(name: str, payload: Dict[str, Any]) -> Path:
    path = profile_path_for_name(name)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_profile_data(name: str) -> Dict[str, Any]:
    path = profile_path_for_name(name)
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_macro(path: Path, device_size: tuple[int, int], points: List[MotionPoint]) -> None:
    payload = {
        "version": 4,
        "kind": "scrcpy_overlay_macro",
        "device_size": {"width": device_size[0], "height": device_size[1]},
        "points": [asdict(p) for p in points],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_macro(path: Path) -> tuple[tuple[int, int], List[MotionPoint]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    size = payload.get("device_size", {})
    points = [
        MotionPoint(
            dt_ms=int(p["dt_ms"]),
            action=str(p["action"]).upper(),
            x=int(p["x"]),
            y=int(p["y"]),
        )
        for p in payload["points"]
    ]
    return (int(size["width"]), int(size["height"])), points


def resolve_adb_path(explicit_adb: Optional[str] = None, scrcpy_path: Optional[str] = None) -> str:
    candidates: List[str] = []
    if explicit_adb:
        candidates.append(explicit_adb)
    env_adb = os.environ.get("ADB_PATH")
    if env_adb:
        candidates.append(env_adb)

    if scrcpy_path:
        scrcpy_dir = os.path.dirname(scrcpy_path)
        candidates.append(os.path.join(scrcpy_dir, "adb.exe"))
        candidates.append(os.path.join(scrcpy_dir, "adb"))

    candidates.append("adb")

    local = os.environ.get("LOCALAPPDATA")
    userprofile = os.environ.get("USERPROFILE")
    android_home = os.environ.get("ANDROID_HOME")
    android_sdk_root = os.environ.get("ANDROID_SDK_ROOT")

    common_bases = [p for p in [android_home, android_sdk_root] if p]
    if local:
        common_bases.append(os.path.join(local, "Android", "Sdk"))
    if userprofile:
        common_bases.extend(
            [
                os.path.join(userprofile, "AppData", "Local", "Android", "Sdk"),
                os.path.join(userprofile, "Android", "Sdk"),
            ]
        )

    for base in common_bases:
        candidates.append(os.path.join(base, "platform-tools", "adb.exe"))
        candidates.append(os.path.join(base, "platform-tools", "adb"))

    for cand in candidates:
        if not cand:
            continue
        found = shutil.which(cand)
        if found:
            return found
        if os.path.isfile(cand):
            return cand

    checked = "\n  - ".join(candidates)
    raise FileNotFoundError(
        "Could not find adb. Install Android SDK Platform-Tools and either add adb to PATH, "
        "set ADB_PATH, pass --adb C:\\path\\to\\adb.exe, or point --scrcpy to a scrcpy folder containing adb.exe.\n"
        f"Checked:\n  - {checked}"
    )


def resolve_scrcpy_path(explicit_scrcpy: Optional[str] = None) -> str:
    candidates: List[str] = []
    if explicit_scrcpy:
        candidates.append(explicit_scrcpy)
    env_scrcpy = os.environ.get("SCRCPY_PATH")
    if env_scrcpy:
        candidates.append(env_scrcpy)
    candidates.append("scrcpy")
    candidates.append("scrcpy.exe")

    cwd = os.getcwd()
    candidates.extend(
        [
            os.path.join(cwd, "scrcpy.exe"),
            os.path.join(cwd, "scrcpy", "scrcpy.exe"),
        ]
    )

    for cand in candidates:
        if not cand:
            continue
        found = shutil.which(cand)
        if found:
            return found
        if os.path.isfile(cand):
            return cand

    checked = "\n  - ".join(candidates)
    raise FileNotFoundError(
        "Could not find scrcpy. Download the official Windows release and either add it to PATH, "
        "set SCRCPY_PATH, or pass --scrcpy C:\\path\\to\\scrcpy.exe.\n"
        f"Checked:\n  - {checked}"
    )


def run_adb(args: List[str], serial: Optional[str] = None, binary: bool = False, adb_path: Optional[str] = None) -> subprocess.CompletedProcess:
    cmd = [resolve_adb_path(adb_path)]
    if serial:
        cmd += ["-s", serial]
    cmd += args

    if binary:
        return subprocess.run(cmd, capture_output=True, text=False, check=False)

    # Decode adb output explicitly instead of relying on the Windows locale
    # default (for example GBK), which can raise UnicodeDecodeError when adb
    # emits bytes outside that code page.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def adb_shell(cmd: str, serial: Optional[str] = None, adb_path: Optional[str] = None) -> subprocess.CompletedProcess:
    return run_adb(["shell", cmd], serial=serial, binary=False, adb_path=adb_path)


def check_device(serial: Optional[str] = None, adb_path: Optional[str] = None) -> None:
    cp = run_adb(["devices"], adb_path=adb_path)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "adb devices failed")
    lines = [line.strip() for line in cp.stdout.splitlines()[1:] if line.strip()]
    online = [line for line in lines if line.endswith("\tdevice")]
    if serial:
        if not any(line.split("\t", 1)[0] == serial for line in online):
            raise RuntimeError(f"Device {serial!r} not found or not authorized.\nadb devices:\n{cp.stdout}")
    elif not online:
        raise RuntimeError("No connected device found. Check USB debugging, cable mode, and authorization.\n" + cp.stdout)


def list_adb_devices(adb_path: Optional[str] = None) -> List[Tuple[str, str]]:
    cp = run_adb(["devices"], adb_path=adb_path)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "adb devices failed")
    rows: List[Tuple[str, str]] = []
    for line in cp.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        rows.append((serial.strip(), state.strip()))
    return rows


def choose_connected_usb_serial(preferred_serial: Optional[str] = None, adb_path: Optional[str] = None) -> str:
    rows = list_adb_devices(adb_path=adb_path)
    ready = [(serial, state) for serial, state in rows if state == "device"]

    if preferred_serial:
        for serial, state in ready:
            if serial == preferred_serial:
                return serial
        raise RuntimeError(f"Device {preferred_serial!r} is not connected and ready.\nCurrent adb devices:\n{rows}")

    usb_like = [serial for serial, _state in ready if ":" not in serial]
    if len(usb_like) == 1:
        return usb_like[0]
    if len(usb_like) > 1:
        raise RuntimeError(
            "Multiple USB devices are connected. Pass --serial to choose one.\n"
            + "\n".join(f"  - {serial}" for serial in usb_like)
        )

    if len(ready) == 1:
        return ready[0][0]

    raise RuntimeError("No single connected device could be selected automatically. Connect one device over USB or pass --serial.")


def adb_pair(pair_target: str, pairing_code: str, adb_path: Optional[str] = None) -> str:
    cp = run_adb(["pair", pair_target, pairing_code], adb_path=adb_path)
    output = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RuntimeError(output.strip() or f"adb pair {pair_target} failed")
    return output.strip()


def adb_tcpip(port: int, serial: Optional[str], adb_path: Optional[str] = None) -> str:
    cp = run_adb(["tcpip", str(port)], serial=serial, adb_path=adb_path)
    output = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RuntimeError(output.strip() or f"adb tcpip {port} failed")
    return output.strip()


def adb_connect(connect_target: str, adb_path: Optional[str] = None) -> str:
    cp = run_adb(["connect", connect_target], adb_path=adb_path)
    output = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode != 0:
        raise RuntimeError(output.strip() or f"adb connect {connect_target} failed")
    return output.strip()


def get_device_ip(serial: Optional[str], adb_path: Optional[str] = None) -> str:
    candidates = [
        "ip route",
        "ip addr show wlan0",
        "ip addr show ap0",
        "ifconfig wlan0",
        "ifconfig",
    ]
    ipv4_pattern = re.compile(r"(?:src|inet)\s+(\d+\.\d+\.\d+\.\d+)")
    bare_ipv4_pattern = re.compile(r"\b(\d+\.\d+\.\d+\.\d+)\b")

    for cmd in candidates:
        cp = adb_shell(cmd, serial=serial, adb_path=adb_path)
        if cp.returncode != 0:
            continue
        body = cp.stdout or ""
        for match in ipv4_pattern.finditer(body):
            ip = match.group(1)
            if not ip.startswith("127."):
                return ip
        for match in bare_ipv4_pattern.finditer(body):
            ip = match.group(1)
            if not ip.startswith("127."):
                return ip
    raise RuntimeError("Could not detect the device IP automatically. Pass --ip explicitly from the phone's Wi-Fi / Wireless debugging screen.")


def wifi_connect_helper(
    adb_path: Optional[str],
    ip: Optional[str],
    port: int,
    serial: Optional[str],
    enable_tcpip: bool,
) -> Tuple[str, str]:
    if port <= 0 or port > 65535:
        raise ValueError("--port must be between 1 and 65535")

    active_serial = serial
    messages: List[str] = []

    if enable_tcpip:
        active_serial = choose_connected_usb_serial(preferred_serial=serial, adb_path=adb_path)
        messages.append(f"Using device: {active_serial}")
        messages.append(adb_tcpip(port=port, serial=active_serial, adb_path=adb_path))
        if not ip:
            ip = get_device_ip(serial=active_serial, adb_path=adb_path)
            messages.append(f"Detected device IP: {ip}")

    if not ip:
        raise ValueError("An IP address is required unless you use --enable-tcpip with a connected device.")

    target = f"{ip}:{port}"
    messages.append(adb_connect(target, adb_path=adb_path))
    return target, "\n".join(messages)


def get_current_display_size(serial: Optional[str] = None, adb_path: Optional[str] = None) -> tuple[int, int]:
    cp = adb_shell("wm size", serial=serial, adb_path=adb_path)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "adb shell wm size failed")
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", cp.stdout)
    if not m:
        m = re.search(r"Override size:\s*(\d+)x(\d+)", cp.stdout)
    if not m:
        m = re.search(r"(\d+)x(\d+)", cp.stdout)
    if not m:
        raise RuntimeError(f"Could not parse wm size output: {cp.stdout!r}")
    w, h = int(m.group(1)), int(m.group(2))

    cp2 = adb_shell("dumpsys input", serial=serial, adb_path=adb_path)
    if cp2.returncode == 0:
        m2 = re.search(r"SurfaceOrientation:\s*(\d+)", cp2.stdout)
        if m2 and int(m2.group(1)) in (1, 3):
            return h, w
    return w, h


def send_motionevent(action: str, x: int, y: int, serial: Optional[str] = None, adb_path: Optional[str] = None, dry_run: bool = False) -> None:
    if action not in {"DOWN", "MOVE", "UP"}:
        raise ValueError(f"Unsupported action: {action}")
    cmd = f"input motionevent {action} {int(x)} {int(y)}"
    if dry_run:
        print(cmd)
        return
    cp = adb_shell(cmd, serial=serial, adb_path=adb_path)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or f"Failed: {cmd}")


def launch_scrcpy(scrcpy_path: str, serial: Optional[str], title: str, extra_args: List[str]) -> subprocess.Popen:
    cmd = [scrcpy_path, "--no-control", "--window-title", title]
    if serial:
        cmd += ["--serial", serial]
    cmd += extra_args
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fit_rect(box_w: int, box_h: int, content_w: int, content_h: int) -> Tuple[int, int, int, int]:
    if box_w <= 0 or box_h <= 0 or content_w <= 0 or content_h <= 0:
        return 0, 0, 0, 0
    scale = min(box_w / content_w, box_h / content_h)
    w = int(round(content_w * scale))
    h = int(round(content_h * scale))
    x = (box_w - w) // 2
    y = (box_h - h) // 2
    return x, y, w, h


def validate_interval_range(interval_min_s: Optional[float], interval_max_s: Optional[float]) -> None:
    if (interval_min_s is None) != (interval_max_s is None):
        raise ValueError("Set both interval min and interval max, or leave both blank")
    if interval_min_s is not None and interval_max_s is not None:
        if interval_min_s < 0 or interval_max_s < 0:
            raise ValueError("Random interval values must be >= 0")
        if interval_min_s > interval_max_s:
            raise ValueError("Interval min must be <= interval max")


def pick_interval(
    rng: random.Random,
    interval_s: float,
    interval_min_s: Optional[float],
    interval_max_s: Optional[float],
) -> float:
    validate_interval_range(interval_min_s, interval_max_s)
    if interval_min_s is not None and interval_max_s is not None:
        return rng.uniform(interval_min_s, interval_max_s)
    return max(0.0, interval_s)



def _sleep_interruptible(
    seconds: float,
    started_at: float,
    auto_quit_s: Optional[float],
    stop_event: Optional[threading.Event] = None,
    on_tick=None,
) -> Optional[str]:
    if seconds <= 0:
        overall_remaining = None if auto_quit_s is None else max(0.0, auto_quit_s - (time.monotonic() - started_at))
        if on_tick is not None:
            on_tick(0.0, overall_remaining)
        return None

    end_time = time.monotonic() + seconds
    while True:
        now = time.monotonic()
        overall_remaining = None if auto_quit_s is None else max(0.0, auto_quit_s - (now - started_at))

        if stop_event is not None and stop_event.is_set():
            if on_tick is not None:
                on_tick(0.0, overall_remaining)
            return "stopped"

        if auto_quit_s is not None and now - started_at >= auto_quit_s:
            if on_tick is not None:
                on_tick(0.0, 0.0)
            return "timer"

        remaining = end_time - now
        if on_tick is not None:
            on_tick(max(0.0, remaining), overall_remaining)

        if remaining <= 0:
            return None
        time.sleep(min(0.05, remaining))


def _format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "—"
    value = max(0.0, float(value))
    if value >= 60:
        mins = int(value // 60)
        secs = value - mins * 60
        return f"{mins}m {secs:04.1f}s"
    return f"{value:.1f}s"


def replay_macro(
    path: Path,
    serial: Optional[str] = None,
    speed: float = 1.0,
    dry_run: bool = False,
    adb_path: Optional[str] = None,
    jitter_px: int = 0,
    timing_jitter_ms: int = 0,
    repeat_count: int = 1,
    interval_s: float = 0.0,
    interval_min_s: Optional[float] = None,
    interval_max_s: Optional[float] = None,
    infinite: bool = False,
    auto_quit_s: Optional[float] = None,
    seed: Optional[int] = None,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if speed <= 0:
        raise ValueError("--speed must be > 0")
    if jitter_px < 0 or timing_jitter_ms < 0:
        raise ValueError("jitter values must be >= 0")
    if repeat_count < 1:
        raise ValueError("--repeat-count must be >= 1")
    if interval_s < 0:
        raise ValueError("--interval-s must be >= 0")
    if auto_quit_s is not None and auto_quit_s <= 0:
        raise ValueError("--auto-quit-s must be > 0")
    validate_interval_range(interval_min_s, interval_max_s)

    def emit(kind: str, **kwargs) -> None:
        if progress_callback is None:
            return
        try:
            payload = {"kind": kind}
            payload.update(kwargs)
            progress_callback(payload)
        except Exception:
            pass

    check_device(serial=serial, adb_path=adb_path)
    macro_size, points = load_macro(path)
    current_size = get_current_display_size(serial=serial, adb_path=adb_path)

    if not points:
        raise RuntimeError("Macro is empty")

    if macro_size != current_size:
        print(
            f"Warning: macro was recorded for {macro_size[0]}x{macro_size[1]}, current display is "
            f"{current_size[0]}x{current_size[1]}. Coordinates will be scaled.",
            file=sys.stderr,
        )

    sx = current_size[0] / macro_size[0]
    sy = current_size[1] / macro_size[1]
    rng = random.Random(seed)
    started_at = time.monotonic()
    total_runs = None if infinite else repeat_count

    runs_completed = 0
    events_sent = 0
    stopped_by_timer = False
    stopped_by_user = False

    emit(
        "start",
        total_runs=total_runs,
        auto_quit_s=auto_quit_s,
        speed=speed,
        jitter_px=jitter_px,
        timing_jitter_ms=timing_jitter_ms,
    )

    while infinite or runs_completed < repeat_count:
        if stop_event is not None and stop_event.is_set():
            stopped_by_user = True
            break

        current_run = runs_completed + 1
        emit(
            "run_start",
            current_run=current_run,
            total_runs=total_runs,
            auto_quit_remaining_s=None if auto_quit_s is None else max(0.0, auto_quit_s - (time.monotonic() - started_at)),
        )

        prev_dt = 0
        for point_index, p in enumerate(points, start=1):
            if stop_event is not None and stop_event.is_set():
                stopped_by_user = True
                break

            if auto_quit_s is not None and time.monotonic() - started_at >= auto_quit_s:
                stopped_by_timer = True
                break

            base_delay = max(0.0, (p.dt_ms - prev_dt) / 1000.0 / speed)
            if timing_jitter_ms:
                base_delay += rng.uniform(-timing_jitter_ms, timing_jitter_ms) / 1000.0
            delay = max(0.0, base_delay)

            reason = _sleep_interruptible(
                delay,
                started_at,
                auto_quit_s,
                stop_event=stop_event,
                on_tick=lambda rem, overall, current_run=current_run, point_index=point_index: emit(
                    "tick",
                    phase="running",
                    current_run=current_run,
                    total_runs=total_runs,
                    point_index=point_index,
                    countdown_s=rem,
                    auto_quit_remaining_s=overall,
                ),
            )
            if reason == "timer":
                stopped_by_timer = True
                break
            if reason == "stopped":
                stopped_by_user = True
                break

            x = round(p.x * sx)
            y = round(p.y * sy)

            if not hasattr(replay_macro, "_press_jitter_dx"):
                replay_macro._press_jitter_dx = 0
                replay_macro._press_jitter_dy = 0

            if p.action == "DOWN":
                if jitter_px:
                    replay_macro._press_jitter_dx = rng.randint(-jitter_px, jitter_px)
                    replay_macro._press_jitter_dy = rng.randint(-jitter_px, jitter_px)
                else:
                    replay_macro._press_jitter_dx = 0
                    replay_macro._press_jitter_dy = 0

            x += replay_macro._press_jitter_dx
            y += replay_macro._press_jitter_dy

            x = clamp_int(x, 0, current_size[0] - 1)
            y = clamp_int(y, 0, current_size[1] - 1)

            send_motionevent(p.action, x, y, serial=serial, adb_path=adb_path, dry_run=dry_run)

            if p.action == "UP":
                replay_macro._press_jitter_dx = 0
                replay_macro._press_jitter_dy = 0

            prev_dt = p.dt_ms
            events_sent += 1

        if stopped_by_timer or stopped_by_user:
            break

        runs_completed += 1
        emit(
            "run_complete",
            runs_completed=runs_completed,
            total_runs=total_runs,
            auto_quit_remaining_s=None if auto_quit_s is None else max(0.0, auto_quit_s - (time.monotonic() - started_at)),
        )

        if not infinite and runs_completed >= repeat_count:
            break

        next_gap = pick_interval(rng, interval_s, interval_min_s, interval_max_s)
        emit(
            "interval_start",
            runs_completed=runs_completed,
            next_run=runs_completed + 1,
            total_runs=total_runs,
            countdown_s=next_gap,
            auto_quit_remaining_s=None if auto_quit_s is None else max(0.0, auto_quit_s - (time.monotonic() - started_at)),
        )
        reason = _sleep_interruptible(
            next_gap,
            started_at,
            auto_quit_s,
            stop_event=stop_event,
            on_tick=lambda rem, overall, runs_completed=runs_completed: emit(
                "tick",
                phase="interval",
                runs_completed=runs_completed,
                next_run=runs_completed + 1,
                total_runs=total_runs,
                countdown_s=rem,
                auto_quit_remaining_s=overall,
            ),
        )
        if reason == "timer":
            stopped_by_timer = True
            break
        if reason == "stopped":
            stopped_by_user = True
            break

    elapsed = time.monotonic() - started_at
    interval_desc = (
        f"{interval_min_s:.3f}-{interval_max_s:.3f}s random"
        if interval_min_s is not None and interval_max_s is not None
        else f"{interval_s:.3f}s fixed"
    )
    suffix = ""
    if stopped_by_timer:
        suffix = " (stopped by auto-quit timer)"
    elif stopped_by_user:
        suffix = " (stopped by user)"

    emit(
        "finish",
        runs_completed=runs_completed,
        total_runs=total_runs,
        events_sent=events_sent,
        elapsed=elapsed,
        stopped_by_timer=stopped_by_timer,
        stopped_by_user=stopped_by_user,
        interval_desc=interval_desc,
    )

    print(
        f"Replayed {events_sent} events across {runs_completed} run(s) in {elapsed:.2f}s "
        f"(speed={speed}, jitter_px={jitter_px}, timing_jitter_ms={timing_jitter_ms}, interval={interval_desc})"
        f"{suffix}"
    )


class ScrcpyRecorderGUI:
    def __init__(
        self,
        output: Path,
        scrcpy_path: str,
        adb_path: str,
        serial: Optional[str],
        title: str,
        window_width: Optional[int],
        no_audio: bool,
        default_jitter_px: int,
        default_timing_jitter_ms: int,
        default_speed: float,
        default_repeat_count: int,
        default_interval_s: float,
        default_interval_min_s: Optional[float],
        default_interval_max_s: Optional[float],
        default_infinite: bool,
        default_auto_quit_s: Optional[float],
    ):
        if not IS_WINDOWS:
            raise RuntimeError("This recorder is Windows-only")
        if tk is None:
            raise RuntimeError("Tkinter is required")

        ensure_app_dirs()
        self.output = output
        self.scrcpy_path = scrcpy_path
        self.adb_path = adb_path
        self.serial = serial
        self.title = title
        self.window_width = window_width
        self.no_audio = no_audio
        self.points: List[MotionPoint] = []
        self.recording_start: Optional[float] = None
        self.recording_enabled = False
        self.dragging = False
        self.last_move_sent = 0.0
        self.move_interval_s = 0.010
        self.last_device_point: Optional[Tuple[int, int]] = None
        self.scrcpy_proc: Optional[subprocess.Popen] = None
        self.hwnd: Optional[int] = None
        self.client_x = 0
        self.client_y = 0
        self.client_w = 1
        self.client_h = 1
        self.content_x = 0
        self.content_y = 0
        self.content_w = 1
        self.content_h = 1
        self.device_w, self.device_h = get_current_display_size(serial=self.serial, adb_path=self.adb_path)

        self.replay_running = False
        self.replay_thread: Optional[threading.Thread] = None
        self.replay_stop_event = threading.Event()
        self.replay_queue: "queue.Queue[dict]" = queue.Queue()
        self.scrcpy_transitioning = False
        self.last_connected_target: Optional[str] = None
        self.scrcpy_reconnect_deadline: float = 0.0
        self.scrcpy_last_restart_attempt: float = 0.0

        extra_args: List[str] = []
        if self.window_width:
            extra_args += ["--window-width", str(self.window_width)]
        if self.no_audio:
            extra_args += ["--no-audio"]

        self.scrcpy_proc = launch_scrcpy(self.scrcpy_path, self.serial, self.title, extra_args)
        self.hwnd = self._wait_for_window(self.title, timeout=8.0)

        self.root = tk.Tk()
        self.root.title("Recorder Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)
        self.root.attributes("-alpha", 0.18)
        self.root.configure(bg="#202020")

        self.canvas = tk.Canvas(
            self.root,
            highlightthickness=0,
            bg="#202020",
            cursor="crosshair",
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.ctrl = tk.Toplevel(self.root)
        self.ctrl.title("Macro Controls")
        self.ctrl.attributes("-topmost", False)
        self.ctrl.resizable(False, False)
        self.ctrl.geometry("460x760")

        self.status_var = tk.StringVar(value="Paused")
        self.replay_status_var = tk.StringVar(value="Idle")
        self.run_counter_var = tk.StringVar(value="0 / 0")
        self.countdown_var = tk.StringVar(value="—")
        self.auto_quit_left_var = tk.StringVar(value="—")

        self.speed_var = tk.StringVar(value=str(default_speed))
        self.repeat_count_var = tk.StringVar(value=str(default_repeat_count))
        self.interval_s_var = tk.StringVar(value=str(default_interval_s))
        self.interval_min_s_var = tk.StringVar(value="" if default_interval_min_s is None else str(default_interval_min_s))
        self.interval_max_s_var = tk.StringVar(value="" if default_interval_max_s is None else str(default_interval_max_s))
        self.infinite_var = tk.BooleanVar(value=default_infinite)
        self.auto_quit_s_var = tk.StringVar(value="" if default_auto_quit_s is None else str(default_auto_quit_s))
        self.jitter_px_var = tk.StringVar(value=str(default_jitter_px))
        self.timing_jitter_var = tk.StringVar(value=str(default_timing_jitter_ms))

        self.active_serial_var = tk.StringVar(value=self.serial or "(auto)")
        self.wifi_pair_target_var = tk.StringVar(value="")
        self.wifi_pair_code_var = tk.StringVar(value="")
        self.wifi_ip_var = tk.StringVar(value="")
        self.wifi_port_var = tk.StringVar(value="5555")
        self.wifi_enable_tcpip_var = tk.BooleanVar(value=False)
        self.wifi_target_var = tk.StringVar(value="")
        self.wifi_status_var = tk.StringVar(value="Idle")

        self.profile_name_var = tk.StringVar(value="default")
        self.macro_name_var = tk.StringVar(value=self.output.stem)
        self.saved_macro_names = list_saved_macro_names()
        self.saved_profile_names = list_saved_profile_names()
        self.library_status_var = tk.StringVar(value=f"Using {self.output.name}")
        self.auto_reconnect_var = tk.BooleanVar(value=False)
        self.reconnect_delay_s_var = tk.StringVar(value="3")
        self.auto_reconnect_status_var = tk.StringVar(value="Disabled")
        self.last_replay_config: Optional[Dict[str, Any]] = None
        self.pending_reconnect_target: Optional[str] = None
        self.pending_replay_restart = False
        self.replay_restart_count = 0
        self.startup_restack_until = time.monotonic() + 3.0
        self.overlay_owner_bound = False

        self._build_controls()

        self.root.bind("<Escape>", self.on_quit)
        self.root.bind("<KeyPress-q>", self.on_quit)
        self.root.bind("<KeyPress-Q>", self.on_quit)
        self.root.bind("<KeyPress-s>", self.on_save)
        self.root.bind("<KeyPress-S>", self.on_save)
        self.root.bind("<KeyPress-c>", self.on_clear)
        self.root.bind("<KeyPress-C>", self.on_clear)
        self.root.bind("<KeyPress-p>", self.on_replay)
        self.root.bind("<KeyPress-P>", self.on_replay)
        self.root.bind("<space>", self.toggle_recording)

        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)

        self.ctrl.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

        self.overlay_owner_bound = False
        self._refresh_geometry()
        self._position_control_window()
        self._draw_overlay()
        try:
            self.root.withdraw()
        except Exception:
            pass
        self.root.after(60, self._poll)


    def _build_controls(self) -> None:
        outer = tk.Frame(self.ctrl, padx=10, pady=10)
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        tab_main = tk.Frame(self.notebook, padx=10, pady=10)
        tab_library = tk.Frame(self.notebook, padx=10, pady=10)
        tab_wifi = tk.Frame(self.notebook, padx=10, pady=10)

        self.notebook.add(tab_main, text="Main")
        self.notebook.add(tab_library, text="Library")
        self.notebook.add(tab_wifi, text="ADB Wi-Fi")

        # Main tab
        status_box = tk.LabelFrame(tab_main, text="Status", padx=8, pady=8)
        status_box.pack(fill="x")

        status_grid = tk.Frame(status_box)
        status_grid.pack(fill="x")
        status_grid.grid_columnconfigure(1, weight=1)

        status_rows = [
            ("Recording", lambda parent: self._make_button(parent, "Start Recording", self.toggle_recording, attr_name="record_btn")),
            ("Status", lambda parent: tk.Label(parent, textvariable=self.status_var, anchor="w")),
            ("Replay status", lambda parent: tk.Label(parent, textvariable=self.replay_status_var, anchor="w")),
            ("Run counter", lambda parent: tk.Label(parent, textvariable=self.run_counter_var, anchor="w")),
            ("Countdown", lambda parent: tk.Label(parent, textvariable=self.countdown_var, anchor="w")),
            ("Auto-quit left", lambda parent: tk.Label(parent, textvariable=self.auto_quit_left_var, anchor="w")),
        ]
        for row, (label_text, widget_factory) in enumerate(status_rows):
            tk.Label(status_grid, text=label_text).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 6, 0))
            widget = widget_factory(status_grid)
            widget.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=(0 if row == 0 else 6, 0))

        replay_box = tk.LabelFrame(tab_main, text="Replay", padx=8, pady=8)
        replay_box.pack(fill="x", pady=(10, 0))
        replay_grid = tk.Frame(replay_box)
        replay_grid.pack(fill="x")
        replay_grid.grid_columnconfigure(1, weight=1)
        replay_grid.grid_columnconfigure(3, weight=1)

        fields = [
            ("Replay speed", self.speed_var, 0, 0),
            ("Repeat count", self.repeat_count_var, 0, 2),
            ("Interval seconds", self.interval_s_var, 1, 0),
            ("Infinite replay", self.infinite_var, 1, 2),
            ("Random interval min s", self.interval_min_s_var, 2, 0),
            ("Auto-quit after s", self.auto_quit_s_var, 2, 2),
            ("Random interval max s", self.interval_max_s_var, 3, 0),
            ("Position jitter px (per press)", self.jitter_px_var, 3, 2),
            ("Timing jitter ms", self.timing_jitter_var, 4, 0),
        ]
        for label_text, var, row, col in fields:
            tk.Label(replay_grid, text=label_text).grid(row=row, column=col, sticky="w", pady=(0 if row == 0 else 6, 0))
            if isinstance(var, tk.BooleanVar):
                tk.Checkbutton(replay_grid, variable=var, onvalue=True, offvalue=False).grid(
                    row=row, column=col + 1, sticky="w", padx=(8, 0), pady=(0 if row == 0 else 6, 0)
                )
            else:
                tk.Entry(replay_grid, textvariable=var, width=14).grid(
                    row=row, column=col + 1, sticky="ew", padx=(8, 12 if col == 0 else 0), pady=(0 if row == 0 else 6, 0)
                )

        reconnect_box = tk.LabelFrame(tab_main, text="Reconnect", padx=8, pady=8)
        reconnect_box.pack(fill="x", pady=(10, 0))
        reconnect_grid = tk.Frame(reconnect_box)
        reconnect_grid.pack(fill="x")
        reconnect_grid.grid_columnconfigure(1, weight=1)
        tk.Label(reconnect_grid, text="Auto reconnect").grid(row=0, column=0, sticky="w")
        tk.Checkbutton(reconnect_grid, variable=self.auto_reconnect_var, onvalue=True, offvalue=False).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        tk.Label(reconnect_grid, text="Retry delay s").grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(reconnect_grid, textvariable=self.reconnect_delay_s_var, width=14).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0)
        )
        tk.Label(reconnect_box, textvariable=self.auto_reconnect_status_var, justify="left", wraplength=400, fg="#444").pack(
            fill="x", pady=(8, 0)
        )

        btns = tk.Frame(tab_main)
        btns.pack(fill="x", pady=(10, 0))
        tk.Button(btns, text="Save", width=10, command=self.on_save).pack(side="left")
        tk.Button(btns, text="Clear", width=10, command=self.on_clear).pack(side="left", padx=6)
        self.replay_btn = tk.Button(btns, text="Replay", width=10, command=self.on_replay)
        self.replay_btn.pack(side="left")
        self.stop_replay_btn = tk.Button(btns, text="Stop", width=10, command=self.stop_replay, state=tk.DISABLED)
        self.stop_replay_btn.pack(side="left", padx=6)
        tk.Button(btns, text="Quit", width=10, command=self.on_quit).pack(side="right")

        tk.Label(
            tab_main,
            text="Shortcuts: Space start/stop | S save | C clear | P replay | Q quit",
            justify="left",
            wraplength=420,
            fg="#444",
        ).pack(fill="x", pady=(10, 0))

        # Library tab
        library = tk.Frame(tab_library)
        library.pack(fill="both", expand=True)
        library.grid_columnconfigure(1, weight=1)

        tk.Label(library, text="Macro").grid(row=0, column=0, sticky="w")
        self.macro_combo = ttk.Combobox(library, textvariable=self.macro_name_var, values=self.saved_macro_names, width=28)
        self.macro_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.macro_combo.bind("<<ComboboxSelected>>", self.on_macro_selected)

        macro_btns = tk.Frame(library)
        macro_btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Button(macro_btns, text="New Macro", width=11, command=self.on_new_macro).pack(side="left")
        tk.Button(macro_btns, text="Save Macro", width=11, command=self.on_save_named_macro).pack(side="left", padx=6)
        tk.Button(macro_btns, text="Load Macro", width=11, command=self.on_load_named_macro).pack(side="left")

        tk.Label(library, text="Profile").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.profile_combo = ttk.Combobox(library, textvariable=self.profile_name_var, values=self.saved_profile_names, width=28)
        self.profile_combo.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(12, 0))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        profile_btns = tk.Frame(library)
        profile_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Button(profile_btns, text="Save Profile", width=11, command=self.on_save_profile).pack(side="left")
        tk.Button(profile_btns, text="Load Profile", width=11, command=self.on_load_profile).pack(side="left", padx=6)
        tk.Button(profile_btns, text="Refresh Lists", width=11, command=self.refresh_library_lists).pack(side="left")

        tk.Label(
            library,
            text="Choose an existing item from the dropdown, or type a new name before saving.",
            justify="left",
            wraplength=420,
            fg="#444",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        tk.Label(library, textvariable=self.library_status_var, justify="left", wraplength=420, fg="#444").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        # Wi-Fi tab
        wifi = tk.Frame(tab_wifi)
        wifi.pack(fill="both", expand=True)
        wifi.grid_columnconfigure(1, weight=1)

        tk.Label(wifi, text="Active serial").grid(row=0, column=0, sticky="w")
        tk.Label(wifi, textvariable=self.active_serial_var, anchor="w").grid(row=0, column=1, sticky="ew", padx=(8, 0))

        tk.Label(wifi, text="Pair target").grid(row=1, column=0, sticky="w", pady=(8, 0))
        tk.Entry(wifi, textvariable=self.wifi_pair_target_var, width=28).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        tk.Label(wifi, text="Pair code").grid(row=2, column=0, sticky="w")
        tk.Entry(wifi, textvariable=self.wifi_pair_code_var, width=28).grid(row=2, column=1, sticky="ew", padx=(8, 0))

        pair_btns = tk.Frame(wifi)
        pair_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Button(pair_btns, text="Pair", width=12, command=self.on_wifi_pair).pack(side="left")

        tk.Label(wifi, text="Connect IP").grid(row=4, column=0, sticky="w", pady=(12, 0))
        tk.Entry(wifi, textvariable=self.wifi_ip_var, width=28).grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=(12, 0))

        tk.Label(wifi, text="Connect port").grid(row=5, column=0, sticky="w", pady=(8, 0))
        tk.Entry(wifi, textvariable=self.wifi_port_var, width=28).grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        tk.Label(wifi, text="Connected target").grid(row=6, column=0, sticky="w")
        tk.Entry(wifi, textvariable=self.wifi_target_var, width=28).grid(row=6, column=1, sticky="ew", padx=(8, 0))

        tk.Label(wifi, text="USB -> TCP/IP").grid(row=7, column=0, sticky="w", pady=(8, 0))
        tk.Checkbutton(wifi, variable=self.wifi_enable_tcpip_var, onvalue=True, offvalue=False).grid(
            row=7, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        wifi_btns = tk.Frame(wifi)
        wifi_btns.grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Button(wifi_btns, text="Connect", width=12, command=self.on_wifi_connect).pack(side="left")
        tk.Button(wifi_btns, text="Use Target", width=12, command=self.on_use_wifi_target).pack(side="left", padx=6)
        tk.Button(wifi_btns, text="Reconnect View", width=12, command=self.on_reconnect_view).pack(side="left")

        tk.Label(
            wifi,
            text="Tip: after switching to a Wi-Fi target, unplug USB only after Active serial shows the IP:port target.",
            justify="left",
            wraplength=420,
            fg="#444",
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))
        tk.Label(wifi, textvariable=self.wifi_status_var, justify="left", wraplength=420, fg="#444").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

    def _make_button(self, parent, text, command, attr_name=None):
        btn = tk.Button(parent, text=text, width=18, command=command)
        if attr_name:
            setattr(self, attr_name, btn)
        return btn

    def _wait_for_window(self, title: str, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hwnd = _find_window_exact(title)
            if hwnd:
                return hwnd
            time.sleep(0.05)
        raise RuntimeError(
            f"Could not find the scrcpy window titled {title!r}. "
            "Try launching scrcpy manually once or pass a unique --title."
        )

    def _refresh_geometry(self) -> None:
        if not self.hwnd or not _is_window(self.hwnd):
            raise RuntimeError("scrcpy window is no longer available")

        x, y, w, h = _client_rect_screen(self.hwnd)
        self.client_x, self.client_y, self.client_w, self.client_h = x, y, max(1, w), max(1, h)
        cx, cy, cw, ch = fit_rect(self.client_w, self.client_h, self.device_w, self.device_h)
        self.content_x, self.content_y, self.content_w, self.content_h = cx, cy, max(1, cw), max(1, ch)
        self.root.geometry(f"{self.client_w}x{self.client_h}+{self.client_x}+{self.client_y}")
        self.canvas.config(width=self.client_w, height=self.client_h)

    def _position_control_window(self) -> None:
        pad = 12
        self.ctrl.update_idletasks()
        ctrl_w = max(460, self.ctrl.winfo_width())
        ctrl_h = max(640, self.ctrl.winfo_height())
        screen_w = self.ctrl.winfo_screenwidth()
        screen_h = self.ctrl.winfo_screenheight()

        preferred_x = self.client_x + self.client_w + pad
        preferred_y = self.client_y

        if preferred_x + ctrl_w > screen_w - 8:
            preferred_x = max(8, self.client_x - ctrl_w - pad)

        if preferred_y + ctrl_h > screen_h - 48:
            preferred_y = max(8, screen_h - ctrl_h - 48)

        self.ctrl.geometry(f"{ctrl_w}x{ctrl_h}+{preferred_x}+{preferred_y}")

    def _bind_overlay_ownership(self) -> None:
        # Intentionally disabled: binding the overlay as an owned window of scrcpy
        # caused rendering/transparency issues on some Windows setups.
        self.overlay_owner_bound = False

    def _restack_overlay_and_controls(self) -> None:
        try:
            self.ctrl.update_idletasks()
            self.ctrl.lift()
        except Exception:
            pass

        if self.recording_enabled:
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                self.root.lift()
            except Exception:
                pass

    def _show_recording_overlay(self) -> None:
        try:
            self.root.deiconify()
        except Exception:
            pass
        try:
            self.root.attributes("-alpha", 0.18)
        except Exception:
            pass
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.overlay_owner_bound = False
        self.startup_restack_until = time.monotonic() + 2.0
        try:
            self._refresh_geometry()
            self._position_control_window()
            self._draw_overlay()
            self._restack_overlay_and_controls()
        except Exception:
            pass

    def _hide_recording_overlay(self) -> None:
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _draw_overlay(self) -> None:
        self.canvas.delete("all")
        x0, y0 = self.content_x, self.content_y
        x1, y1 = x0 + self.content_w, y0 + self.content_h

        self.canvas.create_rectangle(0, 0, self.client_w, self.client_h, outline="", fill="#202020")
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#00ffff", width=2, fill="#202020")

        mode = "RECORDING" if self.recording_enabled else "PAUSED"
        replay_mode = " | REPLAYING" if self.replay_running else ""
        status = (
            f"{mode}{replay_mode} | device {self.device_w}x{self.device_h} | "
            f"events {len(self.points)} | Space start/stop | S save | P replay"
        )
        self.canvas.create_text(10, 10, text=status, anchor="nw", fill="white", font=("Segoe UI", 11, "bold"))

        if not self.recording_enabled:
            self.canvas.create_rectangle(x0 + 12, y0 + 42, x0 + 220, y0 + 82, fill="#444444", outline="")
            self.canvas.create_text(
                x0 + 22,
                y0 + 62,
                text="Paused - click Start Recording",
                anchor="w",
                fill="white",
                font=("Segoe UI", 10, "bold"),
            )

        if self.replay_running:
            self.canvas.create_rectangle(x0 + 12, y0 + 90, x0 + 320, y0 + 130, fill="#1f4f88", outline="")
            self.canvas.create_text(
                x0 + 22,
                y0 + 110,
                text=f"{self.replay_status_var.get()} | {self.run_counter_var.get()} | {self.countdown_var.get()}",
                anchor="w",
                fill="white",
                font=("Segoe UI", 10, "bold"),
            )

        if self.points:
            coords = []
            for p in self.points[-300:]:
                sx, sy = self.device_to_canvas(p.x, p.y)
                coords.extend([sx, sy])
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill="#ffcc00", width=2, smooth=True)

    def _build_scrcpy_extra_args(self) -> List[str]:
        extra_args: List[str] = []
        if self.window_width:
            extra_args += ["--window-width", str(self.window_width)]
        if self.no_audio:
            extra_args += ["--no-audio"]
        return extra_args

    def _stop_scrcpy(self) -> None:
        try:
            if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
                self.scrcpy_proc.terminate()
                try:
                    self.scrcpy_proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self.scrcpy_proc.kill()
                    except Exception:
                        pass
            self.scrcpy_proc = None
            self.hwnd = None
            self.overlay_owner_bound = False
            time.sleep(0.2)
        except Exception:
            pass

    def _restart_scrcpy(self, new_serial: str) -> None:
        check_device(serial=new_serial, adb_path=self.adb_path)
        self._stop_scrcpy()
        time.sleep(0.4)
        self.serial = new_serial
        self.active_serial_var.set(new_serial or "(auto)")
        self._launch_scrcpy_for_current_serial()

    def canvas_to_device(self, x: int, y: int) -> Optional[Tuple[int, int]]:
        if not (
            self.content_x <= x < self.content_x + self.content_w
            and self.content_y <= y < self.content_y + self.content_h
        ):
            return None
        rx = (x - self.content_x) / self.content_w
        ry = (y - self.content_y) / self.content_h
        dx = max(0, min(self.device_w - 1, int(round(rx * (self.device_w - 1)))))
        dy = max(0, min(self.device_h - 1, int(round(ry * (self.device_h - 1)))))
        return dx, dy

    def device_to_canvas(self, x: int, y: int) -> Tuple[int, int]:
        sx = self.content_x + int(round((x / max(1, self.device_w - 1)) * self.content_w))
        sy = self.content_y + int(round((y / max(1, self.device_h - 1)) * self.content_h))
        return sx, sy

    def _set_recording(self, enabled: bool) -> None:
        if self.recording_enabled == enabled:
            return
        if not enabled and self.dragging and self.last_device_point is not None:
            lx, ly = self.last_device_point
            try:
                send_motionevent("UP", lx, ly, serial=self.serial, adb_path=self.adb_path)
            except Exception:
                pass
            self.dragging = False

        self.recording_enabled = enabled
        self.status_var.set("Recording" if enabled else "Paused")
        self.record_btn.config(text="Stop Recording" if enabled else "Start Recording")
        self._draw_overlay()

        if enabled:
            self._show_recording_overlay()
        else:
            self._hide_recording_overlay()

    def _set_replay_running(self, enabled: bool) -> None:
        self.replay_running = enabled
        self.replay_btn.config(state=tk.DISABLED if enabled else tk.NORMAL)
        self.stop_replay_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self._draw_overlay()

    def toggle_recording(self, _event=None) -> None:
        self._set_recording(not self.recording_enabled)

    def log_event(self, action: str, x: int, y: int) -> None:
        now = time.monotonic()
        if self.recording_start is None:
            self.recording_start = now
        dt_ms = int(round((now - self.recording_start) * 1000))
        self.points.append(MotionPoint(dt_ms=dt_ms, action=action, x=x, y=y))
        self.last_device_point = (x, y)
        self._draw_overlay()

    def on_down(self, event) -> None:
        if not self.recording_enabled:
            return
        pt = self.canvas_to_device(event.x, event.y)
        if pt is None:
            return
        self.dragging = True
        self.last_move_sent = 0.0
        x, y = pt
        try:
            send_motionevent("DOWN", x, y, serial=self.serial, adb_path=self.adb_path)
            self.log_event("DOWN", x, y)
        except Exception as exc:
            self._flash_message(f"DOWN failed: {exc}", error=True)

    def on_move(self, event) -> None:
        if not self.recording_enabled or not self.dragging:
            return
        pt = self.canvas_to_device(event.x, event.y)
        if pt is None:
            return
        now = time.monotonic()
        if now - self.last_move_sent < self.move_interval_s:
            return
        self.last_move_sent = now
        x, y = pt
        try:
            send_motionevent("MOVE", x, y, serial=self.serial, adb_path=self.adb_path)
            self.log_event("MOVE", x, y)
        except Exception as exc:
            self._flash_message(f"MOVE failed: {exc}", error=True)

    def on_up(self, event) -> None:
        if not self.recording_enabled or not self.dragging:
            return
        self.dragging = False
        pt = self.canvas_to_device(event.x, event.y)
        if pt is None:
            ex = min(max(event.x, self.content_x), self.content_x + self.content_w - 1)
            ey = min(max(event.y, self.content_y), self.content_y + self.content_h - 1)
            pt = self.canvas_to_device(ex, ey)
        if pt is None:
            return
        x, y = pt
        try:
            send_motionevent("UP", x, y, serial=self.serial, adb_path=self.adb_path)
            self.log_event("UP", x, y)
        except Exception as exc:
            self._flash_message(f"UP failed: {exc}", error=True)

    def _flash_message(self, msg: str, error: bool = False) -> None:
        self.canvas.delete("message")
        bg = "#880000" if error else "#005500"
        self.canvas.create_rectangle(20, 40, self.client_w - 20, 90, fill=bg, outline="", tags="message")
        self.canvas.create_text(30, 65, text=msg, anchor="w", fill="white", font=("Segoe UI", 10, "bold"), tags="message")
        self.root.after(2500, lambda: self.canvas.delete("message"))

    def _parse_float_optional(self, value: str, field_name: str) -> Optional[float]:
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"{field_name} must be a number")

    def _parse_replay_values(self) -> Tuple[float, int, float, Optional[float], Optional[float], bool, Optional[float], int, int]:
        try:
            speed = float(self.speed_var.get().strip())
            repeat_count = int(self.repeat_count_var.get().strip())
            interval_s = float(self.interval_s_var.get().strip())
            jitter_px = int(self.jitter_px_var.get().strip())
            timing_jitter_ms = int(self.timing_jitter_var.get().strip())
        except ValueError:
            raise ValueError("Replay speed/interval must be numbers, and repeat/jitter values must be integers")

        interval_min_s = self._parse_float_optional(self.interval_min_s_var.get(), "Random interval min")
        interval_max_s = self._parse_float_optional(self.interval_max_s_var.get(), "Random interval max")
        auto_quit_s = self._parse_float_optional(self.auto_quit_s_var.get(), "Auto-quit after s")
        infinite = bool(self.infinite_var.get())

        if speed <= 0:
            raise ValueError("Replay speed must be > 0")
        if repeat_count < 1:
            raise ValueError("Repeat count must be >= 1")
        if interval_s < 0:
            raise ValueError("Interval seconds must be >= 0")
        if auto_quit_s is not None and auto_quit_s <= 0:
            raise ValueError("Auto-quit after s must be > 0")
        if jitter_px < 0 or timing_jitter_ms < 0:
            raise ValueError("Jitter values must be >= 0")
        validate_interval_range(interval_min_s, interval_max_s)

        return (
            speed,
            repeat_count,
            interval_s,
            interval_min_s,
            interval_max_s,
            infinite,
            auto_quit_s,
            jitter_px,
            timing_jitter_ms,
        )

    def _queue_replay_update(self, payload: dict) -> None:
        self.replay_queue.put(payload)

    def _handle_replay_update(self, payload: dict) -> None:
        kind = payload.get("kind")
        total_runs = payload.get("total_runs")
        total_text = "∞" if total_runs is None else str(total_runs)

        if kind == "start":
            self.replay_status_var.set("Starting")
            self.run_counter_var.set(f"0 / {total_text}")
            self.countdown_var.set("—")
            self.auto_quit_left_var.set(_format_seconds(payload.get("auto_quit_s")))
            return

        if kind == "run_start":
            current_run = payload.get("current_run", 0)
            self.replay_status_var.set("Running")
            self.run_counter_var.set(f"{current_run} / {total_text}")
            self.countdown_var.set("in progress")
            self.auto_quit_left_var.set(_format_seconds(payload.get("auto_quit_remaining_s")))
            return

        if kind == "run_complete":
            runs_completed = payload.get("runs_completed", 0)
            self.replay_status_var.set("Run complete")
            self.run_counter_var.set(f"{runs_completed} / {total_text}")
            self.countdown_var.set("0.0s")
            self.auto_quit_left_var.set(_format_seconds(payload.get("auto_quit_remaining_s")))
            return

        if kind == "interval_start":
            next_run = payload.get("next_run", 0)
            self.replay_status_var.set("Waiting")
            self.run_counter_var.set(f"{next_run} / {total_text}")
            self.countdown_var.set(_format_seconds(payload.get("countdown_s")))
            self.auto_quit_left_var.set(_format_seconds(payload.get("auto_quit_remaining_s")))
            return

        if kind == "tick":
            phase = payload.get("phase")
            if phase == "interval":
                next_run = payload.get("next_run", 0)
                self.replay_status_var.set("Waiting")
                self.run_counter_var.set(f"{next_run} / {total_text}")
                self.countdown_var.set(_format_seconds(payload.get("countdown_s")))
            else:
                current_run = payload.get("current_run", 0)
                self.replay_status_var.set("Running")
                self.run_counter_var.set(f"{current_run} / {total_text}")
                self.countdown_var.set(_format_seconds(payload.get("countdown_s")))
            self.auto_quit_left_var.set(_format_seconds(payload.get("auto_quit_remaining_s")))
            return

        if kind == "error":
            self._flash_message(str(payload.get("message", "Replay failed")), error=True)
            return

        if kind == "reconnect_wait":
            target = str(payload.get("target", ""))
            attempt = int(payload.get("attempt", 0))
            delay_s = payload.get("delay_s")
            self.replay_status_var.set("Reconnecting")
            self.auto_reconnect_status_var.set(f"Attempt {attempt} to reconnect {target} in {_format_seconds(delay_s)}")
            self.countdown_var.set(_format_seconds(delay_s))
            return

        if kind == "reconnect_request":
            target = str(payload.get("target", ""))
            if target:
                self.last_connected_target = target
                self.wifi_target_var.set(target)
                self.serial = target
                self.active_serial_var.set(target)
                self._begin_transition(20.0)
                self._try_recover_scrcpy_view()
            self.replay_status_var.set("Reconnecting")
            self.auto_reconnect_status_var.set(f"Reconnected transport to {target}. Restarting macro…")
            return

        if kind == "finish":
            self._set_replay_running(False)
            runs_completed = payload.get("runs_completed", 0)
            self.run_counter_var.set(f"{runs_completed} / {total_text}")
            self.countdown_var.set("—")
            self.auto_quit_left_var.set("—")
            self.auto_reconnect_status_var.set("Enabled" if self.auto_reconnect_var.get() else "Disabled")
            if payload.get("stopped_by_timer"):
                self.replay_status_var.set("Stopped by timer")
                self._flash_message("Replay stopped by auto-quit timer")
            elif payload.get("stopped_by_user"):
                self.replay_status_var.set("Stopped")
                self._flash_message("Replay stopped")
            else:
                self.replay_status_var.set("Finished")
                self._flash_message(f"Replay finished in {payload.get('elapsed', 0):.2f}s")
            return

    def _current_target_serial(self) -> str:
        target = (self.serial or "").strip()
        if target:
            return target
        target = self.wifi_target_var.get().strip() or (self.last_connected_target or "")
        return target

    def _begin_transition(self, seconds: float = 12.0) -> None:
        self.scrcpy_transitioning = True
        self.scrcpy_reconnect_deadline = time.monotonic() + max(1.0, seconds)

    def _end_transition(self) -> None:
        self.scrcpy_transitioning = False
        self.scrcpy_reconnect_deadline = 0.0

    def _launch_scrcpy_for_current_serial(self) -> None:
        self.scrcpy_proc = launch_scrcpy(self.scrcpy_path, self.serial, self.title, self._build_scrcpy_extra_args())
        self.hwnd = self._wait_for_window(self.title, timeout=10.0)
        self.device_w, self.device_h = get_current_display_size(serial=self.serial, adb_path=self.adb_path)
        self._refresh_geometry()
        self._position_control_window()
        if self.recording_enabled:
            self._show_recording_overlay()
        else:
            self._hide_recording_overlay()
        self._draw_overlay()

    def _try_recover_scrcpy_view(self) -> bool:
        target = self._current_target_serial()
        if not target:
            return False

        now = time.monotonic()
        if now - self.scrcpy_last_restart_attempt < 1.0:
            return False
        self.scrcpy_last_restart_attempt = now

        try:
            check_device(serial=target, adb_path=self.adb_path)
        except Exception:
            self.wifi_status_var.set(f"View disconnected. Waiting for {target}…")
            return False

        try:
            self.serial = target
            self.active_serial_var.set(target)
            self._stop_scrcpy()
            self._launch_scrcpy_for_current_serial()
            self.wifi_status_var.set(f"Recovered view on {target}")
            self._flash_message(f"Recovered view on {target}")
            self._end_transition()
            return True
        except Exception as exc:
            self.wifi_status_var.set(f"Reconnect pending: {exc}")
            return False

    def on_reconnect_view(self, _event=None) -> None:
        try:
            target = self._current_target_serial()
            if not target:
                raise ValueError("No Wi-Fi target available yet")
            self._begin_transition(15.0)
            self.serial = target
            self.active_serial_var.set(target)
            if self._try_recover_scrcpy_view():
                self._flash_message(f"Reconnected view to {target}")
            else:
                self._flash_message(f"Reconnect started for {target}")
        except Exception as exc:
            self.wifi_status_var.set(str(exc))
            self._flash_message(f"Reconnect failed: {exc}", error=True)

    def on_wifi_pair(self, _event=None) -> None:
        try:
            pair_target = self.wifi_pair_target_var.get().strip()
            pair_code = self.wifi_pair_code_var.get().strip()
            if not pair_target:
                raise ValueError("Enter Pair target from the phone, for example 192.168.1.25:37145")
            if not pair_code:
                raise ValueError("Enter the pairing code from the phone")
            output = adb_pair(pair_target, pair_code, adb_path=self.adb_path)
            self.wifi_status_var.set(output or "Paired")
            ip = pair_target.split(":", 1)[0]
            if not self.wifi_ip_var.get().strip():
                self.wifi_ip_var.set(ip)
            self._flash_message("Wi-Fi pair successful")
        except Exception as exc:
            self.wifi_status_var.set(str(exc))
            self._flash_message(f"Wi-Fi pair failed: {exc}", error=True)

    def on_wifi_connect(self, _event=None) -> None:
        try:
            ip = self.wifi_ip_var.get().strip() or None
            port_raw = self.wifi_port_var.get().strip() or "5555"
            port = int(port_raw)
            enable_tcpip = bool(self.wifi_enable_tcpip_var.get())
            serial_hint = None
            if enable_tcpip and self.serial and ":" not in self.serial:
                serial_hint = self.serial

            self._begin_transition(15.0)
            self.wifi_status_var.set("Connecting…")

            target, output = wifi_connect_helper(
                adb_path=self.adb_path,
                ip=ip,
                port=port,
                serial=serial_hint,
                enable_tcpip=enable_tcpip,
            )

            self.last_connected_target = target
            self.wifi_target_var.set(target)
            self.wifi_status_var.set(output)
            if not self.wifi_ip_var.get().strip():
                self.wifi_ip_var.set(target.split(":", 1)[0])

            # Auto-switch to the connected Wi-Fi target so a USB scrcpy session
            # does not die and take the whole UI down after `adb tcpip`.
            self._restart_scrcpy(target)
            self.wifi_status_var.set(output + f"\nUsing {target}")
            self._flash_message(f"Connected and switched to {target}")
        except Exception as exc:
            self.wifi_status_var.set(str(exc))
            self._flash_message(f"Wi-Fi connect failed: {exc}", error=True)
        finally:
            self._end_transition()

    def on_use_wifi_target(self, _event=None) -> None:
        try:
            target = self.wifi_target_var.get().strip() or (self.last_connected_target or "")
            if not target:
                raise ValueError("Connect to a Wi-Fi target first")
            self._begin_transition(15.0)
            self._restart_scrcpy(target)
            self.wifi_status_var.set(f"Using {target}")
            self._flash_message(f"Switched session to {target}")
        except Exception as exc:
            self.wifi_status_var.set(str(exc))
            self._flash_message(f"Switch failed: {exc}", error=True)
        finally:
            self._end_transition()


    def _parse_retry_delay(self) -> float:
        raw = self.reconnect_delay_s_var.get().strip()
        try:
            value = float(raw)
        except ValueError:
            raise ValueError("Retry delay must be a number")
        if value <= 0:
            raise ValueError("Retry delay must be > 0")
        return value

    def on_new_macro(self, _event=None) -> None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        new_name = f"macro_{timestamp}"
        self.points.clear()
        self.recording_start = None
        self.last_device_point = None
        self.output = macro_path_for_name(new_name)
        self.macro_name_var.set(new_name)
        self.library_status_var.set(f"Started new macro: {new_name}.json")
        self._draw_overlay()
        self._flash_message(f"New macro {new_name}")

    def refresh_library_lists(self, _event=None) -> None:
        self.saved_macro_names = list_saved_macro_names()
        self.saved_profile_names = list_saved_profile_names()
        try:
            self.macro_combo["values"] = self.saved_macro_names
        except Exception:
            pass
        try:
            self.profile_combo["values"] = self.saved_profile_names
        except Exception:
            pass
        self.library_status_var.set(
            f"Found {len(self.saved_macro_names)} macro(s) and {len(self.saved_profile_names)} profile(s)"
        )

    def on_macro_selected(self, _event=None) -> None:
        value = self.macro_name_var.get().strip()
        if value:
            self.library_status_var.set(f"Selected macro: {value}")

    def on_profile_selected(self, _event=None) -> None:
        value = self.profile_name_var.get().strip()
        if value:
            self.library_status_var.set(f"Selected profile: {value}")

    def _current_macro_name(self) -> str:
        return sanitize_name(self.macro_name_var.get().strip())

    def _collect_profile_payload(self) -> Dict[str, Any]:
        return {
            "profile_name": self.profile_name_var.get().strip(),
            "macro_name": self.macro_name_var.get().strip(),
            "serial": self.serial,
            "wifi_ip": self.wifi_ip_var.get(),
            "wifi_port": self.wifi_port_var.get(),
            "wifi_target": self.wifi_target_var.get(),
            "wifi_enable_tcpip": bool(self.wifi_enable_tcpip_var.get()),
            "speed": self.speed_var.get(),
            "repeat_count": self.repeat_count_var.get(),
            "interval_s": self.interval_s_var.get(),
            "interval_min_s": self.interval_min_s_var.get(),
            "interval_max_s": self.interval_max_s_var.get(),
            "infinite": bool(self.infinite_var.get()),
            "auto_quit_s": self.auto_quit_s_var.get(),
            "jitter_px": self.jitter_px_var.get(),
            "timing_jitter_ms": self.timing_jitter_var.get(),
            "auto_reconnect": bool(self.auto_reconnect_var.get()),
            "reconnect_delay_s": self.reconnect_delay_s_var.get(),
        }

    def _apply_profile_payload(self, payload: Dict[str, Any]) -> None:
        self.profile_name_var.set(str(payload.get("profile_name", self.profile_name_var.get())))
        self.macro_name_var.set(str(payload.get("macro_name", self.macro_name_var.get())))
        self.wifi_ip_var.set(str(payload.get("wifi_ip", self.wifi_ip_var.get())))
        self.wifi_port_var.set(str(payload.get("wifi_port", self.wifi_port_var.get())))
        self.wifi_target_var.set(str(payload.get("wifi_target", self.wifi_target_var.get())))
        self.wifi_enable_tcpip_var.set(bool(payload.get("wifi_enable_tcpip", self.wifi_enable_tcpip_var.get())))
        self.speed_var.set(str(payload.get("speed", self.speed_var.get())))
        self.repeat_count_var.set(str(payload.get("repeat_count", self.repeat_count_var.get())))
        self.interval_s_var.set(str(payload.get("interval_s", self.interval_s_var.get())))
        self.interval_min_s_var.set(str(payload.get("interval_min_s", self.interval_min_s_var.get() or "")) if payload.get("interval_min_s") is not None else "")
        self.interval_max_s_var.set(str(payload.get("interval_max_s", self.interval_max_s_var.get() or "")) if payload.get("interval_max_s") is not None else "")
        self.infinite_var.set(bool(payload.get("infinite", self.infinite_var.get())))
        self.auto_quit_s_var.set(str(payload.get("auto_quit_s", self.auto_quit_s_var.get() or "")))
        self.jitter_px_var.set(str(payload.get("jitter_px", self.jitter_px_var.get())))
        self.timing_jitter_var.set(str(payload.get("timing_jitter_ms", self.timing_jitter_var.get())))
        self.auto_reconnect_var.set(bool(payload.get("auto_reconnect", self.auto_reconnect_var.get())))
        self.reconnect_delay_s_var.set(str(payload.get("reconnect_delay_s", self.reconnect_delay_s_var.get())))
        serial_value = payload.get("serial")
        if serial_value:
            self.serial = str(serial_value)
            self.active_serial_var.set(self.serial)
        self.auto_reconnect_status_var.set("Enabled" if self.auto_reconnect_var.get() else "Disabled")

    def on_save_named_macro(self, _event=None) -> None:
        try:
            name = self._current_macro_name()
            path = macro_path_for_name(name)
            save_macro(path, (self.device_w, self.device_h), self.points)
            self.output = path
            self.macro_name_var.set(path.stem)
            self.refresh_library_lists()
            self.library_status_var.set(f"Saved macro: {path.name}")
            self._flash_message(f"Saved macro {path.name}")
        except Exception as exc:
            self.library_status_var.set(str(exc))
            self._flash_message(f"Save macro failed: {exc}", error=True)

    def on_load_named_macro(self, _event=None) -> None:
        try:
            name = self._current_macro_name()
            path = macro_path_for_name(name)
            size, points = load_macro(path)
            self.points = points
            self.output = path
            self.macro_name_var.set(path.stem)
            self.last_device_point = (points[-1].x, points[-1].y) if points else None
            self.refresh_library_lists()
            self.library_status_var.set(f"Loaded macro: {path.name} ({len(points)} events)")
            self._draw_overlay()
            self._flash_message(f"Loaded macro {path.name}")
        except Exception as exc:
            self.library_status_var.set(str(exc))
            self._flash_message(f"Load macro failed: {exc}", error=True)

    def on_save_profile(self, _event=None) -> None:
        try:
            name = sanitize_name(self.profile_name_var.get().strip())
            path = save_profile_data(name, self._collect_profile_payload())
            self.profile_name_var.set(path.stem)
            self.refresh_library_lists()
            self.library_status_var.set(f"Saved profile: {path.name}")
            self._flash_message(f"Saved profile {path.name}")
        except Exception as exc:
            self.library_status_var.set(str(exc))
            self._flash_message(f"Save profile failed: {exc}", error=True)

    def on_load_profile(self, _event=None) -> None:
        try:
            name = sanitize_name(self.profile_name_var.get().strip())
            payload = load_profile_data(name)
            self._apply_profile_payload(payload)
            self.profile_name_var.set(name)
            self.refresh_library_lists()
            self.library_status_var.set(f"Loaded profile: {name}.json")
            self._flash_message(f"Loaded profile {name}.json")
        except Exception as exc:
            self.library_status_var.set(str(exc))
            self._flash_message(f"Load profile failed: {exc}", error=True)

    def _request_ui_reconnect(self, target: str) -> None:
        self.pending_reconnect_target = target
        self.pending_replay_restart = True
        self._queue_replay_update({"kind": "reconnect_request", "target": target})

    def _attempt_transport_reconnect(self, target: str) -> bool:
        try:
            if ":" in target:
                adb_connect(target, adb_path=self.adb_path)
            check_device(serial=target, adb_path=self.adb_path)
            return True
        except Exception:
            return False

    def on_save(self, _event=None) -> None:
        try:
            save_macro(self.output, (self.device_w, self.device_h), self.points)
            self.library_status_var.set(f"Saved current macro: {self.output.name}")
            self._flash_message(f"Saved {len(self.points)} events to {self.output}")
        except Exception as exc:
            self._flash_message(f"Save failed: {exc}", error=True)

    def on_clear(self, _event=None) -> None:
        self.points.clear()
        self.recording_start = None
        self.last_device_point = None
        self._draw_overlay()

    def _start_replay_worker(
        self,
        speed: float,
        repeat_count: int,
        interval_s: float,
        interval_min_s: Optional[float],
        interval_max_s: Optional[float],
        infinite: bool,
        auto_quit_s: Optional[float],
        jitter_px: int,
        timing_jitter_ms: int,
        auto_reconnect: bool,
        reconnect_delay_s: float,
    ) -> None:
        self.replay_stop_event.clear()
        self._set_replay_running(True)
        self.replay_status_var.set("Queued")
        self.countdown_var.set("—")
        self.auto_quit_left_var.set(_format_seconds(auto_quit_s))
        total_text = "∞" if infinite else str(repeat_count)
        self.run_counter_var.set(f"0 / {total_text}")
        self.last_replay_config = {
            "speed": speed,
            "repeat_count": repeat_count,
            "interval_s": interval_s,
            "interval_min_s": interval_min_s,
            "interval_max_s": interval_max_s,
            "infinite": infinite,
            "auto_quit_s": auto_quit_s,
            "jitter_px": jitter_px,
            "timing_jitter_ms": timing_jitter_ms,
            "auto_reconnect": auto_reconnect,
            "reconnect_delay_s": reconnect_delay_s,
            "macro_path": str(self.output),
        }
        self.replay_restart_count = 0
        self.startup_restack_until = time.monotonic() + 3.0
        self.overlay_owner_bound = False
        self.auto_reconnect_status_var.set("Enabled" if auto_reconnect else "Disabled")

        def worker() -> None:
            overall_started = time.monotonic()
            while not self.replay_stop_event.is_set():
                remaining_auto_quit = None
                if auto_quit_s is not None:
                    remaining_auto_quit = max(0.0, auto_quit_s - (time.monotonic() - overall_started))
                    if remaining_auto_quit <= 0:
                        self._queue_replay_update({
                            "kind": "finish",
                            "stopped_by_timer": True,
                            "runs_completed": 0,
                            "total_runs": None if infinite else repeat_count,
                        })
                        return
                try:
                    replay_macro(
                        Path(str(self.output)),
                        serial=self.serial,
                        adb_path=self.adb_path,
                        speed=speed,
                        jitter_px=jitter_px,
                        timing_jitter_ms=timing_jitter_ms,
                        repeat_count=repeat_count,
                        interval_s=interval_s,
                        interval_min_s=interval_min_s,
                        interval_max_s=interval_max_s,
                        infinite=infinite,
                        auto_quit_s=remaining_auto_quit,
                        progress_callback=self._queue_replay_update,
                        stop_event=self.replay_stop_event,
                    )
                    return
                except Exception as exc:
                    if self.replay_stop_event.is_set():
                        self._queue_replay_update({"kind": "finish", "stopped_by_user": True, "runs_completed": 0, "total_runs": None if infinite else repeat_count})
                        return
                    if not auto_reconnect:
                        self._queue_replay_update({"kind": "error", "message": f"Replay failed: {exc}"})
                        self._queue_replay_update({"kind": "finish", "stopped_by_user": True, "runs_completed": 0, "total_runs": None if infinite else repeat_count})
                        return

                    target = self._current_target_serial()
                    self.replay_restart_count += 1
                    self._queue_replay_update({
                        "kind": "error",
                        "message": f"Replay interrupted: {exc}",
                    })
                    while not self.replay_stop_event.is_set():
                        self._queue_replay_update({
                            "kind": "reconnect_wait",
                            "target": target,
                            "attempt": self.replay_restart_count,
                            "delay_s": reconnect_delay_s,
                        })
                        time.sleep(reconnect_delay_s)
                        if self._attempt_transport_reconnect(target):
                            self._request_ui_reconnect(target)
                            break
                    if self.replay_stop_event.is_set():
                        self._queue_replay_update({"kind": "finish", "stopped_by_user": True, "runs_completed": 0, "total_runs": None if infinite else repeat_count})
                        return

        self.replay_thread = threading.Thread(target=worker, daemon=True)
        self.replay_thread.start()

    def stop_replay(self, _event=None) -> None:
        if self.replay_running:
            self.replay_stop_event.set()
            self.replay_status_var.set("Stopping…")

    def on_replay(self, _event=None) -> None:
        if self.replay_running:
            self._flash_message("Replay is already running", error=True)
            return

        try:
            (
                speed,
                repeat_count,
                interval_s,
                interval_min_s,
                interval_max_s,
                infinite,
                auto_quit_s,
                jitter_px,
                timing_jitter_ms,
            ) = self._parse_replay_values()
            reconnect_delay_s = self._parse_retry_delay()
            auto_reconnect = bool(self.auto_reconnect_var.get())
            if not self.output.exists():
                save_macro(self.output, (self.device_w, self.device_h), self.points)
            self._start_replay_worker(
                speed=speed,
                repeat_count=repeat_count,
                interval_s=interval_s,
                interval_min_s=interval_min_s,
                interval_max_s=interval_max_s,
                infinite=infinite,
                auto_quit_s=auto_quit_s,
                jitter_px=jitter_px,
                timing_jitter_ms=timing_jitter_ms,
                auto_reconnect=auto_reconnect,
                reconnect_delay_s=reconnect_delay_s,
            )
            self._flash_message("Replay started")
        except Exception as exc:
            self._flash_message(f"Replay failed: {exc}", error=True)

    def on_quit(self, _event=None) -> None:
        try:
            self._end_transition()
        except Exception:
            pass
        try:
            self.replay_stop_event.set()
        except Exception:
            pass
        try:
            if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
                self.scrcpy_proc.terminate()
        except Exception:
            pass
        try:
            self.ctrl.destroy()
        except Exception:
            pass
        self.root.quit()

    def _poll(self) -> None:
        try:
            while True:
                try:
                    payload = self.replay_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_replay_update(payload)

            proc_dead = bool(self.scrcpy_proc and self.scrcpy_proc.poll() is not None)

            if proc_dead:
                if self.scrcpy_transitioning or time.monotonic() < self.scrcpy_reconnect_deadline:
                    if not self._try_recover_scrcpy_view():
                        self.root.after(200, self._poll)
                        return
                else:
                    self.wifi_status_var.set("View closed. Use Reconnect View or Connect again.")
                    self._stop_scrcpy()
                    self.canvas.delete("all")
                    self.canvas.create_rectangle(0, 0, max(1, self.client_w), max(1, self.client_h), outline="", fill="#202020")
                    self.canvas.create_text(
                        20,
                        20,
                        text="scrcpy view is closed or disconnected.\nUse Reconnect View or Connect again.",
                        anchor="nw",
                        fill="white",
                        font=("Segoe UI", 12, "bold"),
                    )
                    self.root.after(200, self._poll)
                    return

            # If process is alive but the hwnd was lost/recreated, try to reacquire without killing the app.
            if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
                if not self.hwnd or not _is_window(self.hwnd):
                    hwnd = _find_window_exact(self.title)
                    if hwnd:
                        self.hwnd = hwnd
                    elif self.scrcpy_transitioning or time.monotonic() < self.scrcpy_reconnect_deadline:
                        if not self._try_recover_scrcpy_view():
                            self.root.after(200, self._poll)
                            return
                    else:
                        self.wifi_status_var.set("Waiting for scrcpy window…")

            if self.hwnd and _is_window(self.hwnd):
                try:
                    self._refresh_geometry()
                    self._position_control_window()
                    if self.recording_enabled and time.monotonic() < self.startup_restack_until:
                        self._restack_overlay_and_controls()
                    self._draw_overlay()
                except Exception as exc:
                    if self.scrcpy_transitioning or time.monotonic() < self.scrcpy_reconnect_deadline:
                        self.wifi_status_var.set(f"Recovering view: {exc}")
                        self.root.after(200, self._poll)
                        return
                    else:
                        self.wifi_status_var.set(f"View error: {exc}")
            else:
                self._draw_overlay()
        except Exception as exc:
            self.wifi_status_var.set(f"Unexpected UI error: {exc}")
            self._flash_message(str(exc), error=True)
        self.root.after(100, self._poll)

    def run(self) -> None:
        self.root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="scrcpy live-view touch macro recorder/replayer (Windows)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record-scrcpy", help="Launch scrcpy in read-only mode and record gestures from an overlay")
    p_rec.add_argument("output", type=Path, help="Output JSON path")
    p_rec.add_argument("--scrcpy", help="Path to scrcpy.exe")
    p_rec.add_argument("--adb", help="Path to adb.exe")
    p_rec.add_argument("--serial", help="adb device serial, including Wi-Fi targets like 192.168.1.25:5555")
    p_rec.add_argument("--title", default="ADB Macro Preview", help="Unique scrcpy window title")
    p_rec.add_argument("--window-width", type=int, default=420, help="Initial scrcpy window width")
    p_rec.add_argument("--with-audio", action="store_true", help="Forward audio too")
    p_rec.add_argument("--default-jitter-px", type=int, default=2, help="Default replay position jitter shown in the control window")
    p_rec.add_argument("--default-timing-jitter-ms", type=int, default=12, help="Default replay timing jitter shown in the control window")
    p_rec.add_argument("--default-replay-speed", type=float, default=1.0, help="Default replay speed shown in the control window")
    p_rec.add_argument("--default-repeat-count", type=int, default=1, help="Default repeat count shown in the control window")
    p_rec.add_argument("--default-interval-s", type=float, default=0.0, help="Default fixed replay interval shown in the control window")
    p_rec.add_argument("--default-interval-min-s", type=float, help="Default random replay interval min shown in the control window")
    p_rec.add_argument("--default-interval-max-s", type=float, help="Default random replay interval max shown in the control window")
    p_rec.add_argument("--default-infinite", action="store_true", help="Default the GUI replay mode to infinite")
    p_rec.add_argument("--default-auto-quit-s", type=float, help="Default auto-quit timer shown in the control window")

    p_rep = sub.add_parser("replay", help="Replay a saved macro with adb input motionevent")
    p_rep.add_argument("input", type=Path, help="Input JSON path")
    p_rep.add_argument("--adb", help="Path to adb.exe")
    p_rep.add_argument("--serial", help="adb device serial, including Wi-Fi targets like 192.168.1.25:5555")
    p_rep.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier")
    p_rep.add_argument("--jitter-px", type=int, default=0, help="Random coordinate jitter in pixels, applied once per press")
    p_rep.add_argument("--timing-jitter-ms", type=int, default=0, help="Random delay jitter applied between events")
    p_rep.add_argument("--repeat-count", type=int, default=1, help="How many times to run the macro")
    p_rep.add_argument("--interval-s", type=float, default=0.0, help="Fixed delay between runs")
    p_rep.add_argument("--interval-min-s", type=float, help="Random interval minimum between runs")
    p_rep.add_argument("--interval-max-s", type=float, help="Random interval maximum between runs")
    p_rep.add_argument("--infinite", action="store_true", help="Replay forever until stopped or auto-quit fires")
    p_rep.add_argument("--auto-quit-s", type=float, help="Stop replay after this many seconds")
    p_rep.add_argument("--seed", type=int, help="Optional RNG seed for repeatable jitter/intervals")
    p_rep.add_argument("--dry-run", action="store_true", help="Print commands instead of sending them")

    p_pair = sub.add_parser("wifi-pair", help="Pair adb to a device over Wi-Fi using Android 11+ Wireless debugging")
    p_pair.add_argument("pair_target", help="Pairing endpoint shown on the phone, for example 192.168.1.25:37145")
    p_pair.add_argument("--code", required=True, help="Six-digit pairing code shown on the phone")
    p_pair.add_argument("--adb", help="Path to adb.exe")

    p_connect = sub.add_parser("wifi-connect", help="Connect adb to a device over Wi-Fi")
    p_connect.add_argument("--adb", help="Path to adb.exe")
    p_connect.add_argument("--serial", help="Connected USB serial to use for --enable-tcpip, or a specific already-connected device")
    p_connect.add_argument("--ip", help="Device IP address or wireless debugging connect IP")
    p_connect.add_argument("--port", type=int, default=5555, help="TCP port to use, default 5555")
    p_connect.add_argument("--enable-tcpip", action="store_true", help="Enable adb tcpip on a currently USB-connected device before connecting")

    return parser


def main() -> int:
    if not IS_WINDOWS:
        print("This script is currently intended for Windows.", file=sys.stderr)
        return 1
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.cmd == "record-scrcpy":
            scrcpy_path = resolve_scrcpy_path(args.scrcpy)
            adb_path = resolve_adb_path(args.adb, scrcpy_path=scrcpy_path)
            check_device(serial=args.serial, adb_path=adb_path)
            app = ScrcpyRecorderGUI(
                output=args.output,
                scrcpy_path=scrcpy_path,
                adb_path=adb_path,
                serial=args.serial,
                title=args.title,
                window_width=args.window_width,
                no_audio=not args.with_audio,
                default_jitter_px=args.default_jitter_px,
                default_timing_jitter_ms=args.default_timing_jitter_ms,
                default_speed=args.default_replay_speed,
                default_repeat_count=args.default_repeat_count,
                default_interval_s=args.default_interval_s,
                default_interval_min_s=args.default_interval_min_s,
                default_interval_max_s=args.default_interval_max_s,
                default_infinite=args.default_infinite,
                default_auto_quit_s=args.default_auto_quit_s,
            )
            app.run()
            return 0

        if args.cmd == "replay":
            adb_path = resolve_adb_path(args.adb)
            replay_macro(
                args.input,
                serial=args.serial,
                speed=args.speed,
                dry_run=args.dry_run,
                adb_path=adb_path,
                jitter_px=args.jitter_px,
                timing_jitter_ms=args.timing_jitter_ms,
                repeat_count=args.repeat_count,
                interval_s=args.interval_s,
                interval_min_s=args.interval_min_s,
                interval_max_s=args.interval_max_s,
                infinite=args.infinite,
                auto_quit_s=args.auto_quit_s,
                seed=args.seed,
            )
            return 0

        if args.cmd == "wifi-pair":
            adb_path = resolve_adb_path(args.adb)
            output = adb_pair(args.pair_target, args.code, adb_path=adb_path)
            print(output)
            print("\nPaired. If adb does not auto-connect, run wifi-connect with the connect IP/port shown on the phone.")
            return 0

        if args.cmd == "wifi-connect":
            adb_path = resolve_adb_path(args.adb)
            target, output = wifi_connect_helper(
                adb_path=adb_path,
                ip=args.ip,
                port=args.port,
                serial=args.serial,
                enable_tcpip=args.enable_tcpip,
            )
            print(output)
            print("\nConnected target:", target)
            print("Use this serial with record-scrcpy or replay:")
            print(f"  --serial {target}")
            return 0

        parser.error("Unknown command")
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":


    raise SystemExit(main())
