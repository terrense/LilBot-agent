from __future__ import annotations

import os
import sys


def configure_windows_console() -> None:
    """Make Windows consoles friendlier to Rich/ANSI output.

    PowerShell on Windows can otherwise display UTF-8 box drawing bytes as
    mojibake such as "鈺...". This is a best-effort setup; if it fails, the
    app still runs and the user can start PowerShell with `chcp 65001`.
    """

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

