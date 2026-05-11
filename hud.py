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


HUD_STATE_FILE = Path.home() / ".cache" / "ai-fuelgauge-hud.json"
DEFAULT_INTERVAL_SECONDS = 300
DRAG_THRESHOLD_PX = 4
HUD_CACHE_ONLY_ENV = "AI_FUELGAUGE_HUD_CACHE_ONLY"
DEFAULT_OPACITY = 0.88
MIN_OPACITY = 0.25
MAX_OPACITY = 1.0
OPACITY_STEP = 0.10
OPACITY_PRESETS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)

MODE_COMPACT = "compact"
MODE_EXPANDED = "expanded"


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
    rows = [
        _format_row("Codex", "5h", codex, "primary"),
        _format_row("Claude", "5h", claude, "primary"),
    ]
    if _claude_should_use_stale(claude):
        stale_primary = _claude_stale_window("primary")
        rows[1] = (
            _format_window_row("Claude", "5h", stale_primary, stale=True)
            if stale_primary else _format_row("Claude", "5h", None, "primary")
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
    x, y = pos
    max_x = max(0, int(screen_width) - margin)
    max_y = max(0, int(screen_height) - margin)
    return min(max(0, int(x)), max_x), min(max(0, int(y)), max_y)


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
               opacity: "float | None" = None, path: Path = HUD_STATE_FILE) -> None:
    try:
        data = _load_state(path)
        if x is not None and y is not None:
            data["x"] = int(x)
            data["y"] = int(y)
        if opacity is not None:
            data["opacity"] = clamp_opacity(opacity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def save_position(x: int, y: int, path: Path = HUD_STATE_FILE) -> None:
    save_state(x=x, y=y, path=path)


def save_opacity(opacity: float, path: Path = HUD_STATE_FILE) -> None:
    save_state(opacity=opacity, path=path)


def _probe_snapshot() -> dict:
    codex_fresh = afg.probe_codex_fresh()
    codex_stale = afg.read_codex_quota()
    if (
        codex_fresh
        and not codex_fresh.get("error")
        and codex_fresh.get("primary", {}).get("used_percent") is not None
    ):
        codex = codex_fresh
        codex["_source"] = "fresh-api"
    else:
        codex = codex_stale or {}
        if isinstance(codex, dict):
            codex["_source"] = "sqlite-snapshot"
            codex["_fresh_probe_error"] = codex_fresh.get("error") if codex_fresh else "no-data"
    claude = afg.probe_claude_quota()
    afg._save_last_good_claude(claude)
    data = {"codex": codex, "claude": claude, "_from_cache": False}
    afg.save_cache(data)
    return data


def _load_or_probe_snapshot(cache_only: bool = False,
                            force_refresh: bool = False) -> dict:
    if not force_refresh:
        cache = afg.load_cache(afg.CACHE_TTL_SECONDS)
        if isinstance(cache, dict):
            cache["_from_cache"] = True
            return cache
    if cache_only:
        return {"codex": {}, "claude": {}, "_from_cache": False}
    return _probe_snapshot()


class HudApp:
    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS,
                 cache_only: "bool | None" = None,
                 snapshot_loader: "Callable[[], dict] | None" = None,
                 refresh_loader: "Callable[[], dict] | None" = None):
        if tk is None:
            raise RuntimeError("tkinter is required for HUD mode")
        self.interval = interval
        self.mode = MODE_COMPACT
        self.snapshot: dict = {}
        self._fetch_lock = threading.Lock()
        self._press_root: "tuple[int, int] | None" = None
        self._press_pointer: "tuple[int, int] | None" = None
        self._dragged = False
        self._cache_only = (
            os.environ.get(HUD_CACHE_ONLY_ENV) == "1"
            if cache_only is None else bool(cache_only)
        )
        self._snapshot_loader = snapshot_loader
        self._refresh_loader = refresh_loader
        self.opacity = load_opacity()

        self.root = tk.Tk()
        self.root.title("ai-fuelgauge HUD")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._apply_opacity()

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
        pos = load_position()
        if pos is None:
            self.root.update_idletasks()
            x = max(0, self.root.winfo_screenwidth() - 280)
            y = 80
        else:
            x, y = clamp_position(
                pos,
                self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(),
            )
        self.root.geometry(f"+{x}+{y}")

    def _apply_opacity(self) -> None:
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass

    def _save_current_position(self) -> None:
        x, y = clamp_position(
            (self.root.winfo_x(), self.root.winfo_y()),
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )
        save_position(x, y)

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
        self.root.geometry(f"+{self._press_root[0] + dx}+{self._press_root[1] + dy}")
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
        self.menu.tk_popup(event.x_root, event.y_root)
        return "break"

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
        self._apply_opacity()
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
                    else _load_or_probe_snapshot(
                        cache_only=self._cache_only,
                        force_refresh=force_refresh,
                    )
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

    def _schedule_poll(self) -> None:
        self._start_fetch()
        self.root.after(self.interval * 1000, self._schedule_poll)

    def quit(self) -> None:
        self._save_current_position()
        self.root.destroy()

    def run(self) -> None:
        self._schedule_poll()
        self.root.mainloop()


def run_hud(interval: int = DEFAULT_INTERVAL_SECONDS,
            cache_only: "bool | None" = None) -> int:
    if tk is None:
        sys.stderr.write("HUD mode requires tkinter.\n")
        return 2
    HudApp(interval=interval, cache_only=cache_only).run()
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ai-fuelgauge floating HUD")
    ap.add_argument("--interval", type=afg.interval_seconds, default=DEFAULT_INTERVAL_SECONDS,
                    help="poll interval in seconds (default: 300, minimum: 1)")
    args = ap.parse_args()
    sys.exit(run_hud(interval=args.interval))
