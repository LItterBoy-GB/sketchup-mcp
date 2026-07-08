import ctypes
import socket
import sys
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional


class ModalGuardInterrupted(Exception):
    def __init__(self, modal: Dict[str, Any], status: str = "interrupted_by_modal"):
        self.status = status
        self.modal = modal
        title = modal.get("title") or modal.get("hwnd") or "unknown modal"
        super().__init__(f"eval_ruby {status}: {title}")

    def to_payload(self) -> Dict[str, Any]:
        return {
            "success": False,
            "status": self.status,
            "error": str(self),
            "modal": self.modal,
        }


def payload_from_exception(exc: BaseException) -> Optional[Dict[str, Any]]:
    if isinstance(exc, ModalGuardInterrupted):
        return exc.to_payload()
    return None


def interrupt_modal_for_port(host: str, port: int, request_id: Any = None) -> Optional[Dict[str, Any]]:
    if sys.platform != "win32" or host not in {"localhost", "127.0.0.1", "::1"}:
        return None

    pid = find_pid_for_local_port(port)
    if pid is None:
        return None

    modal = detect_modal_for_pid(pid, request_id=request_id)
    if modal is None:
        return None

    hwnd = int(modal["hwnd"], 16)
    action = close_modal_window(hwnd)
    modal["action"] = action
    return modal


def find_pid_for_local_port(port: int) -> Optional[int]:
    if sys.platform != "win32":
        return None

    iphlpapi = ctypes.WinDLL("iphlpapi")
    AF_INET = 2
    TCP_TABLE_OWNER_PID_ALL = 5
    ERROR_INSUFFICIENT_BUFFER = 122

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    size = wintypes.DWORD(0)
    result = iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if result != ERROR_INSUFFICIENT_BUFFER:
        return None

    buffer = ctypes.create_string_buffer(size.value)
    result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if result != 0:
        return None

    row_count = wintypes.DWORD.from_buffer_copy(buffer.raw[: ctypes.sizeof(wintypes.DWORD)]).value
    offset = ctypes.sizeof(wintypes.DWORD)
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)

    for index in range(row_count):
        row = MIB_TCPROW_OWNER_PID.from_buffer_copy(buffer.raw, offset + index * row_size)
        local_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
        if local_port == port:
            return int(row.dwOwningPid)

    return None


def detect_modal_for_pid(pid: int, request_id: Any = None) -> Optional[Dict[str, Any]]:
    windows = _visible_windows_for_pid(pid)
    if not windows:
        return None

    ownerless_windows = [window for window in windows if not window["owner"]]
    ownerless_last_active_popups = {
        window.get("last_active_popup")
        for window in ownerless_windows
        if window.get("last_active_popup") and window.get("last_active_popup") != window["hwnd_int"]
    }
    main_windows = [
        window
        for window in ownerless_windows
        if window["hwnd_int"] not in ownerless_last_active_popups
    ]
    main_hwnds = {window["hwnd_int"] for window in main_windows}
    last_active_popups = {
        window.get("last_active_popup")
        for window in main_windows
        if window.get("last_active_popup") and window.get("last_active_popup") != window["hwnd_int"]
    }
    main_disabled = any(not window["enabled"] for window in main_windows)

    popups = [
        window
        for window in windows
        if window["hwnd_int"] not in main_hwnds
        and (
            window["owner"] in main_hwnds
            or window["root_owner"] in main_hwnds
            or window["hwnd_int"] in last_active_popups
            or window["class"] == "#32770"
        )
    ]

    if not main_disabled and not popups:
        return None

    candidate = popups[0] if popups else main_windows[0]
    return {
        "pid": pid,
        "hwnd": candidate["hwnd"],
        "class": candidate["class"],
        "title": candidate["title"],
        "main_disabled": main_disabled,
        "request_id": request_id,
    }


def close_modal_window(hwnd: int) -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    WM_CLOSE = 0x0010
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    VK_ESCAPE = 0x1B

    if user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0):
        time.sleep(0.05)
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            return "wm_close"

    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    user32.PostMessageW(wintypes.HWND(hwnd), WM_KEYDOWN, VK_ESCAPE, 0)
    user32.PostMessageW(wintypes.HWND(hwnd), WM_KEYUP, VK_ESCAPE, 0)
    return "escape"


def _visible_windows_for_pid(pid: int) -> List[Dict[str, Any]]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    GW_OWNER = 4
    GA_ROOTOWNER = 3
    windows: List[Dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != pid:
            return True

        windows.append(
            {
                "hwnd": f"0x{int(hwnd):08x}",
                "hwnd_int": int(hwnd),
                "title": _window_text(user32, hwnd),
                "class": _class_name(user32, hwnd),
                "enabled": bool(user32.IsWindowEnabled(hwnd)),
                "owner": int(user32.GetWindow(hwnd, GW_OWNER)),
                "root_owner": int(user32.GetAncestor(hwnd, GA_ROOTOWNER)),
                "last_active_popup": int(user32.GetLastActivePopup(hwnd)),
            }
        )
        return True

    user32.EnumWindows(enum_proc, 0)
    return windows


def _window_text(user32, hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _class_name(user32, hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value
