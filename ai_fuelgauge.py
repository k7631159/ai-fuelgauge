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
import hashlib
import json
import math
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
# Treat the token as expired if it expires within this many seconds. Avoids
# the race where a token passes the local check, then expires mid-flight
# and the server returns 401. Codex suggested 60s; bigger means more
# proactive refreshes, smaller means more in-flight expirations.
CLAUDE_TOKEN_EXPIRY_BUFFER_SECONDS = 60

# Last-known-good Claude probe — preserved beyond the 30s probe cache so the
# expired-token UI can render stale-but-actionable bars instead of a bare
# "expired" placeholder. Stays bounded to a single quota window's worth of
# usefulness; past 24h the data is too stale to be informative.
LAST_GOOD_CLAUDE_FILE = HOME / ".cache" / "usage-quota-last-claude.json"
LAST_GOOD_CLAUDE_TTL_SECONDS = 24 * 3600
# Bump if the saved record's shape changes; old records become invalid.
LAST_GOOD_CLAUDE_SCHEMA = 1
# Within this many seconds of a window's resets_at, the cached util is treated
# as rolled-over (not displayable). Avoids rendering a number that flips to a
# new window the moment the user reads it.
LAST_GOOD_BAR_RESET_GUARD_SECONDS = 60

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
def _normalize_codex_window(w: "dict | None", now: int) -> dict:
    """Normalize one primary/secondary window from a Codex JSON-RPC response.

    Module-level (not nested) so tests can exercise it directly without
    spawning `codex app-server`. Tolerant to malformed fields — a response
    with a string `resetsAt`, missing keys, or the wrong shape should yield
    a best-effort dict, never raise.
    """
    if not isinstance(w, dict):
        return {}
    # Fall back to snake_case `reset_at` only when the canonical `resetsAt` is
    # absent OR explicitly None. Using `.get()` + `is None` instead of
    # `w.get("resetsAt") or w.get("reset_at")` so that a legitimate `0` is
    # preserved (it's falsy but not None).
    reset_at = w.get("resetsAt")
    if reset_at is None:
        reset_at = w.get("reset_at")
    reset_in = None
    if reset_at is not None:
        try:
            reset_in = int(reset_at) - now
        except (TypeError, ValueError):
            # Non-numeric timestamp (e.g. ISO string from a future plan-tier
            # response variant) — surface `reset_at` as-is and leave the
            # countdown unknown rather than crashing the whole probe.
            pass
    return {
        "used_percent": w.get("usedPercent"),
        "window_minutes": w.get("windowDurationMins") or w.get("window_minutes"),
        "reset_at": reset_at,
        "reset_in_seconds": reset_in,
    }


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
    # Full shutdown: terminate → wait with bounded timeout → kill as last resort.
    # Without the explicit wait(), terminate() signals the child but the parent
    # returns before the child actually exits, leaving a Unix zombie entry and
    # keeping the reader-thread pipe FDs open until eventual GC. Over a 24h
    # tray-polling session (~288 spawns) that accumulates.
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass  # truly stuck; nothing else we can safely do
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

    result.update({
        "plan": rl.get("planType") or "?",
        "as_of": now,
        "primary": _normalize_codex_window(rl.get("primary") or {}, now),
        "secondary": _normalize_codex_window(rl.get("secondary") or {}, now),
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
        p = _normalize_codex_window(block.get("primary") or {}, now)
        s = _normalize_codex_window(block.get("secondary") or {}, now)
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


def _extract_expires_at_from_cred_json(data) -> "int | None":
    """Find Claude Code's `expiresAt` (Unix epoch milliseconds) in the
    credentials JSON. Returns None when absent or non-numeric — callers
    should treat that as 'unknown expiry, fall through to reactive 401
    handling' rather than 'expired'.
    """
    for path in (("claudeAiOauth", "expiresAt"), ("expiresAt",), ("expires_at",)):
        cur = data
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, (int, float)):
            return int(cur)
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


def _token_fingerprint(token: "str | None") -> "str | None":
    """SHA256[:12] of a token. For trace/diff comparisons; never leaks the
    token itself. Used to detect whether `claude auth status` actually
    rewrote credentials, instead of trusting the subprocess returncode
    (which can be 0 even when refresh silently failed)."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _is_env_token_mode() -> bool:
    """True iff the current token came from $CLAUDE_CODE_OAUTH_TOKEN.
    Env tokens are static — `claude auth status` can't refresh them — so the
    401 retry path should skip the spawn entirely and surface a clear
    'replace your env var' message."""
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def read_claude_creds_with_meta() -> "dict | None":
    """Return current Claude credentials as `{access_token, expires_at_ms, source}`.

    Resolution order matches `read_claude_token`:
      1. $CLAUDE_CODE_OAUTH_TOKEN env var (no expires_at_ms — env tokens
         are static and cannot be auto-refreshed)
      2. credentials.json (file mode — has expires_at_ms when the field
         is well-formed)
      3. macOS Keychain ("Claude Code-credentials")

    `expires_at_ms` is None when:
      - source is env (no metadata)
      - keychain returned a bare token string (not JSON)
      - the JSON was malformed or the field was missing/non-numeric

    Callers use the metadata for the proactive expiry check; missing
    metadata means "fall through to reactive 401 handling" rather than
    "assume expired" — the latter would needlessly skip probes for any
    keychain-only setup that doesn't expose expiry info.
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {"access_token": env_token, "expires_at_ms": None, "source": "env"}

    if CLAUDE_CREDS.exists():
        try:
            data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
            tok = _extract_token_from_cred_json(data)
            if tok:
                return {
                    "access_token": tok,
                    "expires_at_ms": _extract_expires_at_from_cred_json(data),
                    "source": "file",
                }
        except Exception:
            pass

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                raw = out.stdout.strip()
                try:
                    data = json.loads(raw)
                    tok = _extract_token_from_cred_json(data)
                    if tok:
                        return {
                            "access_token": tok,
                            "expires_at_ms": _extract_expires_at_from_cred_json(data),
                            "source": "keychain",
                        }
                except json.JSONDecodeError:
                    if raw.startswith("sk-ant-"):
                        return {"access_token": raw, "expires_at_ms": None, "source": "keychain-bare"}
        except Exception:
            pass

    return None


def _is_token_expired_or_expiring(expires_at_ms: "int | None",
                                   buffer_seconds: int = CLAUDE_TOKEN_EXPIRY_BUFFER_SECONDS) -> bool:
    """True iff expires_at_ms is in the past or within `buffer_seconds`.

    Returns False when expires_at_ms is None — that means we have no expiry
    info (env token, keychain-bare, malformed JSON). The reactive 401 path
    is the safety net for those cases.
    """
    if expires_at_ms is None:
        return False
    now_ms = int(time.time() * 1000)
    return expires_at_ms <= now_ms + (buffer_seconds * 1000)


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

    Uses `GET /api/oauth/usage` — undocumented but returns structured JSON
    (`five_hour` / `seven_day` blocks with utilization + reset timestamps)
    and does not consume an API token (the OAuth endpoint is not billed
    against the model rate-limit budget).
    """
    # Proactive token expiry check — root-cause defense, not a backstop.
    # Today's incident showed CF treats expired-bearer requests as abuse and
    # locks the IP for ~30 minutes. The cheapest way to avoid that is to
    # never send an expired bearer in the first place: read expiresAt from
    # credentials.json locally, refresh proactively if expired, and bail
    # out without a network call if refresh truly fails.
    creds = read_claude_creds_with_meta()
    if creds is None:
        return {"error": "no-token-found"}
    token = creds["access_token"]

    if _is_token_expired_or_expiring(creds.get("expires_at_ms")):
        # Expiry guard is unconditional — even on the reactive 401 retry
        # (where _allow_refresh=False), we MUST NOT send a known-stale
        # bearer to the upstream. The README's contract is "no network
        # request is made if refresh doesn't produce a new, non-expired
        # token", and that has to hold across both the proactive entry
        # and any internal retry.
        if _is_env_token_mode():
            # Env token can't be auto-refreshed — surface a clear local error
            # without hitting the network at all.
            return {"error": "env-token-expired", "_proactive_skip": True,
                    "_expires_at_ms": creds.get("expires_at_ms")}
        if not _allow_refresh:
            # Reactive 401 retry path discovered the freshly-refreshed
            # token is also stale. Don't try to refresh again (recursion
            # guard); bail proactively.
            return {"error": "auth-expired-no-refresh",
                    "_proactive_skip": True,
                    "_refresh_attempted": True,
                    "_token_changed": False,
                    "_expires_at_ms": creds.get("expires_at_ms")}
        before_fp = _token_fingerprint(token)
        _trigger_claude_auth_refresh()
        creds = read_claude_creds_with_meta() or creds
        token = creds["access_token"]
        after_fp = _token_fingerprint(token)
        # Refresh worked iff fingerprint changed AND new token is no longer
        # in the expiry window. Either alone isn't sufficient — a refreshed
        # token that's STILL stale (server clock skew? bug?) shouldn't be
        # used.
        token_changed = bool(after_fp) and after_fp != before_fp
        still_expired = _is_token_expired_or_expiring(creds.get("expires_at_ms"))
        if not token_changed or still_expired:
            return {
                "error": "auth-expired-no-refresh",
                "_proactive_skip": True,
                "_refresh_attempted": True,
                "_token_changed": token_changed,
                "_expires_at_ms": creds.get("expires_at_ms"),
            }

    request_token_fp = _token_fingerprint(token)
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
        # Context manager ensures the underlying socket / fd is released
        # promptly instead of waiting for GC. Matters under tray polling
        # (every 5 min) and during transient retry storms.
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            raw_body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw_body = e.read()
        except Exception:
            raw_body = b""
        finally:
            # HTTPError wraps a file-like body the same way a normal response
            # does — without close() it leaks fds during 4xx/5xx retry storms
            # (e.g. rate-limit bursts from /api/oauth/usage).
            try:
                e.close()
            except Exception:
                pass
    except Exception as e:
        return {"error": f"probe-failed: {e}"}

    # 401 → token-fingerprint-aware refresh path.
    # Logic (see commit msg for rationale):
    #   1. Re-read token. If it's already different from the one that 401'd,
    #      another process refreshed for us — retry directly without spawning.
    #   2. Skip spawn entirely in env-token mode (env tokens can't refresh).
    #   3. Otherwise spawn `claude auth status`. After spawn, re-read token.
    #      ONLY retry if fingerprint actually changed. Subprocess returncode
    #      is informational; trusting it caused today's bug where refresh
    #      "succeeded" (exit 0) but didn't rewrite credentials.
    if status == 401 and _allow_refresh:
        if _is_env_token_mode():
            # Caller will see status=401 + this marker and surface a
            # 'replace env var' message instead of 'run claude'.
            return _claude_finalize(status, raw_body, debug, env_token_mode=True)

        current_token = read_claude_token()
        current_fp = _token_fingerprint(current_token)
        if current_fp and current_fp != request_token_fp:
            # Race A: another process already refreshed — retry directly.
            retry = probe_claude_quota(debug=debug, _allow_refresh=False)
            if retry is not None:
                return retry
        else:
            refresh_returncode_ok = _trigger_claude_auth_refresh()
            post_token = read_claude_token()
            post_fp = _token_fingerprint(post_token)
            if post_fp and post_fp != request_token_fp:
                # Refresh actually rewrote credentials (whatever returncode said).
                retry = probe_claude_quota(debug=debug, _allow_refresh=False)
                if retry is not None:
                    return retry
            # Token unchanged → refresh truly failed. Mark so UI explains
            # 'auto-refresh didn't help'.
            return _claude_finalize(
                status, raw_body, debug,
                refresh_attempted=True,
                refresh_subprocess_ok=refresh_returncode_ok,
            )

    return _claude_finalize(status, raw_body, debug)


def _claude_finalize(status: int, raw_body: bytes, debug: bool,
                     env_token_mode: bool = False,
                     refresh_attempted: bool = False,
                     refresh_subprocess_ok: "bool | None" = None) -> dict:
    """Parse a /api/oauth/usage response body into the canonical result.

    Optional markers describe local auth-state context that the caller may
    use to render actionable error messages distinct from raw HTTP codes:
      * env_token_mode=True   → 401 came from an env-var token that we
                                cannot auto-refresh.
      * refresh_attempted=True → we did spawn `claude auth status` but
                                  the token fingerprint did not change,
                                  so the refresh effectively failed.
    """
    result: dict = {"status": status}
    if env_token_mode:
        result["_env_token_mode"] = True
    if refresh_attempted:
        result["_refresh_attempted"] = True
        if refresh_subprocess_ok is not None:
            result["_refresh_subprocess_ok"] = refresh_subprocess_ok

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
    # Corrupt state that parses as valid JSON but the wrong top-level shape
    # (e.g. a partial write that landed as `"null"`, `[]`, or a bare number)
    # would make `data.get(...)` raise AttributeError — which the except above
    # does NOT catch. Reject non-dict shapes explicitly so the cache self-heals.
    if not isinstance(data, dict):
        return None
    cached_at = data.get("_cached_at", 0)
    if not isinstance(cached_at, (int, float)):
        return None
    if int(time.time()) - cached_at > ttl:
        return None
    return data


def save_cache(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {**data, "_cached_at": int(time.time())}
        CACHE_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")
    except Exception:
        pass


# --- last-known-good Claude (stale-bar fallback for expired-token paths) ---
def _save_last_good_claude(claude: "dict | None") -> None:
    """Persist a successful Claude probe so the next expired-token probe can
    render stale bars instead of a bare 'expired' placeholder.

    Saves only when the result clearly came from a usable response (no error,
    HTTP < 400, at least one window block parsed). Records only the bar-render
    fields — token / refresh / debug metadata is intentionally dropped.
    """
    if not isinstance(claude, dict):
        return
    if claude.get("error"):
        return
    status = claude.get("status")
    if isinstance(status, int) and status >= 400:
        return
    primary = claude.get("primary")
    secondary = claude.get("secondary")
    if not (isinstance(primary, dict) or isinstance(secondary, dict)):
        return
    record = {
        "_schema": LAST_GOOD_CLAUDE_SCHEMA,
        "_probed_at": int(time.time()),
        "primary": primary if isinstance(primary, dict) else None,
        "secondary": secondary if isinstance(secondary, dict) else None,
    }
    # Atomic write: tray polls every 5min while the CLI runs ad hoc, so the
    # file can be opened for read mid-write. write-temp + os.replace gives
    # readers either the old record or the new one — never a torn JSON
    # blob that would bounce them to the empty-fallback path for one tick.
    # `tempfile.mkstemp` gives a per-process unique temp name so two
    # simultaneous savers (e.g. CLI + tray firing at the same second)
    # can't trample each other's in-flight write before the rename.
    try:
        import tempfile
        parent = LAST_GOOD_CLAUDE_FILE.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(parent),
            prefix=LAST_GOOD_CLAUDE_FILE.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str))
            os.replace(tmp_path, LAST_GOOD_CLAUDE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _load_last_good_claude() -> "dict | None":
    """Return the last-known-good Claude record if it's recent and trusted.

    Rejects: missing file, unreadable JSON, non-dict shape, schema mismatch,
    age > 24h, and clock-skew (cached time in the future). The schema gate
    ensures we never render bars from a record shape the current code no
    longer trusts.
    """
    if not LAST_GOOD_CLAUDE_FILE.exists():
        return None
    try:
        data = json.loads(LAST_GOOD_CLAUDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("_schema") != LAST_GOOD_CLAUDE_SCHEMA:
        return None
    probed_at = data.get("_probed_at")
    if not isinstance(probed_at, (int, float)) or isinstance(probed_at, bool):
        return None
    age = int(time.time()) - int(probed_at)
    if age < 0:
        # Cached probe time is in the future — clock skew or tampering.
        return None
    if age > LAST_GOOD_CLAUDE_TTL_SECONDS:
        return None
    return data


def _stale_bar_status(window: "dict | None") -> str:
    """Classify a cached window for display: 'valid', 'rolled_over', 'no_data'.

    A window is rolled_over once the original resets_at is at or past
    (now + guard) — at that point the cached util belongs to a window that
    has already ended, so showing it would mislead. The 60s guard avoids
    rendering a value that flips to a new window in the user's face.

    'valid' requires both a finite numeric used_percent AND a numeric
    reset_at. Without a trustworthy reset_at we can't distinguish a
    rolled-over window from one still in flight, so we refuse to
    display the bar — silently showing potentially-rolled-over numbers
    would undercut the "omit rolled-over" contract.
    """
    if not isinstance(window, dict):
        return "no_data"
    used = window.get("used_percent")
    reset_at = window.get("reset_at")
    if used is None and reset_at is None:
        return "no_data"
    if not isinstance(reset_at, (int, float)) or isinstance(reset_at, bool):
        return "no_data"
    now = int(time.time())
    if int(reset_at) <= now + LAST_GOOD_BAR_RESET_GUARD_SECONDS:
        return "rolled_over"
    if used is None or isinstance(used, bool):
        # bool is a subclass of int in Python; without the explicit guard,
        # `float(True)` would silently render as 1% utilization.
        return "no_data"
    try:
        v = float(used)
    except (TypeError, ValueError):
        return "no_data"
    if math.isnan(v) or math.isinf(v):
        return "no_data"
    return "valid"


def _format_stale_age(seconds: int) -> str:
    """Render age as a human, rounded-down string: '0m', '45m', '3h', '1d'.

    Rounded down (not nearest) so a "3h stale" label never overstates
    freshness. No decimals — false precision on cached data is misleading.
    """
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


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
        if isinstance(d["used_percent"], bool):
            # bool is a subclass of int; without this guard True/False from
            # an evolved endpoint would render as a 1% / 0% bar.
            return
        try:
            pct = float(d["used_percent"])
        except (TypeError, ValueError):
            # Non-numeric utilization from a hostile / evolved endpoint —
            # treat as "no data" rather than crashing the render.
            return
        # json.loads accepts NaN/Infinity via parse_constant by default.
        # Those propagate through max/min/round and blow up bar() at
        # int(round(NaN)). Reject here before anything else reads `pct`.
        if math.isnan(pct) or math.isinf(pct):
            return
        have_window_data = True
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


def _render_claude(claude: dict, use_color: bool, debug: bool) -> None:
    """Render the Claude block with one branch per error state.

    Distinguishes the proactive auth-state errors (no network call was made,
    so they get 'safe to ignore + how to fix' messaging) from reactive HTTP
    statuses (real server response). The 401 path further splits by why the
    auto-refresh path didn't help so users know whether to run `claude` or
    replace an env var.
    """
    err = claude.get("error")
    status = claude.get("status")
    primary = claude.get("primary") or {}
    secondary = claude.get("secondary") or {}
    have_parsed_data = any(
        w.get("used_percent") is not None or w.get("reset_in_seconds") is not None
        for w in (primary, secondary)
    )
    http_error = bool(status) and status >= 400

    # Map both proactive and reactive env-token expiry to the same UX
    # path so CLI matches tray's classifier (which collapses these into
    # a single `envtok` state). The reactive case arrives without an
    # `error` key — the probe got an HTTP response, just a 401 — so
    # detect via the `_env_token_mode` marker. Skipped when the body
    # actually parsed window data (rare but possible), so the legacy
    # 401-with-body path keeps working.
    expiry_kind = None
    if err == "auth-expired-no-refresh":
        expiry_kind = "auth-expired-no-refresh"
    elif err == "env-token-expired":
        expiry_kind = "env-token-expired"
    elif status == 401 and claude.get("_env_token_mode") and not have_parsed_data:
        expiry_kind = "env-token-expired"
    if expiry_kind:
        _render_claude_expired(claude, expiry_kind, use_color)
        # The proactive expiry paths return before any HTTP call, so
        # response_body is absent and `_print_response_body` is a no-op.
        # The reactive env-token 401 path DOES carry a body when
        # debug=True (the 401 response from upstream), and the user
        # asked for raw payloads — print it before returning.
        if debug:
            _print_response_body(claude)
        return
    if err:
        # no-token-found, probe-failed: …, or anything unclassified.
        print(f"{C.red if use_color else ''}Claude error: {err}{C.reset if use_color else ''}")
        if debug:
            _print_response_body(claude)
        return

    if http_error and not have_parsed_data:
        col = C.red if use_color else ""
        dim = C.dim if use_color else ""
        rst = C.reset if use_color else ""
        if status == 401:
            # The env-token branch is handled earlier via the expiry-kind
            # routing — by the time we land here, _env_token_mode is False
            # (or have_parsed_data was True, in which case we'd be in the
            # `else` arm rendering bars).
            refresh_attempted = bool(claude.get("_refresh_attempted"))
            print(f"{col}Claude probe HTTP 401 — auth token expired or invalid{rst}")
            if refresh_attempted:
                print(f"  {dim}Auto-refresh via `claude auth status` didn't restore access.{rst}")
                print(f"  {dim}Run `claude` once to re-authenticate, then retry.{rst}")
            else:
                print(f"  {dim}Run `claude` once to re-authenticate, then retry.{rst}")
        else:
            print(f"{col}Claude probe HTTP {status}{rst}")
    else:
        print_block(
            "Claude", None, primary, secondary, use_color,
            no_data_hint="no utilization data returned (API-key user or Team/Enterprise plan?)",
        )
        if http_error:
            col = C.yellow if use_color else ""
            rst = C.reset if use_color else ""
            print(f"  {col}note: HTTP {status} from probe — figures above parsed from response body{rst}")

    if debug:
        _print_response_body(claude)


def _render_claude_expired(claude: dict, err: str, use_color: bool) -> None:
    """Render the proactive-expiry branch with last-known-good stale bars
    when a recent successful probe is on file, or fall back to a plain
    actionable error when it isn't.

    The stale path preserves ambient usefulness (the user still sees
    yesterday's 5h / week numbers) without pretending they're fresh — the
    header marks staleness, each bar carries a (stale) tag, and rolled-over
    windows are explicitly omitted rather than silently misrepresented.
    """
    col = C.red if use_color else ""
    dim = C.dim if use_color else ""
    rst = C.reset if use_color else ""

    last_good = _load_last_good_claude()
    primary_status = "no_data"
    secondary_status = "no_data"
    if last_good is not None:
        primary_status = _stale_bar_status(last_good.get("primary"))
        secondary_status = _stale_bar_status(last_good.get("secondary"))
    have_displayable_stale = (
        last_good is not None
        and (primary_status == "valid" or secondary_status == "valid")
    )

    if have_displayable_stale:
        age = int(time.time()) - int(last_good["_probed_at"])
        age_text = _format_stale_age(age)
        if err == "env-token-expired":
            print(f"{col}Claude  ($CLAUDE_CODE_OAUTH_TOKEN expired — cached {age_text} ago){rst}")
        else:
            print(f"{col}Claude  (cached {age_text} ago; token expired){rst}")
        _render_stale_bar("5h", last_good.get("primary"), primary_status, use_color)
        _render_stale_bar("week", last_good.get("secondary"), secondary_status, use_color)
        if err == "env-token-expired":
            print(f"  {dim}Replace $CLAUDE_CODE_OAUTH_TOKEN with a fresh token, or unset it.{rst}")
        else:
            print(f"  {dim}To refresh: open `claude`, type `/exit`, then run `usage` again.{rst}")
        print()
        return

    # No usable stale data — fall back to the plain expired actionable error.
    if err == "env-token-expired":
        print(f"{col}Claude: $CLAUDE_CODE_OAUTH_TOKEN appears expired{rst}")
        print(f"  {dim}Env tokens can't be auto-refreshed. Replace the env var with a fresh token,{rst}")
        print(f"  {dim}or unset it to fall back to ~/.claude/.credentials.json + `claude` login.{rst}")
        return

    expires_at_ms = claude.get("_expires_at_ms")
    ago_text = ""
    if expires_at_ms:
        mins_ago = (int(time.time() * 1000) - expires_at_ms) / 1000 / 60
        if mins_ago > 60:
            ago_text = f" ({mins_ago / 60:.1f}h ago)"
        elif mins_ago > 0:
            ago_text = f" ({mins_ago:.0f}m ago)"
    print(f"{col}Claude: auth token expired{ago_text}{rst}")
    print(f"  {dim}To refresh: open `claude`, type `/exit` in the prompt, then run `usage` again.{rst}")
    print(f"  {dim}Claude CLI has no non-interactive refresh — this is a one-time manual step.{rst}")
    print(f"  {dim}(No HTTP request was made — proactive skip avoids rate-limit triggers.){rst}")


def _render_stale_bar(label: str, window: "dict | None", status: str, use_color: bool) -> None:
    """Print one stale Claude bar honoring per-bar validity.

    'no_data'      — silent (the bar didn't exist in the cached probe).
    'rolled_over'  — explicit "(cached window already reset — omitted)" so
                     the absence of a number is intentional, not a layout
                     glitch.
    'valid'        — render bar with re-derived "reset in N" from the
                     original reset_at (so the countdown stays accurate
                     even hours after the probe), tagged (stale).
    """
    dim = C.dim if use_color else ""
    rst = C.reset if use_color else ""
    if status == "no_data":
        return
    if status == "rolled_over":
        print(f"  {label:8} {dim}(cached window already reset — omitted){rst}")
        return
    if not isinstance(window, dict):
        return
    pct = window.get("used_percent")
    try:
        pct_f = float(pct)
    except (TypeError, ValueError):
        return
    if math.isnan(pct_f) or math.isinf(pct_f):
        return
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        reset_in = int(reset_at) - int(time.time())
    else:
        reset_in = window.get("reset_in_seconds")
    reset_str = fmt_duration(int(reset_in)) if isinstance(reset_in, (int, float)) else "-"
    col = color_for(pct_f) if use_color else ""
    bar_str = bar(pct_f, use_color=use_color)
    print(f"  {label:8} {col}{pct_f:4.0f}%{rst}  {bar_str}  reset {reset_str}  {dim}(stale){rst}")


def _print_response_body(claude: dict) -> None:
    """Dump the parsed response body for --debug. No-op when there isn't one
    (e.g., proactive skip paths return before any HTTP call)."""
    body = claude.get("response_body")
    if body is None:
        return
    print("--- /api/oauth/usage response ---")
    if isinstance(body, dict):
        print(json.dumps(body, indent=2, default=str))
    else:
        print(body)


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
    ap.add_argument("--debug", action="store_true", help="Dump raw API responses for debugging")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--tray", action="store_true",
                    help="Run as system tray app (install deps: pip install -r requirements-tray.txt)")
    ap.add_argument("--interval", type=interval_seconds, default=300,
                    help="Tray poll interval in seconds (default: 300, minimum: 1)")
    ap.add_argument("--no-detach", action="store_true",
                    help="(Windows tray) stay in the foreground instead of auto-relaunching as pythonw "
                         "detached — useful for debugging so stderr stays visible")
    args = ap.parse_args(argv)

    if args.tray:
        try:
            from tray import run_tray
        except ImportError as e:
            sys.stderr.write(f"tray mode requires extra dependencies: {e}\n")
            sys.stderr.write("Install with: pip install --user -r requirements-tray.txt\n")
            return 2
        return run_tray(interval=args.interval, detach=not args.no_detach)

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
        # Persist the last successful Claude probe so the next expired-token
        # render (CLI or tray) can show stale bars rather than a bare
        # "expired" placeholder. No-op when the probe didn't succeed.
        _save_last_good_claude(claude)
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
        stale_seconds = None
        # bool is a subclass of int in Python, so `isinstance(False, int)` is
        # True. Exclude it explicitly — `int(False) == 0` would make a boolean
        # sqlite cell render as a 1970-epoch "stale" marker, which is wrong.
        if as_of is not None and not isinstance(as_of, bool):
            try:
                stale_seconds = int(time.time()) - int(as_of)
            except (TypeError, ValueError):
                # sqlite ts column can hold arbitrary content; don't let a
                # non-numeric value crash the whole render after we've
                # already printed the Codex header.
                pass
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
    else:
        _render_claude(claude, use_color, args.debug)

    if data.get("_from_cache"):
        print(f"{C.gray if use_color else ''}(cached, TTL {CACHE_TTL_SECONDS}s){C.reset if use_color else ''}")
    return 0


def main_entry() -> None:
    """Entry point for `pip install` generated console_scripts."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_entry()
