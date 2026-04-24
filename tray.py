"""tray.py — system tray mode for ai-fuelgauge.

Runs a small icon in the Windows system tray / macOS menu bar / Linux tray.
Polls quota every N minutes and updates the icon color:
  - green  < 70%
  - orange 70..89%
  - red    >= 90%

Fires a desktop notification when the 5h window crosses 80% or the weekly
window crosses 90% (fired once per threshold crossing, reset on drop).

Requires: pystray, Pillow (+ winotify on Windows, plyer elsewhere).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# Optional runtime deps. Let ImportError propagate to the caller — the wrapper
# in ai_fuelgauge.py surfaces a unified install message pointing at
# requirements-tray.txt, which covers the platform-conditional winotify/plyer.
import pystray
from PIL import Image, ImageDraw

# Platform-native notification backend.
_NOTIFY = None
if sys.platform == "win32":
    try:
        from winotify import Notification as _WinNotify  # type: ignore
        _NOTIFY = "winotify"
    except ImportError:
        pass
if _NOTIFY is None:
    try:
        from plyer import notification as _PlyerNotify  # type: ignore
        _NOTIFY = "plyer"
    except ImportError:
        pass


def _notify(title: str, message: str) -> None:
    if _NOTIFY == "winotify":
        try:
            _WinNotify(app_id="ai-fuelgauge", title=title, msg=message).show()
        except Exception as e:
            sys.stderr.write(f"winotify failed: {e}\n")
    elif _NOTIFY == "plyer":
        try:
            _PlyerNotify.notify(title=title, message=message, app_name="ai-fuelgauge")
        except Exception as e:
            sys.stderr.write(f"plyer failed: {e}\n")
    else:
        sys.stderr.write(f"NOTIFY (no backend): {title} — {message}\n")


# Reuse probe functions from sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_fuelgauge as afg  # noqa: E402


DEFAULT_INTERVAL_SECONDS = 300
THRESHOLD_PRIMARY_PCT = 80   # 5h window
THRESHOLD_SECONDARY_PCT = 90  # weekly window
HYSTERESIS_PCT = 10           # must drop this far below threshold before re-notifying

# Cross-platform single-instance guard. Advisory lock on this file is held for
# the lifetime of the process; the OS releases it on exit (including crash),
# so there is no stale-pid cleanup to manage.
_LOCK_FILE = Path.home() / ".cache" / "ai-fuelgauge-tray.lock"


def _reexec_detached_on_windows(argv: "list[str]") -> bool:
    """If launched via console `python.exe` on Windows, relaunch as `pythonw.exe`
    detached from the parent console and return True (caller should exit).

    Without this, closing the terminal that ran `python ai_fuelgauge.py --tray`
    kills the tray because `python.exe` is a console-subsystem binary tied to
    its parent console. `pythonw.exe` is a Windows-subsystem binary with no
    console attachment, so it survives.

    Skipped for: non-Windows, already-pythonw, frozen PyInstaller binaries,
    `--no-detach` flag (handled by caller before invoking this).
    """
    if sys.platform != "win32":
        return False
    exe = Path(sys.executable)
    if exe.name.lower() != "python.exe":
        return False  # already pythonw, or frozen binary — no re-exec needed
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        return False  # some stripped installs lack pythonw; just run foreground
    import subprocess
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


def _acquire_single_instance_lock() -> "int | None":
    """Return an open fd that must be kept alive, or None if another tray holds the lock."""
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        fd = os.open(str(_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return None
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
    return fd


def _make_icon(max_pct: float, size: int = 64) -> "Image.Image":
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if max_pct >= 90:
        fill = (231, 76, 60, 255)    # red
    elif max_pct >= 70:
        fill = (243, 156, 18, 255)   # orange
    else:
        fill = (46, 204, 113, 255)   # green
    margin = 6
    d.ellipse([margin, margin, size - margin, size - margin], fill=fill)
    return img


def _snapshot() -> dict:
    codex = afg.probe_codex_fresh()
    if not codex or codex.get("error"):
        fallback = afg.read_codex_quota()
        if fallback:
            codex = fallback
            if isinstance(codex, dict):
                codex["_source"] = "sqlite-snapshot"
    claude = afg.probe_claude_quota()
    return {"codex": codex, "claude": claude}


def _max_pct(snap: dict) -> float:
    m = 0.0
    for key in ("codex", "claude"):
        d = snap.get(key) or {}
        if isinstance(d, dict):
            for w in ("primary", "secondary"):
                p = (d.get(w) or {}).get("used_percent")
                if p is not None:
                    try:
                        m = max(m, float(p))
                    except (TypeError, ValueError):
                        pass
    return m


def _summary_line(snap: dict) -> str:
    parts = []
    for label, key in (("Codex", "codex"), ("Claude", "claude")):
        d = snap.get(key) or {}
        if not d or (isinstance(d, dict) and d.get("error")):
            parts.append(f"{label} ?")
            continue
        p = (d.get("primary") or {}).get("used_percent")
        s = (d.get("secondary") or {}).get("used_percent")
        p_str = f"{p:.0f}%" if p is not None else "?"
        s_str = f"{s:.0f}%" if s is not None else "?"
        parts.append(f"{label} {p_str}/{s_str}")
    return " | ".join(parts)


def _pct_or_none(d, window) -> "float | None":
    if not isinstance(d, dict):
        return None
    w = d.get(window) or {}
    p = w.get("used_percent")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


class TrayApp:
    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self.snapshot: dict = {}
        self._notified_primary = False
        self._notified_secondary = False
        self.icon: "pystray.Icon | None" = None
        self._stop = threading.Event()
        # Non-blocking guard: held while a fetch is in flight so repeated menu
        # clicks (or a poller tick racing a manual refresh) skip instead of
        # stacking concurrent probes against the Codex / Claude APIs.
        self._fetch_lock = threading.Lock()

    # --- menu item text getters (callables so they re-evaluate on menu open) ---
    def _codex_5h(self, _item):
        p = _pct_or_none(self.snapshot.get("codex"), "primary")
        return f"Codex 5h: {p:.0f}%" if p is not None else "Codex 5h: ?"

    def _codex_week(self, _item):
        p = _pct_or_none(self.snapshot.get("codex"), "secondary")
        return f"Codex week: {p:.0f}%" if p is not None else "Codex week: ?"

    def _claude_5h(self, _item):
        p = _pct_or_none(self.snapshot.get("claude"), "primary")
        return f"Claude 5h: {p:.0f}%" if p is not None else "Claude 5h: ?"

    def _claude_week(self, _item):
        p = _pct_or_none(self.snapshot.get("claude"), "secondary")
        return f"Claude week: {p:.0f}%" if p is not None else "Claude week: ?"

    # --- actions ---
    def _refresh_now(self, _icon=None, _item=None):
        threading.Thread(target=self._do_fetch, daemon=True).start()

    def _quit(self, _icon=None, _item=None):
        self._stop.set()
        if self.icon:
            self.icon.stop()

    # --- core ---
    def _do_fetch(self):
        if not self._fetch_lock.acquire(blocking=False):
            return  # another fetch is in flight — skip to avoid overlap
        try:
            snap = _snapshot()
        except Exception as e:
            sys.stderr.write(f"fetch failed: {e}\n")
        else:
            self.snapshot = snap
            self._check_thresholds(snap)
            self._apply_to_icon()
        finally:
            self._fetch_lock.release()

    def _check_thresholds(self, snap: dict) -> None:
        max_p = 0.0
        max_s = 0.0
        for key in ("codex", "claude"):
            d = snap.get(key) or {}
            p = _pct_or_none(d, "primary") or 0.0
            s = _pct_or_none(d, "secondary") or 0.0
            max_p = max(max_p, p)
            max_s = max(max_s, s)

        if max_p >= THRESHOLD_PRIMARY_PCT and not self._notified_primary:
            _notify("AI quota warning", f"5-hour window at {max_p:.0f}%")
            self._notified_primary = True
        elif max_p < THRESHOLD_PRIMARY_PCT - HYSTERESIS_PCT:
            self._notified_primary = False

        if max_s >= THRESHOLD_SECONDARY_PCT and not self._notified_secondary:
            _notify("AI quota warning", f"Weekly window at {max_s:.0f}%")
            self._notified_secondary = True
        elif max_s < THRESHOLD_SECONDARY_PCT - HYSTERESIS_PCT:
            self._notified_secondary = False

    def _apply_to_icon(self):
        if not self.icon:
            return
        m = _max_pct(self.snapshot)
        self.icon.icon = _make_icon(m)
        self.icon.title = _summary_line(self.snapshot)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _poller(self):
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            self._do_fetch()

    def _menu(self) -> "pystray.Menu":
        return pystray.Menu(
            pystray.MenuItem(self._codex_5h, None, enabled=False),
            pystray.MenuItem(self._codex_week, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._claude_5h, None, enabled=False),
            pystray.MenuItem(self._claude_week, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", self._refresh_now),
            pystray.MenuItem("Quit", self._quit),
        )

    def run(self):
        # Initial synchronous fetch so the first icon is meaningful.
        self._do_fetch()
        self.icon = pystray.Icon(
            "ai-fuelgauge",
            icon=_make_icon(_max_pct(self.snapshot)),
            title=_summary_line(self.snapshot),
            menu=self._menu(),
        )
        threading.Thread(target=self._poller, daemon=True).start()
        self.icon.run()  # blocks until _quit()


def run_tray(interval: int = DEFAULT_INTERVAL_SECONDS, detach: bool = True) -> int:
    # Detach BEFORE acquiring the lock so the exiting parent never holds it.
    # The detached pythonw child re-enters run_tray and reaches acquire next.
    if detach and _reexec_detached_on_windows(sys.argv):
        return 0

    lock_fd = _acquire_single_instance_lock()
    if lock_fd is None:
        # Another tray is already running. Silent exit — pythonw.exe has no
        # visible stderr, and double-launching via usage-tray should just be a no-op.
        sys.stderr.write("ai-fuelgauge tray is already running.\n")
        return 0
    try:
        TrayApp(interval=interval).run()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ai-fuelgauge tray mode")
    ap.add_argument("--interval", type=afg.interval_seconds, default=DEFAULT_INTERVAL_SECONDS,
                    help="poll interval in seconds (default: 300, minimum: 1)")
    ap.add_argument("--no-detach", action="store_true",
                    help="(Windows) run in the foreground instead of auto-relaunching as pythonw detached — "
                         "useful for debugging so stderr stays visible")
    args = ap.parse_args()
    sys.exit(run_tray(interval=args.interval, detach=not args.no_detach))
