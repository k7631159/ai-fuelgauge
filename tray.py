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
    # Mirror the CLI: each successful probe updates the stale-bar fallback
    # used the next time the token has expired. Only writes on success;
    # no-ops on error / 4xx / no parsed windows.
    afg._save_last_good_claude(claude)
    return {"codex": codex, "claude": claude}


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
        # both are present (per Codex pre-commit review).
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
    # When the probe is in a known error state (401 / 429 / offline / login /
    # no-cli / ...) the 5h row carries the full explanation; the week row
    # falls back to its normal "?" so the menu doesn't duplicate the same
    # error text on two adjacent rows.
    def _codex_5h(self, _item):
        d = self.snapshot.get("codex") or {}
        err_info = _classify_probe_error(d, "Codex")
        if err_info is not None:
            return err_info[1]
        p = _pct_or_none(d, "primary")
        return f"Codex 5h: {p:.0f}%" if p is not None else "Codex 5h: ?"

    def _codex_week(self, _item):
        d = self.snapshot.get("codex") or {}
        if _classify_probe_error(d, "Codex") is not None:
            return "Codex week: -"
        p = _pct_or_none(d, "secondary")
        return f"Codex week: {p:.0f}%" if p is not None else "Codex week: ?"

    def _claude_5h(self, _item):
        d = self.snapshot.get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None:
            # Proactive-expiry: render stale-or-omitted bar so the menu
            # 5h/week rows stay populated instead of collapsing into the
            # generic 'run claude' explanation.
            if err_info[0] in ("expired", "envtok"):
                return _claude_stale_menu_label("primary", "5h")
            return err_info[1]
        p = _pct_or_none(d, "primary")
        return f"Claude 5h: {p:.0f}%" if p is not None else "Claude 5h: ?"

    def _claude_week(self, _item):
        d = self.snapshot.get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None:
            if err_info[0] in ("expired", "envtok"):
                return _claude_stale_menu_label("secondary", "week")
            return "Claude week: --"
        p = _pct_or_none(d, "secondary")
        return f"Claude week: {p:.0f}%" if p is not None else "Claude week: ?"

    # --- expired-token hint row (visible whenever stale-bar routing fires) ---
    # Routed via the classifier so reactive paths line up with proactive ones:
    # a 401 + `_env_token_mode: True` from the network round-trip surfaces
    # as `envtok` too, and must keep the env-var replacement hint or the
    # user sees stale numbers with no recovery instruction.
    def _claude_hint_visible(self, _item) -> bool:
        d = self.snapshot.get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is None:
            return False
        return err_info[0] in ("expired", "envtok")

    def _claude_hint_text(self, _item) -> str:
        d = self.snapshot.get("claude") or {}
        err_info = _classify_probe_error(d, "Claude")
        if err_info is not None and err_info[0] == "envtok":
            return "Claude: replace $CLAUDE_CODE_OAUTH_TOKEN"
        return "Claude: token expired — run `claude`"

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
        # The whole fetch + post-processing runs under one try/except. The
        # previous `try/else` placed `_apply_to_icon()` in the `else` branch
        # OUTSIDE the try — any Pillow/pystray backend error raised from
        # there would propagate out of the poller/refresh thread and, under
        # the detached `pythonw.exe` tray, vanish into /dev/null. Result:
        # tray shows stale data forever with no signal. Now all steps are
        # caught so the fetch lock is always released cleanly AND the thread
        # survives to try again on the next poll.
        try:
            snap = _snapshot()
            self.snapshot = snap
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

    def _check_thresholds(self, snap: dict) -> None:
        # Threshold-crossing notifications intentionally read only the live
        # snapshot, not last-known-good stale bars. Re-firing toasts on
        # stale data would notify based on the user's yesterday-state every
        # time the tray polls, which is alarm fatigue without new info —
        # the expired-state hint already surfaces in the icon and menu.
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
