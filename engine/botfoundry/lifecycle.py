"""Shut the engine down if the app that launched it disappears (crash, Ctrl+C, killed)."""
import os
import sys
import threading
from typing import Callable


def is_process_alive(pid: int) -> bool:
    """True if a process with this id currently exists."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by someone else
    return True


def watch_parent(
    pid: int,
    on_gone: Callable[[], None],
    interval: float = 2.0,
    stop: threading.Event | None = None,
) -> threading.Thread:
    """Poll `pid` in a background thread; call `on_gone` once when it no longer exists."""
    stop_event = stop or threading.Event()

    def loop() -> None:
        while not stop_event.wait(interval):
            if not is_process_alive(pid):
                on_gone()
                return

    thread = threading.Thread(target=loop, name="parent-watchdog", daemon=True)
    thread.start()
    return thread
