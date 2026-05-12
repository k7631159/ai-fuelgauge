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

import math
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
import quota_state  # noqa: E402
from windows_detach import reexec_detached_on_windows as _reexec_detached_on_windows  # noqa: E402


DEFAULT_INTERVAL_SECONDS = 300
THRESHOLD_PRIMARY_PCT = 80   # 5h window
THRESHOLD_SECONDARY_PCT = 90  # weekly window
HYSTERESIS_PCT = 10           # must drop this far below threshold before re-notifying

# Cross-platform single-instance guard. Advisory lock on this file is held for
# the lifetime of the process; the OS releases it on exit (including crash),
# so there is no stale-pid cleanup to manage.
_LOCK_FILE = Path.home() / ".cache" / "ai-fuelgauge-tray.lock"


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
    return quota_state.probe_snapshot()


def _max_pct(snap: dict) -> float:
    m = 0.0
    for key in ("codex", "claude"):
        d = snap.get(key) or {}
        if not isinstance(d, dict):
            continue
        # Claude proactive-expiry / reactive env-token 401: defer to last-
        # known-good so the icon dot keeps reflecting the bars the user
        # sees in the menu (instead of silently dropping to 0 and going
        # green when Claude is at 95%). Routed through the same
        # classifier as the menu/tooltip/hint paths so all four agree on
        # which states qualify as stale-displayable.
        # Only stale bars whose original window hasn't rolled over count.
        if key == "claude":
            err_info = _classify_probe_error(d, "Claude")
        else:
            err_info = None
        if err_info is not None and err_info[0] in ("expired", "envtok"):
            stale = afg._load_last_good_claude()
            if stale:
                for w in ("primary", "secondary"):
                    bar_data = stale.get(w)
                    if afg._stale_bar_status(bar_data) != "valid":
                        continue
                    p = bar_data.get("used_percent") if isinstance(bar_data, dict) else None
                    try:
                        v = float(p) if p is not None else None
                    except (TypeError, ValueError):
                        v = None
                    if v is None or math.isnan(v) or math.isinf(v):
                        continue
                    m = max(m, v)
            continue
        for w in ("primary", "secondary"):
            p = (d.get(w) or {}).get("used_percent")
            if p is None or isinstance(p, bool):
                continue
            try:
                v = float(p)
            except (TypeError, ValueError):
                continue
            # NaN / inf propagate through max() unpredictably and would
            # feed into _make_icon's threshold comparison; skip them.
            if math.isnan(v) or math.isinf(v):
                continue
            m = max(m, v)
    return m


def _classify_probe_error(d: "dict | None", provider: str) -> "tuple[str, str] | None":
    r"""Map a probe result to (short_label, menu_text) when the result is a
    known error state, else return None (let the caller treat as success).

    - short_label — tight string shown in the tray title (<= ~8 chars), e.g.
      `429`, `auth`, `login`, `offline`, `no-cli`.
    - menu_text   — fuller human explanation shown in the right-click menu,
      e.g. `Claude: auth expired — run \`claude\``.

    Having labels distinct between error types addresses the UX gap where
    every failure rendered identically as `Claude ?/?`, which users
    (reasonably) read as "the tool is broken" rather than "server said 429".
    """
    if not isinstance(d, dict) or not d:
        return "?", f"{provider}: no data"
    err = d.get("error") or ""
    status = d.get("status")

    if provider == "Claude":
        # Proactive auth-state errors first — they carry actionable user
        # context without needing to look at HTTP status (no probe was made).
        if err == "env-token-expired":
            return "envtok", "Claude: $CLAUDE_CODE_OAUTH_TOKEN expired — replace it"
        if err == "auth-expired-no-refresh":
            return "expired", "Claude: token expired — run `claude` to re-login"
        # Check status-based signals next — a concrete HTTP code is more
        # specific than a generic "probe-failed" string, so it wins when
        # both are present.
        if status == 429:
            return "429", "Claude: rate limited, retrying later"
        if status == 401:
            # Refined 401 messaging: distinguish env-token (can't refresh)
            # from refresh-attempted-but-failed (run `claude` interactively).
            if d.get("_env_token_mode"):
                return "envtok", "Claude: $CLAUDE_CODE_OAUTH_TOKEN expired — replace it"
            if d.get("_refresh_attempted"):
                return "auth", "Claude: auto-refresh failed — run `claude`"
            return "auth", "Claude: auth expired — run `claude`"
        if isinstance(status, int) and status >= 400:
            return str(status), f"Claude: HTTP {status}"
        if err == "no-token-found":
            return "login", "Claude: not logged in — run `claude`"
        if err.startswith("probe-failed"):
            return "offline", "Claude: offline / probe failed"
    elif provider == "Codex":
        if err == "codex-not-in-path":
            return "no-cli", "Codex: codex CLI not found on PATH"
        if err.startswith("spawn-failed"):
            return "spawn", "Codex: codex CLI spawn failed"
        if err == "no-response-from-app-server":
            return "no-resp", "Codex: app-server not responding"
        if err.startswith("jsonrpc-error"):
            return "rpc-err", "Codex: JSON-RPC error from app-server"
        if err == "empty-rateLimits":
            return "empty", "Codex: empty rateLimits response"
        if err.startswith("codex sqlite"):
            return "db-err", "Codex: sqlite snapshot error"

    # Unclassified error with an `error` key — surface it verbatim (truncated).
    if err:
        return "err", f"{provider}: {err[:40]}"

    return None  # success — let caller render utilization numbers


def _summary_line(snap: dict) -> str:
    """Build the multi-line tray tooltip.

    Two lines so Codex and Claude are vertically aligned and individually
    scannable. NOTIFYICONDATA.szTip on Windows accepts \\r\\n; on other
    platforms pystray may collapse the newline to a space, which is an
    acceptable fallback (still readable).
    """
    lines = []
    for label, key in (("Codex", "codex"), ("Claude", "claude")):
        d = snap.get(key) or {}
        lines.append(_provider_summary_line(label, d))
    return "\r\n".join(lines)


def _provider_summary_line(label: str, d: dict) -> str:
    """Format one provider's tooltip line with healthy/stale/error variants."""
    err_info = _classify_probe_error(d, label)
    if err_info is not None:
        # Stale-with-bars: when the proactive-expiry error has a recent
        # last-known-good behind it, prefer the stale summary so the
        # tooltip still carries useful numbers instead of just "expired".
        if label == "Claude" and err_info[0] in ("expired", "envtok"):
            stale = _claude_stale_tooltip()
            if stale:
                return stale
            return f"{label:6}  expired"
        return f"{label:6}  {err_info[0]}"
    p = _pct_or_none(d, "primary")
    s = _pct_or_none(d, "secondary")
    p_str = f"{p:.0f}%" if p is not None else "?"
    s_str = f"{s:.0f}%" if s is not None else "?"
    return f"{label:6}  5h: {p_str}  week: {s_str}"


def _claude_stale_tooltip() -> "str | None":
    """Format the Claude tooltip line from last-known-good when usable.

    Returns None when there's nothing displayable (cache miss, expired
    cache, both bars rolled over). Skips rolled-over bars from the line
    and notes the rollover in the suffix so users know why a bar is
    missing rather than thinking the data is incomplete.
    """
    last_good = afg._load_last_good_claude()
    if not last_good:
        return None
    primary = last_good.get("primary")
    secondary = last_good.get("secondary")
    p_status = afg._stale_bar_status(primary)
    s_status = afg._stale_bar_status(secondary)
    age_seconds = int(time.time()) - int(last_good["_probed_at"])
    age_text = afg._format_stale_age(age_seconds)

    parts = []
    if p_status == "valid":
        try:
            pct = float(primary["used_percent"])
            if not (math.isnan(pct) or math.isinf(pct)):
                parts.append(f"5h: {pct:.0f}%")
        except (TypeError, ValueError, KeyError):
            pass
    if s_status == "valid":
        try:
            pct = float(secondary["used_percent"])
            if not (math.isnan(pct) or math.isinf(pct)):
                parts.append(f"week: {pct:.0f}%")
        except (TypeError, ValueError, KeyError):
            pass

    if not parts:
        return None

    rolled_note = ""
    if p_status == "rolled_over" and s_status == "valid":
        rolled_note = "; 5h window reset"
    elif s_status == "rolled_over" and p_status == "valid":
        rolled_note = "; week window reset"

    return f"Claude   {'  '.join(parts)}  ({age_text} stale, expired{rolled_note})"


def _claude_stale_menu_label(window_key: str, window_short: str) -> str:
    """Format one Claude menu row from last-known-good state.

    Returns "Claude {short}: 29% (3h stale)" when the cached bar is still
    in its window, "Claude {short}: -- (window reset)" when rolled over,
    and "Claude {short}: --" when nothing usable is available. The double
    dash is intentional and consistent across every fallback so menu
    width stays stable across scenarios.
    """
    last_good = afg._load_last_good_claude()
    if not last_good:
        return f"Claude {window_short}: --"
    bar_data = last_good.get(window_key)
    status = afg._stale_bar_status(bar_data)
    if status == "rolled_over":
        return f"Claude {window_short}: -- (window reset)"
    if status != "valid" or not isinstance(bar_data, dict):
        return f"Claude {window_short}: --"
    pct = bar_data.get("used_percent")
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None
    if pct_f is None or math.isnan(pct_f) or math.isinf(pct_f):
        return f"Claude {window_short}: --"
    age = int(time.time()) - int(last_good["_probed_at"])
    age_text = afg._format_stale_age(age)
    return f"Claude {window_short}: {pct_f:.0f}% ({age_text} stale)"


def _pct_or_none(d, window) -> "float | None":
    if not isinstance(d, dict):
        return None
    w = d.get(window) or {}
    p = w.get("used_percent")
    if p is None or isinstance(p, bool):
        # bool is a subclass of int; without this guard True/False from an
        # evolved endpoint would render as 1% / 0%.
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    # NaN / inf would feed into menu formatting as "nan%" / "inf%" and into
    # _max_pct's threshold math; surface as "no data" instead.
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _window_max_provider(snap: dict, window: str) -> "tuple[float, str]":
    max_pct = 0.0
    max_labels: list[str] = []
    for label, key in (("Codex", "codex"), ("Claude", "claude")):
        d = snap.get(key) or {}
        pct = _pct_or_none(d, window)
        if pct is None:
            continue
        if pct > max_pct:
            max_pct = pct
            max_labels = [label]
        elif pct == max_pct and pct > 0:
            max_labels.append(label)

    if len(max_labels) == 1:
        return max_pct, max_labels[0]
    if len(max_labels) > 1:
        return max_pct, " + ".join(max_labels)
    return max_pct, "AI"


def _reset_str(d, window) -> "str | None":
    """Format the time-to-reset for one window as a human-readable
    countdown ('3h03m', '15h35m'), live-recomputed from `reset_at`
    when available so the displayed countdown stays accurate even
    minutes after the probe.

    Falls back to the at-probe-time `reset_in_seconds` when no
    `reset_at` was captured. Returns None when neither is usable —
    caller should omit the reset annotation rather than show '-'.
    """
    if not isinstance(d, dict):
        return None
    w = d.get(window) or {}
    if not isinstance(w, dict):
        return None
    reset_at = w.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        delta = int(reset_at) - int(time.time())
        if delta < 0:
            delta = 0
        return afg.fmt_duration(delta)
    reset_in = w.get("reset_in_seconds")
    if isinstance(reset_in, (int, float)) and not isinstance(reset_in, bool):
        delta = max(0, int(reset_in))
        return afg.fmt_duration(delta)
    return None


class TrayApp:
    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self.quota_state = quota_state.QuotaStateService()
        self._notified_primary = False
        self._notified_secondary = False
        self.icon: "pystray.Icon | None" = None
        self._stop = threading.Event()
        # Non-blocking guard: held while a fetch is in flight so repeated menu
        # clicks (or a poller tick racing a manual refresh) skip instead of
        # stacking concurrent probes against the Codex / Claude APIs.
        self._fetch_lock = threading.Lock()
        self._hud_app = None
        self._hud_thread = None
        self._hud_lock = threading.Lock()
        self._hud_instance_lock_fd: "int | None" = None
        self._hud_stop_requested = False

    # --- menu item text getters (callables so they re-evaluate on menu open) ---
    # When the probe is in a known error state (401 / 429 / offline / login /
    # no-cli / ...) the 5h row carries the full explanation; the week row
    # falls back to its normal "?" so the menu doesn't duplicate the same
    # error text on two adjacent rows.
    def _codex_5h(self, _item):
        d = self._snapshot_view().get("codex") or {}
        err_info = _classify_probe_error(d, "Codex")
        if err_info is not None:
            return err_info[1]
        p = _pct_or_none(d, "primary")
        if p is None:
            return "Codex 5h: ?"
        reset = _reset_str(d, "primary")
        return f"Codex 5h: {p:.0f}% (resets {reset})" if reset else f"Codex 5h: {p:.0f}%"

    def _codex_week(self, _item):
        d = self._snapshot_view().get("codex") or {}
        if _classify_probe_error(d, "Codex") is not None:
            return "Codex week: -"
        p = _pct_or_none(d, "secondary")
        if p is None:
            return "Codex week: ?"
        reset = _reset_str(d, "secondary")
        return f"Codex week: {p:.0f}% (resets {reset})" if reset else f"Codex week: {p:.0f}%"

    def _claude_5h(self, _item):
        d = self._snapshot_view().get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None:
            # Proactive-expiry: render stale-or-omitted bar so the menu
            # 5h/week rows stay populated instead of collapsing into the
            # generic 'run claude' explanation.
            if err_info[0] in ("expired", "envtok"):
                return _claude_stale_menu_label("primary", "5h")
            return err_info[1]
        p = _pct_or_none(d, "primary")
        if p is None:
            return "Claude 5h: ?"
        reset = _reset_str(d, "primary")
        return f"Claude 5h: {p:.0f}% (resets {reset})" if reset else f"Claude 5h: {p:.0f}%"

    def _claude_week(self, _item):
        d = self._snapshot_view().get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None:
            if err_info[0] in ("expired", "envtok"):
                return _claude_stale_menu_label("secondary", "week")
            return "Claude week: --"
        p = _pct_or_none(d, "secondary")
        if p is None:
            return "Claude week: ?"
        reset = _reset_str(d, "secondary")
        return f"Claude week: {p:.0f}% (resets {reset})" if reset else f"Claude week: {p:.0f}%"

    # --- expired-token hint row (visible whenever stale-bar routing fires) ---
    # Routed via the classifier so reactive paths line up with proactive ones:
    # a 401 + `_env_token_mode: True` from the network round-trip surfaces
    # as `envtok` too, and must keep the env-var replacement hint or the
    # user sees stale numbers with no recovery instruction.
    def _claude_hint_visible(self, _item) -> bool:
        d = self._snapshot_view().get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is None:
            return False
        return err_info[0] in ("expired", "envtok")

    def _claude_hint_text(self, _item) -> str:
        d = self._snapshot_view().get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None and err_info[0] == "envtok":
            return "Claude: replace $CLAUDE_CODE_OAUTH_TOKEN"
        return "Claude: token expired — run `claude`"

    # --- actions ---
    def _refresh_now(self, _icon=None, _item=None):
        threading.Thread(target=self._do_fetch, daemon=True).start()

    def _hud_running(self) -> bool:
        return self._hud_thread is not None and self._hud_thread.is_alive()

    def _external_hud_running(self) -> bool:
        if self._hud_running():
            return False
        try:
            from hud import acquire_hud_lock, release_hud_lock
            lock_fd = acquire_hud_lock()
        except Exception:
            return False
        if lock_fd is None:
            return True
        try:
            release_hud_lock(lock_fd)
        except Exception:
            pass
        return False

    def _hud_label(self, _item) -> str:
        if self._hud_running():
            return "Hide HUD"
        if self._external_hud_running():
            return "Close HUD"
        return "Show HUD"

    def _hud_action_enabled(self, _item) -> bool:
        return True

    def _toggle_hud(self, _icon=None, _item=None):
        if self._hud_running():
            self._stop_hud()
            self._save_hud_visibility(False)
        elif self._external_hud_running():
            self._request_external_hud_close()
            self._save_hud_visibility(False)
            self._watch_external_hud_close()
        else:
            self._start_hud()
            self._save_hud_visibility(True)
        self._update_menu_safely()

    def _request_external_hud_close(self) -> None:
        try:
            from hud import request_hud_close
            request_hud_close()
        except Exception:
            pass

    def _save_hud_visibility(self, visible: bool) -> None:
        try:
            from hud import save_visibility
            save_visibility(visible)
        except Exception:
            pass

    def _load_hud_visibility(self) -> bool:
        try:
            from hud import load_visibility
            return load_visibility()
        except Exception:
            return False

    def _restore_hud_visibility(self) -> None:
        if self._load_hud_visibility():
            if self._external_hud_running():
                self._request_external_hud_close()
                threading.Thread(
                    target=self._start_hud_after_external_close,
                    daemon=True,
                ).start()
            else:
                self._start_hud()
        self._update_menu_safely()

    def _start_hud_after_external_close(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._external_hud_running():
                self._start_hud()
                self._update_menu_safely()
                return
            time.sleep(0.2)
        self._update_menu_safely()

    def _watch_external_hud_close(self, timeout: float = 5.0) -> None:
        threading.Thread(
            target=self._wait_for_external_hud_close,
            args=(timeout,),
            daemon=True,
        ).start()

    def _wait_for_external_hud_close(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._external_hud_running():
                self._update_menu_safely()
                return
            time.sleep(0.2)
        self._update_menu_safely()

    def _update_menu_safely(self) -> None:
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _start_hud(self) -> None:
        with self._hud_lock:
            if self._hud_running():
                return
            from hud import acquire_hud_lock
            lock_fd = acquire_hud_lock()
            if lock_fd is None:
                return
            self._hud_stop_requested = False
            self._hud_instance_lock_fd = lock_fd
            thread = threading.Thread(target=self._run_hud, daemon=True)
            self._hud_thread = thread
            thread.start()

    def _run_hud(self) -> None:
        closed_normally = False
        try:
            from hud import HudApp
            app = HudApp(
                interval=self.interval,
                cache_only=True,
                snapshot_loader=self._snapshot_for_hud,
                refresh_loader=self._refresh_snapshot_for_hud,
            )
            with self._hud_lock:
                self._hud_app = app
                stop_requested = self._hud_stop_requested
            if stop_requested:
                app.root.after(0, app.quit)
            app.run()
            closed_normally = True
        except Exception as e:
            try:
                if sys.stderr is not None:
                    sys.stderr.write(f"HUD failed: {e}\n")
            except Exception:
                pass
        finally:
            with self._hud_lock:
                lock_fd = self._hud_instance_lock_fd
                self._hud_app = None
                self._hud_thread = None
                self._hud_instance_lock_fd = None
                self._hud_stop_requested = False
            if lock_fd is not None:
                try:
                    from hud import release_hud_lock
                    release_hud_lock(lock_fd)
                except Exception:
                    pass
            if closed_normally and not self._stop.is_set():
                self._save_hud_visibility(False)
            self._update_menu_safely()

    def _stop_hud(self) -> None:
        with self._hud_lock:
            app = self._hud_app
            if app is None:
                self._hud_stop_requested = self._hud_thread is not None
        if app is None:
            return
        try:
            app.root.after(0, app.quit)
        except Exception:
            pass

    def _snapshot_for_hud(self) -> dict:
        return self._snapshot_view()

    def _snapshot_view(self) -> dict:
        return self.quota_state.current_snapshot()

    def _refresh_snapshot_for_hud(self) -> dict:
        self._do_fetch(blocking=True)
        return self._snapshot_for_hud()

    def _quit(self, _icon=None, _item=None):
        self._stop.set()
        self._stop_hud()
        if self.icon:
            self.icon.stop()

    # --- core ---
    def _do_fetch(self, blocking: bool = False) -> bool:
        if not self._fetch_lock.acquire(blocking=blocking):
            # False is only reachable in the non-blocking menu/poller path.
            # HUD-triggered refresh uses blocking=True inside a worker thread.
            return False  # another fetch is in flight — skip to avoid overlap
        # The whole fetch + post-processing runs under one try/except. The
        # previous `try/else` placed `_apply_to_icon()` in the `else` branch
        # OUTSIDE the try — any Pillow/pystray backend error raised from
        # there would propagate out of the poller/refresh thread and, under
        # the detached `pythonw.exe` tray, vanish into /dev/null. Result:
        # tray shows stale data forever with no signal. Now all steps are
        # caught so the fetch lock is always released cleanly AND the thread
        # survives to try again on the next poll.
        try:
            snap = self.quota_state.refresh_snapshot(force_refresh=True)
            self._check_thresholds(snap)
            self._apply_to_icon()
        except Exception as e:
            # Wrap the stderr write too: under `pythonw.exe` sys.stderr can
            # be None (no attached console), and calling .write on None would
            # itself raise, propagate out of the thread, and kill the poller —
            # defeating the whole point of the except.
            try:
                if sys.stderr is not None:
                    sys.stderr.write(f"fetch failed: {e}\n")
            except Exception:
                pass
        finally:
            self._fetch_lock.release()
        return True

    def _check_thresholds(self, snap: dict) -> None:
        # Threshold-crossing notifications intentionally read only the live
        # snapshot, not last-known-good stale bars. Re-firing toasts on
        # stale data would notify based on the user's yesterday-state every
        # time the tray polls, which is alarm fatigue without new info —
        # the expired-state hint already surfaces in the icon and menu.
        max_p, primary_provider = _window_max_provider(snap, "primary")
        max_s, secondary_provider = _window_max_provider(snap, "secondary")

        if max_p >= THRESHOLD_PRIMARY_PCT and not self._notified_primary:
            _notify(
                f"{primary_provider} quota warning",
                f"5-hour window at {max_p:.0f}%",
            )
            self._notified_primary = True
        elif max_p < THRESHOLD_PRIMARY_PCT - HYSTERESIS_PCT:
            self._notified_primary = False

        if max_s >= THRESHOLD_SECONDARY_PCT and not self._notified_secondary:
            _notify(
                f"{secondary_provider} quota warning",
                f"Weekly window at {max_s:.0f}%",
            )
            self._notified_secondary = True
        elif max_s < THRESHOLD_SECONDARY_PCT - HYSTERESIS_PCT:
            self._notified_secondary = False

    def _apply_to_icon(self):
        if not self.icon:
            return
        snap = self._snapshot_view()
        m = _max_pct(snap)
        self.icon.icon = _make_icon(m)
        self.icon.title = _summary_line(snap)
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
            # Action hint row appears below the bars so the user reads the
            # retained values first, then the recovery action — error-first
            # ordering would make stale-with-bars look less actionable than
            # it is. Hidden in the healthy/non-expiry paths.
            pystray.MenuItem(
                self._claude_hint_text, None,
                enabled=False, visible=self._claude_hint_visible,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", self._refresh_now),
            pystray.MenuItem(
                self._hud_label,
                self._toggle_hud,
                enabled=self._hud_action_enabled,
            ),
            pystray.MenuItem("Quit", self._quit),
        )

    def run(self):
        # Initial synchronous fetch so the first icon is meaningful.
        self._do_fetch()
        self.icon = pystray.Icon(
            "ai-fuelgauge",
            icon=_make_icon(_max_pct(self._snapshot_view())),
            title=_summary_line(self._snapshot_view()),
            menu=self._menu(),
        )
        self._restore_hud_visibility()
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
