"""hud.py - optional floating quota overlay for ai-fuelgauge."""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - surfaced by run_hud()
    tk = None  # type: ignore


sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_fuelgauge as afg  # noqa: E402
import quota_state  # noqa: E402


HUD_STATE_FILE = Path.home() / ".cache" / "ai-fuelgauge-hud.json"
HUD_STATE_LOCK_FILE = Path.home() / ".cache" / "ai-fuelgauge-hud-state.lock"
HUD_LOCK_FILE = Path.home() / ".cache" / "ai-fuelgauge-hud.lock"
HUD_CLOSE_REQUEST_FILE = Path.home() / ".cache" / "ai-fuelgauge-hud-close.json"
DEFAULT_INTERVAL_SECONDS = 300
CACHE_SYNC_INTERVAL_SECONDS = 5
COMMAND_POLL_INTERVAL_MS = 1000
WINDOW_REASSERT_INTERVAL_MS = 3000
DRAG_THRESHOLD_PX = 4
HUD_CACHE_ONLY_ENV = "AI_FUELGAUGE_HUD_CACHE_ONLY"
DEFAULT_OPACITY = 0.88
MIN_OPACITY = 0.25
MAX_OPACITY = 1.0
OPACITY_STEP = 0.10
OPACITY_PRESETS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)

MODE_COMPACT = "compact"
MODE_EXPANDED = "expanded"


def acquire_hud_lock(path: Path = HUD_LOCK_FILE) -> "int | None":
    """Return an fd holding the singleton HUD lock, or None if one is running."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
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


def release_hud_lock(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _state_lock_path(path: Path = HUD_STATE_FILE) -> Path:
    if path == HUD_STATE_FILE:
        return HUD_STATE_LOCK_FILE
    return path.with_name(path.name + ".lock")


def _acquire_state_lock(path: Path = HUD_STATE_FILE) -> "int | None":
    lock_path = _state_lock_path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_state_lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def clear_hud_close_request(path: Path = HUD_CLOSE_REQUEST_FILE) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def request_hud_close(path: Path = HUD_CLOSE_REQUEST_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"command": "quit", "requested_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def consume_hud_close_request(started_at: float,
                              path: Path = HUD_CLOSE_REQUEST_FILE) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:
        clear_hud_close_request(path)
        return False
    if not isinstance(data, dict) or data.get("command") != "quit":
        clear_hud_close_request(path)
        return False
    requested_at = data.get("requested_at")
    clear_hud_close_request(path)
    if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool):
        return False
    return float(requested_at) >= started_at - 2


def _finite_pct(value) -> "float | None":
    if value is None or isinstance(value, bool):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(pct) or math.isinf(pct):
        return None
    return pct


def _block_pct(block: "dict | None") -> "float | None":
    if not isinstance(block, dict):
        return None
    return _finite_pct(block.get("used_percent"))


def _block_reset_text(block: "dict | None") -> str:
    if not isinstance(block, dict):
        return "-"
    reset_at = block.get("reset_at")
    reset_in = block.get("reset_in_seconds")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        reset_in = int(reset_at) - int(time.time())
    if isinstance(reset_in, (int, float)) and not isinstance(reset_in, bool):
        return afg.fmt_duration(int(reset_in))
    return "-"


def _claude_stale_window(window: str) -> "dict | None":
    last_good = afg._load_last_good_claude()
    if not isinstance(last_good, dict):
        return None
    block = last_good.get(window)
    if afg._stale_bar_status(block) != "valid":
        return None
    return block if isinstance(block, dict) else None


def _claude_should_use_stale(provider: "dict | None") -> bool:
    if not isinstance(provider, dict):
        return False
    err = provider.get("error")
    if err in ("auth-expired-no-refresh", "env-token-expired"):
        return True
    return provider.get("status") == 401 and bool(provider.get("_env_token_mode"))


def _provider_error_row(label: str, provider: "dict | None") -> "str | None":
    if not isinstance(provider, dict) or not provider:
        return None
    err = provider.get("error") or ""
    status = provider.get("status")
    if label == "Claude":
        if status == 429:
            return "Claude rate limited"
        if err == "env-token-expired":
            return "Claude env token expired"
        if err == "auth-expired-no-refresh":
            return "Claude token expired"
        if status == 401:
            return "Claude auth expired"
        if isinstance(status, int) and status >= 400:
            return f"Claude HTTP {status}"
        if err == "no-token-found":
            return "Claude not logged in"
        if err.startswith("probe-failed"):
            return "Claude probe failed"
    elif label == "Codex":
        if err == "codex-not-in-path":
            return "Codex CLI not found"
        if err.startswith("spawn-failed"):
            return "Codex spawn failed"
        if err == "no-response-from-app-server":
            return "Codex no response"
        if err.startswith("jsonrpc-error"):
            return "Codex JSON-RPC error"
        if err == "empty-rateLimits":
            return "Codex empty response"
        if err.startswith("codex sqlite"):
            return "Codex sqlite error"
    if err:
        return f"{label} {err[:24]}"
    return None


def _format_window_row(label: str, window_label: str, block: "dict | None",
                       stale: bool = False) -> str:
    pct = _block_pct(block)
    if pct is None:
        pct_text = " --"
        reset_text = "-"
    else:
        pct_text = f"{pct:3.0f}%"
        reset_text = _block_reset_text(block)
    suffix = " stale" if stale else ""
    return f"{label:<6} {window_label:<4} {pct_text:>4} {reset_text:>6}{suffix}"


def _format_row(label: str, window_label: str, provider: "dict | None", window: str,
                stale: bool = False) -> str:
    block = provider.get(window) if isinstance(provider, dict) else None
    return _format_window_row(label, window_label, block, stale=stale)


def format_hud_rows(snapshot: dict, mode: str = MODE_COMPACT) -> "list[str]":
    """Return fixed-width HUD rows for compact or expanded mode."""
    codex = snapshot.get("codex") if isinstance(snapshot, dict) else {}
    claude = snapshot.get("claude") if isinstance(snapshot, dict) else {}
    codex_error = _provider_error_row("Codex", codex)
    claude_error = _provider_error_row("Claude", claude)
    rows = [
        codex_error or _format_row("Codex", "5h", codex, "primary"),
        claude_error or _format_row("Claude", "5h", claude, "primary"),
    ]
    if _claude_should_use_stale(claude):
        stale_primary = _claude_stale_window("primary")
        rows[1] = (
            _format_window_row("Claude", "5h", stale_primary, stale=True)
            if stale_primary else claude_error or _format_row("Claude", "5h", None, "primary")
        )

    if mode == MODE_EXPANDED:
        codex_week = _format_row("Codex", "week", codex, "secondary")
        claude_week = _format_row("Claude", "week", claude, "secondary")
        if _claude_should_use_stale(claude):
            stale_secondary = _claude_stale_window("secondary")
            claude_week = (
                _format_window_row("Claude", "week", stale_secondary, stale=True)
                if stale_secondary else _format_row("Claude", "week", None, "secondary")
            )
        rows = [rows[0], codex_week, rows[1], claude_week]
    return rows


def toggle_mode(mode: str) -> str:
    return MODE_EXPANDED if mode == MODE_COMPACT else MODE_COMPACT


def is_drag(start: "tuple[int, int]", end: "tuple[int, int]",
            threshold: int = DRAG_THRESHOLD_PX) -> bool:
    return abs(end[0] - start[0]) > threshold or abs(end[1] - start[1]) > threshold


def _load_state(path: Path = HUD_STATE_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_position(path: Path = HUD_STATE_FILE) -> "tuple[int, int] | None":
    data = _load_state(path)
    if not data:
        return None
    x = data.get("x")
    y = data.get("y")
    if isinstance(x, int) and not isinstance(x, bool) and isinstance(y, int) and not isinstance(y, bool):
        return x, y
    return None


def clamp_position(pos: "tuple[int, int]", screen_width: int, screen_height: int,
                   margin: int = 24) -> "tuple[int, int]":
    """Keep a frameless HUD recoverable after monitor/layout changes."""
    return clamp_position_to_bounds(pos, 0, 0, screen_width, screen_height, margin)


def clamp_position_to_bounds(pos: "tuple[int, int]",
                             min_x: int,
                             min_y: int,
                             width: int,
                             height: int,
                             margin: int = 24) -> "tuple[int, int]":
    """Keep a frameless HUD recoverable inside a desktop coordinate rectangle."""
    x, y = pos
    min_x = int(min_x)
    min_y = int(min_y)
    max_x = max(min_x, min_x + int(width) - margin)
    max_y = max(min_y, min_y + int(height) - margin)
    return min(max(min_x, int(x)), max_x), min(max(min_y, int(y)), max_y)


def clamp_opacity(value) -> float:
    pct = _finite_pct(value)
    if pct is None:
        return DEFAULT_OPACITY
    return min(max(pct, MIN_OPACITY), MAX_OPACITY)


def adjust_opacity_value(current, steps: int) -> float:
    return clamp_opacity(round(clamp_opacity(current) + (steps * OPACITY_STEP), 2))


def load_opacity(path: Path = HUD_STATE_FILE) -> float:
    data = _load_state(path)
    if not data:
        return DEFAULT_OPACITY
    return clamp_opacity(data.get("opacity"))


def save_state(x: "int | None" = None, y: "int | None" = None,
               opacity: "float | None" = None,
               visible: "bool | None" = None,
               path: Path = HUD_STATE_FILE) -> None:
    lock_fd = _acquire_state_lock(path)
    try:
        data = _load_state(path)
        if x is not None and y is not None:
            data["x"] = int(x)
            data["y"] = int(y)
        if opacity is not None:
            data["opacity"] = clamp_opacity(opacity)
        if visible is not None:
            data["visible"] = bool(visible)
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data))
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        pass
    finally:
        if lock_fd is not None:
            _release_state_lock(lock_fd)


def save_position(x: int, y: int, path: Path = HUD_STATE_FILE) -> None:
    save_state(x=x, y=y, path=path)


def save_opacity(opacity: float, path: Path = HUD_STATE_FILE) -> None:
    save_state(opacity=opacity, path=path)


def load_visibility(path: Path = HUD_STATE_FILE) -> bool:
    data = _load_state(path)
    return bool(data.get("visible")) if data else False


def save_visibility(visible: bool, path: Path = HUD_STATE_FILE) -> None:
    save_state(visible=visible, path=path)


def _probe_snapshot() -> dict:
    return quota_state.probe_snapshot()


def _load_or_probe_snapshot(cache_only: bool = False,
                            force_refresh: bool = False) -> dict:
    return quota_state.load_or_probe_snapshot(
        cache_only=cache_only,
        force_refresh=force_refresh,
    )


class HudApp:
    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS,
                 cache_only: "bool | None" = None,
                 snapshot_loader: "Callable[[], dict] | None" = None,
                 refresh_loader: "Callable[[], dict] | None" = None):
        if tk is None:
            raise RuntimeError("tkinter is required for HUD mode")
        if (snapshot_loader is None) != (refresh_loader is None):
            raise ValueError("snapshot_loader and refresh_loader must be provided together")
        self.interval = interval
        self.mode = MODE_COMPACT
        self.snapshot: dict = {}
        self._fetch_lock = threading.Lock()
        self._press_root: "tuple[int, int] | None" = None
        self._press_pointer: "tuple[int, int] | None" = None
        self._last_position: "tuple[int, int] | None" = None
        self._dragged = False
        self._cache_only = (
            os.environ.get(HUD_CACHE_ONLY_ENV) == "1"
            if cache_only is None else bool(cache_only)
        )
        self._snapshot_loader = snapshot_loader
        self._refresh_loader = refresh_loader
        self._quota_state = (
            None
            if snapshot_loader is not None or refresh_loader is not None
            else quota_state.QuotaStateService(cache_only=self._cache_only)
        )
        self._started_at = time.time()
        self.opacity = load_opacity()
        self._menu_open = False
        self._close_position_saved = False

        self.root = tk.Tk()
        self.root.title("ai-fuelgauge HUD")
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._reassert_window_attributes()

        self.label = tk.Label(
            self.root,
            text="Loading...",
            font=("Consolas", 10),
            justify="left",
            anchor="w",
            padx=10,
            pady=8,
            bg="#101418",
            fg="#f2f5f8",
        )
        self.label.pack(fill="both", expand=True)

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="Refresh now", command=self.refresh_now)
        self.menu.add_command(label="Toggle details", command=self.toggle)

        self.opacity_menu = tk.Menu(self.menu, tearoff=False)
        self._opacity_preset_indices: dict[float, int] = {}
        for preset in OPACITY_PRESETS:
            self.opacity_menu.add_command(
                label=self._opacity_preset_label(preset),
                command=lambda value=preset: self.set_opacity(value),
            )
            self._opacity_preset_indices[preset] = self.opacity_menu.index("end")
        self.opacity_menu.add_separator()
        self.opacity_menu.add_command(
            label=self._opacity_default_label(),
            command=lambda: self.set_opacity(DEFAULT_OPACITY),
        )
        self._opacity_default_index = self.opacity_menu.index("end")
        self.menu.add_cascade(label=self._opacity_label(), menu=self.opacity_menu)
        self._opacity_menu_index = self.menu.index("end")

        self.menu.add_separator()
        self.menu.add_command(label="Quit HUD", command=self.quit)

        for widget in (self.root, self.label):
            widget.bind("<ButtonPress-1>", self._on_press)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._on_release)
            widget.bind("<Button-3>", self._show_menu)
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Button-4>", self._on_scroll_up)
            widget.bind("<Button-5>", self._on_scroll_down)

        self._place_initial()

    def _place_initial(self) -> None:
        self.root.update_idletasks()
        pos = load_position()
        if pos is None:
            min_x, min_y, width, height = self._desktop_bounds()
            x = max(min_x, min_x + width - 280)
            y = min_y + 80
        else:
            x, y = self._clamp_position(pos)
        self.root.geometry(f"+{x}+{y}")
        self.root.update_idletasks()
        self._last_position = (x, y)

    def _apply_opacity(self) -> None:
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass

    def _reassert_window_attributes(self) -> None:
        try:
            self.root.attributes("-alpha", self.opacity)
            if getattr(self, "_menu_open", False):
                return
            if not self.root.winfo_ismapped():
                return
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass

    def _desktop_bounds(self) -> "tuple[int, int, int, int]":
        try:
            min_x = int(self.root.winfo_vrootx())
            min_y = int(self.root.winfo_vrooty())
            width = int(self.root.winfo_vrootwidth())
            height = int(self.root.winfo_vrootheight())
            if width > 0 and height > 0:
                return min_x, min_y, width, height
        except (tk.TclError, AttributeError):
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_position(self, pos: "tuple[int, int]") -> "tuple[int, int]":
        min_x, min_y, width, height = self._desktop_bounds()
        return clamp_position_to_bounds(pos, min_x, min_y, width, height)

    def _save_current_position(self) -> None:
        # _last_position is the authoritative record of where the user wants
        # the HUD. It is set by _place_initial and _on_drag. winfo_x/winfo_y
        # are only a fallback because on Windows they can report (0, 0) for an
        # overrideredirect toplevel mid-teardown.
        pos = self._last_position
        if pos is None:
            try:
                self.root.update_idletasks()
                pos = (self.root.winfo_x(), self.root.winfo_y())
            except tk.TclError:
                return
        x, y = self._clamp_position(pos)
        self._last_position = (x, y)
        save_position(x, y)

    def _save_close_position_once(self) -> None:
        if self._close_position_saved:
            return
        self._close_position_saved = True
        self._save_current_position()

    def _on_destroy(self, event) -> None:
        if getattr(event, "widget", None) is self.root:
            self._save_close_position_once()

    def _on_press(self, event) -> str:
        self._press_root = (self.root.winfo_x(), self.root.winfo_y())
        self._press_pointer = (event.x_root, event.y_root)
        self._dragged = False
        return "break"

    def _on_drag(self, event) -> str:
        if self._press_root is None or self._press_pointer is None:
            return "break"
        current = (event.x_root, event.y_root)
        if is_drag(self._press_pointer, current):
            self._dragged = True
        if not self._dragged:
            return "break"
        dx = current[0] - self._press_pointer[0]
        dy = current[1] - self._press_pointer[1]
        x = self._press_root[0] + dx
        y = self._press_root[1] + dy
        self._last_position = (x, y)
        self.root.geometry(f"+{x}+{y}")
        return "break"

    def _on_release(self, event) -> str:
        if self._press_pointer is None:
            return "break"
        if self._dragged:
            self._save_current_position()
        else:
            self.toggle()
        self._press_root = None
        self._press_pointer = None
        self._dragged = False
        return "break"

    def _show_menu(self, event) -> str:
        self._refresh_opacity_menu()
        self._menu_open = True
        try:
            self.root.attributes("-topmost", False)
            self.menu.tk_popup(event.x_root, event.y_root)
            self.root.after(150, self._watch_menu_close)
        except tk.TclError:
            self._menu_open = False
            self._reassert_window_attributes()
        return "break"

    def _menu_is_mapped(self) -> bool:
        try:
            return bool(self.menu.winfo_ismapped() or self.opacity_menu.winfo_ismapped())
        except tk.TclError:
            return False

    def _watch_menu_close(self) -> None:
        if self._menu_is_mapped():
            self.root.after(150, self._watch_menu_close)
            return
        self._menu_open = False
        self._reassert_window_attributes()

    def _opacity_pct(self, opacity: float) -> int:
        return int(round(clamp_opacity(opacity) * 100))

    def _opacity_label(self) -> str:
        return f"Opacity {self._opacity_pct(self.opacity)}%"

    def _opacity_preset_label(self, opacity: float) -> str:
        marker = "*" if abs(clamp_opacity(opacity) - self.opacity) < 0.001 else " "
        return f"{marker} {self._opacity_pct(opacity)}%"

    def _opacity_default_label(self) -> str:
        marker = "*" if abs(DEFAULT_OPACITY - self.opacity) < 0.001 else " "
        return f"{marker} Default {self._opacity_pct(DEFAULT_OPACITY)}%"

    def _refresh_opacity_menu(self) -> None:
        try:
            self.menu.entryconfigure(self._opacity_menu_index, label=self._opacity_label())
            for preset, index in self._opacity_preset_indices.items():
                self.opacity_menu.entryconfigure(index, label=self._opacity_preset_label(preset))
            self.opacity_menu.entryconfigure(
                self._opacity_default_index,
                label=self._opacity_default_label(),
            )
        except tk.TclError:
            pass

    def _on_mousewheel(self, event) -> str:
        steps = 1 if event.delta > 0 else -1
        self.adjust_opacity(steps)
        return "break"

    def _on_scroll_up(self, event) -> str:
        self.adjust_opacity(1)
        return "break"

    def _on_scroll_down(self, event) -> str:
        self.adjust_opacity(-1)
        return "break"

    def toggle(self) -> None:
        self.mode = toggle_mode(self.mode)
        self._render()

    def set_opacity(self, opacity: float) -> None:
        self.opacity = clamp_opacity(opacity)
        self._reassert_window_attributes()
        self._refresh_opacity_menu()
        save_opacity(self.opacity)

    def adjust_opacity(self, steps: int) -> None:
        self.set_opacity(adjust_opacity_value(self.opacity, steps))

    def refresh_now(self) -> None:
        self._start_fetch(force_refresh=True)

    def _render(self) -> None:
        rows = format_hud_rows(self.snapshot, self.mode) if self.snapshot else ["Loading..."]
        self.label.configure(text="\n".join(rows))

    def _apply_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self._render()
        self._reassert_window_attributes()

    def _start_fetch(self, force_refresh: bool = False) -> None:
        if not self._fetch_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                loader = (
                    self._refresh_loader
                    if force_refresh and self._refresh_loader is not None
                    else self._snapshot_loader
                )
                snapshot = (
                    loader()
                    if loader is not None
                    else self._quota_state.refresh_snapshot(force_refresh=force_refresh)
                )
                self.root.after(0, lambda: self._apply_snapshot(snapshot))
            except Exception as e:
                try:
                    if sys.stderr is not None:
                        sys.stderr.write(f"HUD refresh failed: {e}\n")
                except Exception:
                    pass
            finally:
                self._fetch_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _start_view_sync(self) -> None:
        if not self._fetch_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                if self._snapshot_loader is not None:
                    snapshot = self._snapshot_loader()
                else:
                    snapshot = (
                        quota_state.cached_snapshot()
                        or self._quota_state.current_snapshot()
                    )
                self.root.after(0, lambda: self._apply_snapshot(snapshot))
            except Exception as e:
                try:
                    if sys.stderr is not None:
                        sys.stderr.write(f"HUD sync failed: {e}\n")
                except Exception:
                    pass
            finally:
                self._fetch_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_poll(self) -> None:
        self._start_fetch()
        self.root.after(self.interval * 1000, self._schedule_poll)

    def _schedule_view_sync(self) -> None:
        self._start_view_sync()
        self.root.after(CACHE_SYNC_INTERVAL_SECONDS * 1000, self._schedule_view_sync)

    def _schedule_command_poll(self) -> None:
        if consume_hud_close_request(self._started_at):
            self.quit()
            return
        self.root.after(COMMAND_POLL_INTERVAL_MS, self._schedule_command_poll)

    def _schedule_window_reassert(self) -> None:
        self._reassert_window_attributes()
        self.root.after(WINDOW_REASSERT_INTERVAL_MS, self._schedule_window_reassert)

    def quit(self) -> None:
        self._save_close_position_once()
        self.root.destroy()

    def run(self) -> None:
        self._schedule_poll()
        self._schedule_view_sync()
        self._schedule_command_poll()
        self._schedule_window_reassert()
        self.root.mainloop()


def run_hud(interval: int = DEFAULT_INTERVAL_SECONDS,
            cache_only: "bool | None" = None) -> int:
    if tk is None:
        sys.stderr.write("HUD mode requires tkinter.\n")
        return 2
    lock_fd = acquire_hud_lock()
    if lock_fd is None:
        return 0
    try:
        clear_hud_close_request()
        HudApp(interval=interval, cache_only=cache_only).run()
    finally:
        release_hud_lock(lock_fd)
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ai-fuelgauge floating HUD")
    ap.add_argument("--interval", type=afg.interval_seconds, default=DEFAULT_INTERVAL_SECONDS,
                    help="poll interval in seconds (default: 300, minimum: 1)")
    args = ap.parse_args()
    sys.exit(run_hud(interval=args.interval))
