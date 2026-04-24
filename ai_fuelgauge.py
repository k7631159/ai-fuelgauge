"""ai_fuelgauge — show remaining quota for Codex + Claude subscriptions.

Displays 5-hour and weekly rate-limit utilization for:
  * OpenAI Codex via `codex app-server` JSON-RPC `account/rateLimits/read`
    (works for Plus / Pro; Business / Enterprise shown as credit balance)
  * Anthropic Claude via `GET /api/oauth/usage` with the OAuth access token
    stored by Claude Code (works for Pro / Max; Team / Enterprise may behave
    differently; API-key users aren't supported — different auth flow)

Invocation:
  python ai_fuelgauge.py                  Show quota (30s cache)
  python ai_fuelgauge.py --json           Machine-readable JSON
  python ai_fuelgauge.py --no-cache       Force refresh
  python ai_fuelgauge.py --debug          Dump raw responses for debugging
  python ai_fuelgauge.py --no-color       Disable ANSI colors

Credential resolution:
  Codex:  ~/.codex/auth.json handled by `codex app-server`
  Claude: $CLAUDE_CODE_OAUTH_TOKEN → $CLAUDE_CONFIG_DIR/.credentials.json
          → ~/.claude/.credentials.json → macOS Keychain ("Claude Code-credentials")

Requirements:
  * Python 3.7+ (stdlib only, no external packages)
  * Codex CLI installed and logged in (to see Codex quota)
  * Claude Code installed and logged in (to see Claude quota)

Known caveats:
  * `codex app-server` is marked experimental — may break on Codex CLI updates.
  * `/api/oauth/usage` is not in Anthropic's public docs and is reserved for
    native Anthropic applications; may change without notice.

License: MIT.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import urllib.error
import urllib.request

# --- paths ---
HOME = Path.home()
CODEX_LOGS_DB = HOME / ".codex" / "logs_2.sqlite"
CODEX_AUTH = HOME / ".codex" / "auth.json"
# Claude credentials location can be overridden by $CLAUDE_CONFIG_DIR;
# on macOS it's stored in Keychain instead of a file.
_CLAUDE_CONFIG_DIR = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else HOME / ".claude"
CLAUDE_CREDS = _CLAUDE_CONFIG_DIR / ".credentials.json"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CACHE_FILE = HOME / ".cache" / "usage-quota.json"
CACHE_TTL_SECONDS = 30
CODEX_APP_SERVER_TIMEOUT = 8  # seconds
CLAUDE_AUTH_REFRESH_TIMEOUT = 10  # seconds for `claude auth status` invocation

# --- ANSI colors (enabled on Windows 10+ cmd via os.system('') trick) ---
if os.name == "nt":
    os.system("")  # enables VT processing on Windows cmd

class C:
    reset = "\x1b[0m"
    bold = "\x1b[1m"
    dim = "\x1b[2m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    cyan = "\x1b[36m"
    gray = "\x1b[90m"


def color_for(percent: float) -> str:
    if percent >= 90:
        return C.red
    if percent >= 70:
        return C.yellow
    return C.green


def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        return "-"
    m, _ = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}d{h:02d}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def bar(percent: float, width: int = 30, use_color: bool = True) -> str:
    """Render an ASCII bar like [====>                         ] width=30."""
    p = max(0.0, min(100.0, percent))
    filled = int(round(p / 100.0 * width))
    if filled == 0:
        body = " " * width
    elif filled >= width:
        body = "=" * width
    else:
        body = "=" * (filled - 1) + ">" + " " * (width - filled)
    if use_color:
        col = color_for(p)
        return f"[{col}{body}{C.reset}]"
    return f"[{body}]"


# --- Codex ---
def read_codex_quota() -> dict | None:
    """Return latest rate_limits from codex logs_2.sqlite or None."""
    if not CODEX_LOGS_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CODEX_LOGS_DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT ts, feedback_log_body FROM logs "
            "WHERE feedback_log_body LIKE '%codex.rate_limits%' "
            "ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        con.close()
    except sqlite3.Error as e:
        return {"error": f"codex sqlite: {e}"}
    if not row:
        return None
    ts, body = row
    idx = body.find('{"type":"codex.rate_limits"')
    if idx < 0:
        return None
    # Walk braces to extract the JSON object.
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(idx, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end < 0:
        return None
    try:
        obj = json.loads(body[idx:end])
    except json.JSONDecodeError:
        return None
    rl = obj.get("rate_limits") or {}
    primary = rl.get("primary") or {}
    secondary = rl.get("secondary") or {}
    now = int(time.time())
    return {
        "plan": obj.get("plan_type", "?"),
        "as_of": ts,
        "primary": {
            "used_percent": primary.get("used_percent"),
            "window_minutes": primary.get("window_minutes"),
            "reset_at": primary.get("reset_at"),
            "reset_in_seconds": (primary.get("reset_at", now) - now) if primary.get("reset_at") else None,
        },
        "secondary": {
            "used_percent": secondary.get("used_percent"),
            "window_minutes": secondary.get("window_minutes"),
            "reset_at": secondary.get("reset_at"),
            "reset_in_seconds": (secondary.get("reset_at", now) - now) if secondary.get("reset_at") else None,
        },
    }


# --- Codex fresh probe (via official `codex app-server` JSON-RPC protocol) ---
def _find_codex_bin() -> str | None:
    """Locate the codex CLI binary."""
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    return None


def probe_codex_fresh(debug: bool = False) -> dict | None:
    """Query codex app-server via JSON-RPC for real-time rate limits.

    Uses method `account/rateLimits/read` which is defined in Codex CLI's
    experimental app-server protocol. More stable than reverse-engineered
    HTTP endpoints because the Codex team maintains the protocol schema.
    """
    codex_bin = _find_codex_bin()
    if not codex_bin:
        return {"error": "codex-not-in-path"}

    popen_kwargs: dict = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        shell=False,
    )
    # Suppress the console window that Windows creates for spawned processes.
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen([codex_bin, "app-server"], **popen_kwargs)
    except Exception as e:
        return {"error": f"spawn-failed: {e}"}

    lines: "queue.Queue[str]" = queue.Queue()
    stderr_buf: list[str] = []

    def reader(stream, sink):
        try:
            for line in iter(stream.readline, b""):
                sink(line.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass

    threading.Thread(target=reader, args=(proc.stdout, lines.put), daemon=True).start()
    threading.Thread(target=reader, args=(proc.stderr, stderr_buf.append), daemon=True).start()

    def send(obj):
        try:
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except Exception:
            pass

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"clientInfo": {"name": "usage-quota", "version": "1.0"}}})
    send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})

    deadline = time.time() + CODEX_APP_SERVER_TIMEOUT
    responses: dict[int, dict] = {}
    while time.time() < deadline and 2 not in responses:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        if isinstance(mid, int):
            responses[mid] = msg

    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass

    result: dict = {}
    if debug:
        result["responses"] = responses
        if stderr_buf:
            result["stderr_tail"] = stderr_buf[-5:]

    resp = responses.get(2)
    if not resp:
        result["error"] = "no-response-from-app-server"
        if stderr_buf and not debug:
            result["stderr_tail"] = stderr_buf[-3:]
        return result
    if "error" in resp:
        result["error"] = f"jsonrpc-error: {resp['error']}"
        return result
    r = resp.get("result") or {}
    rl = r.get("rateLimits") or {}
    if not rl:
        result["error"] = "empty-rateLimits"
        return result

    now = int(time.time())

    def normalize(w):
        if not isinstance(w, dict):
            return {}
        reset_at = w.get("resetsAt") or w.get("reset_at")
        return {
            "used_percent": w.get("usedPercent"),
            "window_minutes": w.get("windowDurationMins") or w.get("window_minutes"),
            "reset_at": reset_at,
            "reset_in_seconds": (int(reset_at) - now) if reset_at else None,
        }

    result.update({
        "plan": rl.get("planType") or "?",
        "as_of": now,
        "primary": normalize(rl.get("primary") or {}),
        "secondary": normalize(rl.get("secondary") or {}),
    })

    # Business / Enterprise plans use a credit model instead of (or alongside) windows.
    credits = rl.get("credits")
    if isinstance(credits, dict):
        result["credits"] = {
            "has_credits": credits.get("hasCredits"),
            "unlimited": credits.get("unlimited"),
            "balance": credits.get("balance"),
        }

    # Optionally surface additional rate limits (e.g. Spark) if any are actually used.
    # The JSON-RPC response echoes the top-level rateLimits under
    # rateLimitsByLimitId (observed key: "codex"), which would otherwise
    # duplicate the main bucket in `additional`. Skip by matching the
    # top-level limitId — authoritative identity check. When limitId is
    # absent we deliberately do NOT fall back to value-matching: a genuinely
    # distinct bucket could coincidentally share values at a given moment,
    # and silently dropping it is worse than letting a possible echo through.
    extra = r.get("rateLimitsByLimitId") or {}
    top_limit_id = rl.get("limitId")
    additional = []
    for lid, block in extra.items():
        if not isinstance(block, dict):
            continue
        if top_limit_id and lid == top_limit_id:
            continue
        p = normalize(block.get("primary") or {})
        s = normalize(block.get("secondary") or {})
        if (p.get("used_percent") or 0) > 0 or (s.get("used_percent") or 0) > 0:
            additional.append({
                "limit_id": lid,
                "limit_name": block.get("limitName") or lid,
                "primary": p,
                "secondary": s,
            })
    if additional:
        result["additional"] = additional
    return result


# --- Claude ---
def _extract_token_from_cred_json(data) -> str | None:
    """Given parsed credentials JSON, find the OAuth access token."""
    for path in (("claudeAiOauth", "accessToken"), ("accessToken",), ("access_token",)):
        cur = data
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, str):
            return cur
    return None


def read_claude_token() -> str | None:
    """Return Claude OAuth access token.

    Resolution order:
      1. $CLAUDE_CODE_OAUTH_TOKEN env var (direct token, for CI)
      2. $CLAUDE_CONFIG_DIR/.credentials.json or ~/.claude/.credentials.json
      3. macOS Keychain service "Claude Code-credentials"
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return env_token
    if CLAUDE_CREDS.exists():
        try:
            data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
            tok = _extract_token_from_cred_json(data)
            if tok:
                return tok
        except Exception:
            pass
    # macOS Keychain fallback
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                raw = out.stdout.strip()
                # Keychain value is usually the full credentials JSON
                try:
                    data = json.loads(raw)
                    tok = _extract_token_from_cred_json(data)
                    if tok:
                        return tok
                except json.JSONDecodeError:
                    # or it may be just the token string
                    if raw.startswith("sk-ant-"):
                        return raw
        except Exception:
            pass
    return None


def _trigger_claude_auth_refresh() -> bool:
    """Ask the official `claude` CLI to refresh its OAuth token, best-effort.

    We don't implement OAuth refresh ourselves — that would mean writing back to
    a credential file we don't own. Instead we invoke `claude auth status`, which
    is a lightweight auth-only subcommand (no API token consumption). If the
    token is expired, the CLI's startup auth path refreshes and rewrites
    `.credentials.json`; if it's already valid, the call is a quick no-op.

    Returns True if the subprocess exited 0, False on any failure. Note: a True
    return does NOT guarantee the token was refreshed — `auth status` may be
    passive in some versions, so callers still need a fallback for persistent 401.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False
    popen_kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        res = subprocess.run(
            [claude_bin, "auth", "status"],
            timeout=CLAUDE_AUTH_REFRESH_TIMEOUT,
            **popen_kwargs,
        )
        return res.returncode == 0
    except Exception:
        return False


def probe_claude_quota(debug: bool = False, _allow_refresh: bool = True) -> dict | None:
    """Read Claude subscription quota via the OAuth usage endpoint.

    Uses `GET /api/oauth/usage` — still undocumented, but cleaner than parsing
    the `anthropic-ratelimit-unified-*` headers off a `/v1/messages` probe:
    structured JSON response AND does not consume an API token (the OAuth
    endpoint is not billed against the model rate-limit budget).
    """
    token = read_claude_token()
    if not token:
        return {"error": "no-token-found"}
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "user-agent": "ai-fuelgauge-probe/0.2",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        status = resp.status
        raw_body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw_body = e.read()
        except Exception:
            raw_body = b""
    except Exception as e:
        return {"error": f"probe-failed: {e}"}

    # 401 typically means the cached OAuth token has expired. Delegate refresh
    # to the official `claude` CLI (it owns the credential file), then retry
    # exactly once. `_allow_refresh=False` on the inner call prevents recursion.
    if status == 401 and _allow_refresh:
        if _trigger_claude_auth_refresh():
            retry = probe_claude_quota(debug=debug, _allow_refresh=False)
            if retry is not None:
                return retry

    result: dict = {"status": status}

    try:
        obj = json.loads(raw_body.decode("utf-8", errors="replace")) if raw_body else None
    except json.JSONDecodeError:
        obj = None

    if debug:
        result["response_body"] = obj if obj is not None else raw_body[:500].decode("utf-8", errors="replace")

    if not isinstance(obj, dict):
        return result

    now = int(time.time())

    def _to_window(block: "dict | None", window_minutes: int) -> "dict | None":
        if not isinstance(block, dict):
            return None
        util = block.get("utilization")
        resets = block.get("resets_at")
        reset_at = None
        reset_in = None
        if isinstance(resets, str):
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(resets.replace("Z", "+00:00"))
                reset_at = int(dt.timestamp())
                reset_in = reset_at - now
            except ValueError:
                pass
        if util is None and reset_in is None:
            return None
        return {
            "window_minutes": window_minutes,
            "used_percent": util,  # endpoint returns 0..100 already
            "reset_at": reset_at,
            "reset_in_seconds": reset_in,
        }

    primary = _to_window(obj.get("five_hour"), 300)
    secondary = _to_window(obj.get("seven_day"), 10080)
    if primary:
        result["primary"] = primary
    if secondary:
        result["secondary"] = secondary

    return result


# --- cache ---
def load_cache(ttl: int) -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(time.time()) - data.get("_cached_at", 0) > ttl:
        return None
    return data


def save_cache(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {**data, "_cached_at": int(time.time())}
        CACHE_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")
    except Exception:
        pass


# --- rendering ---
def print_block(
    name: str,
    plan: str | None,
    primary: dict | None,
    secondary: dict | None,
    use_color: bool,
    as_of_seconds_ago: int | None = None,
    credits: dict | None = None,
    no_data_hint: str | None = None,
) -> None:
    header = f"{C.bold if use_color else ''}{name}{C.reset if use_color else ''}"
    if plan:
        header += f" {C.dim if use_color else ''}{plan}{C.reset if use_color else ''}"
    if as_of_seconds_ago is not None and as_of_seconds_ago > 120:
        note_col = ""
        if use_color:
            note_col = C.yellow if as_of_seconds_ago > 1800 else C.dim
        rst = C.reset if use_color else ""
        header += f"  {note_col}(as of {fmt_duration(as_of_seconds_ago)} ago){rst}"
    print(header)

    have_window_data = False

    def line(label: str, d: dict | None) -> None:
        nonlocal have_window_data
        if not d or d.get("used_percent") is None:
            return
        have_window_data = True
        pct = float(d["used_percent"])
        reset_s = d.get("reset_in_seconds")
        reset_str = fmt_duration(int(reset_s)) if reset_s is not None else "-"
        col = color_for(pct) if use_color else ""
        rst = C.reset if use_color else ""
        print(f"  {label:8} {col}{pct:4.0f}%{rst}  {bar(pct, use_color=use_color)}  reset {reset_str}")

    line("5h", primary)
    line("week", secondary)

    if credits and (credits.get("has_credits") or credits.get("unlimited") or credits.get("balance") not in (None, "0")):
        bal = credits.get("balance")
        if credits.get("unlimited"):
            text = "unlimited"
        elif bal is not None:
            text = f"balance {bal}"
        else:
            text = "credits plan"
        col = C.cyan if use_color else ""
        rst = C.reset if use_color else ""
        print(f"  {'credits':8} {col}{text}{rst}")
        have_window_data = True

    if not have_window_data:
        hint = no_data_hint or "no quota data (Team/Enterprise plan or API-key user?)"
        col = C.gray if use_color else ""
        rst = C.reset if use_color else ""
        print(f"  {col}{hint}{rst}")
    print()


def interval_seconds(value: str) -> int:
    """argparse type: parse --interval as a positive integer (seconds, >= 1).

    Shared by this CLI and tray.py's standalone entry so both reject the same
    inputs. Keeping the offending value in the error message makes the
    argparse diagnostic actionable.
    """
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 second, got {n}")
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Show Codex + Claude remaining quota")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--debug", action="store_true", help="Include all response headers (debug Claude)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--tray", action="store_true",
                    help="Run as system tray app (requires: pip install pystray Pillow winotify)")
    ap.add_argument("--interval", type=interval_seconds, default=300,
                    help="Tray poll interval in seconds (default: 300, minimum: 1)")
    args = ap.parse_args(argv)

    if args.tray:
        try:
            from tray import run_tray
        except ImportError as e:
            sys.stderr.write(f"tray mode requires extra deps: {e}\n")
            sys.stderr.write("Install with: pip install --user pystray Pillow winotify\n")
            return 2
        return run_tray(interval=args.interval)

    use_color = (not args.no_color) and sys.stdout.isatty()

    cache = None if args.no_cache else load_cache(CACHE_TTL_SECONDS)
    if cache and not args.debug:
        data = cache
        data["_from_cache"] = True
    else:
        # Prefer fresh Codex probe; fall back to sqlite snapshot if probe fails.
        codex_fresh = probe_codex_fresh(debug=args.debug)
        codex_stale = read_codex_quota()
        if codex_fresh and not codex_fresh.get("error") and codex_fresh.get("primary", {}).get("used_percent") is not None:
            codex = codex_fresh
            codex["_source"] = "fresh-api"
        else:
            codex = codex_stale or {}
            if isinstance(codex, dict):
                codex["_source"] = "sqlite-snapshot"
                codex["_fresh_probe_error"] = codex_fresh.get("error") if codex_fresh else "no-data"
                # keep debug payload if present
                if args.debug and codex_fresh:
                    codex["_fresh_debug"] = {k: v for k, v in codex_fresh.items() if k in ("status","response_body","headers","response_body_sample")}
        claude = probe_claude_quota(debug=args.debug)
        data = {"codex": codex, "claude": claude, "_from_cache": False}
        if not args.debug:
            save_cache(data)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    codex = data.get("codex")
    claude = data.get("claude")

    if codex is None:
        print(f"{C.gray if use_color else ''}Codex: no local data{C.reset if use_color else ''}\n")
    elif isinstance(codex, dict) and codex.get("error"):
        print(f"{C.red if use_color else ''}Codex error: {codex['error']}{C.reset if use_color else ''}\n")
    else:
        as_of = codex.get("as_of")
        stale_seconds = (int(time.time()) - int(as_of)) if as_of else None
        print_block(
            "Codex",
            None,  # plan tier is tracked in JSON output but not shown in the display
            codex.get("primary"),
            codex.get("secondary"),
            use_color,
            as_of_seconds_ago=stale_seconds,
            credits=codex.get("credits"),
            no_data_hint="no rate-limit data (Business/Enterprise credit plan?)",
        )
        if stale_seconds and stale_seconds > 1800:
            note = (
                "  " + (C.dim if use_color else "") +
                "tip: Codex rate_limits only refresh when you use Codex Desktop. "
                "Run one turn there to refresh."
                + (C.reset if use_color else "")
            )
            print(note)

    if claude is None:
        print(f"{C.gray if use_color else ''}Claude: no data{C.reset if use_color else ''}")
    elif claude.get("error"):
        print(f"{C.red if use_color else ''}Claude error: {claude['error']}{C.reset if use_color else ''}")
    else:
        status = claude.get("status")
        http_error = bool(status) and status >= 400
        primary = claude.get("primary") or {}
        secondary = claude.get("secondary") or {}
        # 429 (and some 4xx) still carry rate-limit headers, so probe_claude_quota
        # may have parsed useful reset/utilization data even on an error response.
        # Surface the quota block whenever anything was extracted; the HTTP status
        # is then shown as a trailing note rather than suppressing the data.
        have_parsed_data = any(
            w.get("used_percent") is not None or w.get("reset_in_seconds") is not None
            for w in (primary, secondary)
        )
        if http_error and not have_parsed_data:
            col = C.red if use_color else ""
            dim = C.dim if use_color else ""
            rst = C.reset if use_color else ""
            if status == 401:
                print(f"{col}Claude probe HTTP 401 — auth token expired or invalid{rst}")
                print(f"  {dim}Auto-refresh via `claude auth status` didn't restore access.{rst}")
                print(f"  {dim}Run `claude` once to re-authenticate, then retry.{rst}")
            else:
                print(f"{col}Claude probe HTTP {status}{rst}")
        else:
            print_block(
                "Claude",
                None,
                primary,
                secondary,
                use_color,
                no_data_hint="no unified rate-limit headers (API-key user or Team/Enterprise plan?)",
            )
            if http_error:
                col = C.yellow if use_color else ""
                rst = C.reset if use_color else ""
                print(f"  {col}note: HTTP {status} from probe — figures above parsed from response headers{rst}")
        if args.debug:
            print("--- /api/oauth/usage response ---")
            body = claude.get("response_body")
            if isinstance(body, dict):
                print(json.dumps(body, indent=2, default=str))
            elif body:
                print(body)

    if data.get("_from_cache"):
        print(f"{C.gray if use_color else ''}(cached, TTL {CACHE_TTL_SECONDS}s){C.reset if use_color else ''}")
    return 0


def main_entry() -> None:
    """Entry point for `pip install` generated console_scripts."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_entry()
