from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass
class ConsoleFontStatus:
    supported: bool
    applied: bool
    requested_size: int
    current_size: int
    face_name: str
    message: str


_LAST_FONT_STATUS = ConsoleFontStatus(False, False, 0, 0, "", "not attempted")


def configure_windows_console(font_size: int | None = None) -> None:
    """Best-effort Windows console setup for UTF-8, ANSI, and font size."""

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)

        stdout_handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
            enable_virtual_terminal = 0x0004
            kernel32.SetConsoleMode(stdout_handle, mode.value | enable_virtual_terminal)
    except Exception:
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if font_size is None:
        font_size = _env_font_size()
    if font_size:
        set_windows_console_font_size(font_size)


def console_font_status() -> ConsoleFontStatus:
    return _LAST_FONT_STATUS


def _env_font_size() -> int:
    raw = os.environ.get("LILBOT_FONT_SIZE") or os.environ.get("LILBOT_CONSOLE_FONT_SIZE") or ""
    if not raw.strip():
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 0


def set_windows_console_font_size(size: int) -> ConsoleFontStatus:
    """Request a larger font from classic Windows console hosts.

    Windows Terminal owns its own profile font size and can ignore this Win32
    console-buffer API when running through ConPTY. We still record the result
    so `/display` can tell whether the host accepted, clamped, or rejected it.
    """

    global _LAST_FONT_STATUS
    if os.name != "nt":
        _LAST_FONT_STATUS = ConsoleFontStatus(False, False, size, 0, "", "not windows")
        return _LAST_FONT_STATUS
    if size <= 0:
        _LAST_FONT_STATUS = ConsoleFontStatus(True, False, size, 0, "", "disabled")
        return _LAST_FONT_STATUS
    try:
        import ctypes

        lf_facesize = 32
        std_output_handle = -11

        class Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class ConsoleFontInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("nFont", ctypes.c_ulong),
                ("dwFontSize", Coord),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * lf_facesize),
            ]

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(std_output_handle)
        info = ConsoleFontInfoEx()
        info.cbSize = ctypes.sizeof(ConsoleFontInfoEx)
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            _LAST_FONT_STATUS = ConsoleFontStatus(False, False, size, 0, "", "GetCurrentConsoleFontEx failed")
            return _LAST_FONT_STATUS

        before_y = int(info.dwFontSize.Y)
        face_name = str(info.FaceName).rstrip("\x00")
        info.dwFontSize.Y = int(size)
        if not kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            _LAST_FONT_STATUS = ConsoleFontStatus(
                True,
                False,
                size,
                before_y,
                face_name,
                "SetCurrentConsoleFontEx failed",
            )
            return _LAST_FONT_STATUS

        after = ConsoleFontInfoEx()
        after.cbSize = ctypes.sizeof(ConsoleFontInfoEx)
        if kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(after)):
            after_y = int(after.dwFontSize.Y)
            after_face = str(after.FaceName).rstrip("\x00")
        else:
            after_y = size
            after_face = face_name
        _LAST_FONT_STATUS = ConsoleFontStatus(
            True,
            after_y >= size,
            size,
            after_y,
            after_face,
            "applied" if after_y >= size else "host ignored or clamped request",
        )
        return _LAST_FONT_STATUS
    except Exception as exc:
        _LAST_FONT_STATUS = ConsoleFontStatus(False, False, size, 0, "", f"{type(exc).__name__}: {exc}")
        return _LAST_FONT_STATUS
