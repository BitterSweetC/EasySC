from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Sequence

from PIL import ImageGrab


QUALITY_PRESETS = {
    "Smooth": {"scale": 0.5, "quality": 55, "fps": 5},
    "Standard": {"scale": 0.7, "quality": 68, "fps": 8},
    "HD": {"scale": 1.0, "quality": 80, "fps": 10},
}

CAPTURE_MODE_FULL_DESKTOP = "full_desktop"
CAPTURE_MODE_SINGLE_MONITOR = "single_monitor"
CAPTURE_MODE_ACTIVE_WINDOW = "active_window"


@dataclass(slots=True)
class MonitorInfo:
    index: int
    left: int
    top: int
    right: int
    bottom: int
    name: str = ""

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def label(self) -> str:
        device = f" {self.name}" if self.name else ""
        return f"屏幕 {self.index + 1}{device} ({self.width}x{self.height})"


@dataclass(slots=True)
class ScreenCaptureConfig:
    preset: str = "Standard"
    scale: float = 0.7
    jpeg_quality: int = 68
    fps: int = 8
    capture_mode: str = CAPTURE_MODE_FULL_DESKTOP
    monitor_index: int = 0
    window_handle: Optional[int] = None
    window_title: str = ""

    @classmethod
    def from_preset(
        cls,
        preset: str,
        capture_mode: str = CAPTURE_MODE_FULL_DESKTOP,
        monitor_index: int = 0,
        window_handle: Optional[int] = None,
        window_title: str = "",
    ) -> "ScreenCaptureConfig":
        info = QUALITY_PRESETS.get(preset, QUALITY_PRESETS["Standard"])
        return cls(
            preset=preset if preset in QUALITY_PRESETS else "Standard",
            scale=float(info["scale"]),
            jpeg_quality=int(info["quality"]),
            fps=int(info["fps"]),
            capture_mode=capture_mode,
            monitor_index=monitor_index,
            window_handle=window_handle,
            window_title=window_title,
        )


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(RECT),
    ctypes.c_longlong,
)


def normalize_bbox(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = [int(value) for value in bbox]
    if right <= left or bottom <= top:
        raise RuntimeError("Invalid capture rectangle.")
    return left, top, right, bottom



def get_monitor_by_index(monitors: Sequence[MonitorInfo], monitor_index: int) -> MonitorInfo:
    for monitor in monitors:
        if monitor.index == monitor_index:
            return monitor
    if 0 <= monitor_index < len(monitors):
        return monitors[monitor_index]
    raise RuntimeError("The selected monitor is unavailable.")



def list_monitors() -> list[MonitorInfo]:
    if os.name != "nt":
        return []

    user32 = ctypes.windll.user32
    monitors: list[MonitorInfo] = []

    def callback(h_monitor, hdc, lprc_monitor, dw_data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(h_monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                MonitorInfo(
                    index=len(monitors),
                    left=int(rect.left),
                    top=int(rect.top),
                    right=int(rect.right),
                    bottom=int(rect.bottom),
                    name=info.szDevice.strip(),
                )
            )
        return 1

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(callback), 0)
    monitors.sort(key=lambda item: (item.left, item.top, item.index))
    for idx, monitor in enumerate(monitors):
        monitor.index = idx
    return monitors



def get_foreground_window_handle() -> int:
    user32 = ctypes.windll.user32
    handle = int(user32.GetForegroundWindow())
    if handle <= 0:
        raise RuntimeError("No active window is available for capture.")
    return handle



def get_window_title(window_handle: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(window_handle)
    if length <= 0:
        return "当前页面"
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(window_handle, buffer, length + 1)
    return buffer.value.strip() or "当前页面"



def get_window_bbox(window_handle: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    rect = RECT()
    if not user32.GetWindowRect(window_handle, ctypes.byref(rect)):
        raise RuntimeError("Unable to read the active window bounds.")
    return normalize_bbox((rect.left, rect.top, rect.right, rect.bottom))


class ScreenCapturer:
    def __init__(self, config: ScreenCaptureConfig):
        self.config = config

    def _grab_full_desktop(self):
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            return ImageGrab.grab()
        except OSError:
            try:
                return ImageGrab.grab()
            except OSError as exc:
                raise RuntimeError("Screen capture is unavailable in the current Windows session.") from exc

    def _grab_bbox(self, bbox: tuple[int, int, int, int]):
        normalized = normalize_bbox(bbox)
        try:
            return ImageGrab.grab(bbox=normalized, all_screens=True)
        except TypeError:
            return ImageGrab.grab(bbox=normalized)
        except OSError as exc:
            raise RuntimeError("The selected capture target is unavailable.") from exc

    def _grab_image(self):
        if self.config.capture_mode == CAPTURE_MODE_FULL_DESKTOP:
            return self._grab_full_desktop()
        if self.config.capture_mode == CAPTURE_MODE_SINGLE_MONITOR:
            monitors = list_monitors()
            monitor = get_monitor_by_index(monitors, self.config.monitor_index)
            return self._grab_bbox(monitor.bbox)
        if self.config.capture_mode == CAPTURE_MODE_ACTIVE_WINDOW:
            if not self.config.window_handle:
                raise RuntimeError("No active window has been selected for capture.")
            return self._grab_bbox(get_window_bbox(self.config.window_handle))
        raise RuntimeError(f"Unsupported capture mode: {self.config.capture_mode}")

    def capture_jpeg(self) -> bytes:
        image = self._grab_image()

        if self.config.scale < 1.0:
            width = max(1, int(image.width * self.config.scale))
            height = max(1, int(image.height * self.config.scale))
            image = image.resize((width, height))

        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=self.config.jpeg_quality, optimize=True)
        return buffer.getvalue()
