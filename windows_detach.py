"""Windows pythonw.exe re-exec helper for long-running GUI modes."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def reexec_detached_on_windows(argv: "list[str]") -> bool:
    """Relaunch console python.exe as detached pythonw.exe on Windows.

    Returns True in the original parent when a detached child was started and
    the caller should exit. Returns False on non-Windows, already-pythonw,
    frozen binaries, missing pythonw.exe, or spawn failure.
    """
    if sys.platform != "win32":
        return False
    exe = Path(sys.executable)
    if exe.name.lower() != "python.exe":
        return False
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        return False
    try:
        subprocess.Popen(
            [str(pythonw), *argv],
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            ),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return True
