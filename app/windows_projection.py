from __future__ import annotations

import ctypes
import locale
import os
import platform
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CREATE_NO_WINDOW = 0x08000000
DISPLAY_SWITCH_ARGS = {
    "clone": "/clone",
    "extend": "/extend",
    "internal": "/internal",
    "external": "/external",
}


@dataclass(slots=True)
class ProjectionDiagnostics:
    os_version: str
    wlan_interface: str = ""
    wireless_display_supported: Optional[bool] = None
    graphics_driver_supported: Optional[bool] = None
    wifi_driver_supported: Optional[bool] = None
    miracast_available: Optional[bool] = None
    hdcp_supported: Optional[bool] = None
    netsh_summary: str = ""
    miracast_summary: str = ""
    raw_netsh: str = ""
    raw_dxdiag: str = ""
    warnings: list[str] = field(default_factory=list)


def _run_command(command: list[str], encoding: Optional[str] = None) -> str:
    resolved_encoding = encoding
    if resolved_encoding is None:
        if os.name == "nt":
            resolved_encoding = "gbk"
        else:
            resolved_encoding = locale.getpreferredencoding(False)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding=resolved_encoding,
        errors="ignore",
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0 and not output:
        raise RuntimeError(f"Command failed: {' '.join(command)}")
    return output


def _parse_bool_token(token: str) -> Optional[bool]:
    value = token.strip().lower()
    negative_terms = ("no", "not supported", "unsupported", "false", "否", "不支持", "不可用")
    positive_terms = ("yes", "available", "supported", "true", "是", "支持", "可用")
    if any(term in value for term in negative_terms):
        return False
    if any(term in value for term in positive_terms):
        return True
    return None


def parse_netsh_output(text: str) -> dict[str, object]:
    info: dict[str, object] = {
        "wlan_interface": "",
        "wireless_display_supported": None,
        "graphics_driver_supported": None,
        "wifi_driver_supported": None,
        "netsh_summary": "",
    }
    interface_match = re.search(r"(?:Interface name|接口名称)\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if interface_match:
        info["wlan_interface"] = interface_match.group(1).strip()

    summary_line = ""
    for line in text.splitlines():
        if "Wireless Display Supported" in line or "无线显示" in line:
            summary_line = line.strip()
            break

    if not summary_line:
        return info

    info["netsh_summary"] = summary_line
    _, _, tail = summary_line.partition(":")
    info["wireless_display_supported"] = _parse_bool_token(tail)

    graphics_match = re.search(r"(?:Graphics Driver|图形驱动程序)\s*:\s*([^) ，,]+)", tail, flags=re.IGNORECASE)
    wifi_match = re.search(r"(?:Wi-?Fi Driver|WLAN 驱动程序)\s*:\s*([^) ，,]+)", tail, flags=re.IGNORECASE)
    if graphics_match:
        info["graphics_driver_supported"] = _parse_bool_token(graphics_match.group(1))
    if wifi_match:
        info["wifi_driver_supported"] = _parse_bool_token(wifi_match.group(1))
    return info


def parse_dxdiag_output(text: str) -> dict[str, object]:
    miracast_lines = [line.strip() for line in text.splitlines() if "Miracast" in line]
    if not miracast_lines:
        return {
            "miracast_available": None,
            "hdcp_supported": None,
            "miracast_summary": "",
        }

    preferred_line = next(
        (line for line in miracast_lines if re.search(r"\bAvailable\b", line, flags=re.IGNORECASE) or "可用" in line),
        miracast_lines[0],
    )
    lower_line = preferred_line.lower()
    available = None
    if "available" in lower_line or "可用" in preferred_line:
        available = True
    elif "not supported" in lower_line or "不支持" in preferred_line:
        available = False

    hdcp_supported = None
    if "hdcp" in lower_line:
        hdcp_supported = "without hdcp" not in lower_line and "no hdcp" not in lower_line

    return {
        "miracast_available": available,
        "hdcp_supported": hdcp_supported,
        "miracast_summary": preferred_line,
    }


def get_os_version_text() -> str:
    return f"{platform.system()} {platform.release()} ({platform.version()})"


def collect_projection_diagnostics() -> ProjectionDiagnostics:
    diagnostics = ProjectionDiagnostics(os_version=get_os_version_text())

    try:
        netsh_output = _run_command(["netsh", "wlan", "show", "drivers"], encoding="utf-8")
        diagnostics.raw_netsh = netsh_output
        parsed_netsh = parse_netsh_output(netsh_output)
        diagnostics.wlan_interface = str(parsed_netsh["wlan_interface"])
        diagnostics.wireless_display_supported = parsed_netsh["wireless_display_supported"]  # type: ignore[assignment]
        diagnostics.graphics_driver_supported = parsed_netsh["graphics_driver_supported"]  # type: ignore[assignment]
        diagnostics.wifi_driver_supported = parsed_netsh["wifi_driver_supported"]  # type: ignore[assignment]
        diagnostics.netsh_summary = str(parsed_netsh["netsh_summary"])
    except Exception as exc:
        diagnostics.warnings.append(f"netsh 检测失败：{exc}")

    try:
        temp_file = Path(tempfile.gettempdir()) / "screen_casting_dxdiag.txt"
        _run_command(["dxdiag", "/t", str(temp_file)], encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not temp_file.exists():
            time.sleep(0.1)
        dxdiag_output = temp_file.read_text(encoding="utf-8", errors="ignore") if temp_file.exists() else ""
        diagnostics.raw_dxdiag = dxdiag_output
        parsed_dxdiag = parse_dxdiag_output(dxdiag_output)
        diagnostics.miracast_available = parsed_dxdiag["miracast_available"]  # type: ignore[assignment]
        diagnostics.hdcp_supported = parsed_dxdiag["hdcp_supported"]  # type: ignore[assignment]
        diagnostics.miracast_summary = str(parsed_dxdiag["miracast_summary"])
    except Exception as exc:
        diagnostics.warnings.append(f"dxdiag 检测失败：{exc}")

    if diagnostics.wireless_display_supported is False:
        diagnostics.warnings.append("Wi‑Fi 驱动报告当前设备不支持无线显示。")
    if diagnostics.graphics_driver_supported is False:
        diagnostics.warnings.append("显卡驱动未满足 Miracast 要求。")
    if diagnostics.wifi_driver_supported is False:
        diagnostics.warnings.append("无线网卡驱动未满足 Miracast 要求。")
    if diagnostics.miracast_available is False:
        diagnostics.warnings.append("dxdiag 报告 Miracast 当前不可用。")
    if not diagnostics.warnings and diagnostics.miracast_available:
        diagnostics.warnings.append("当前检测结果显示可以尝试 Win+K 连接无线显示器。")

    return diagnostics


def open_settings_uri(uri: str) -> None:
    if not hasattr(os, "startfile"):
        raise RuntimeError("当前系统不支持 Windows 设置 URI。")
    os.startfile(uri)  # type: ignore[attr-defined]


def open_display_settings() -> None:
    open_settings_uri("ms-settings:display")


def open_project_settings() -> None:
    open_settings_uri("ms-settings:project")


def open_connect_panel() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    keybd_event = user32.keybd_event
    keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_ulong]
    keybd_event.restype = None

    vk_lwin = 0x5B
    vk_k = 0x4B
    key_up = 0x0002

    keybd_event(vk_lwin, 0, 0, 0)
    keybd_event(vk_k, 0, 0, 0)
    keybd_event(vk_k, 0, key_up, 0)
    keybd_event(vk_lwin, 0, key_up, 0)


def get_display_switch_arg(mode: str) -> str:
    if mode not in DISPLAY_SWITCH_ARGS:
        raise ValueError(f"Unsupported projection mode: {mode}")
    return DISPLAY_SWITCH_ARGS[mode]


def switch_projection_mode(mode: str) -> None:
    arg = get_display_switch_arg(mode)
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    display_switch = os.path.join(system_root, "System32", "DisplaySwitch.exe")
    subprocess.Popen([display_switch, arg], creationflags=CREATE_NO_WINDOW)
