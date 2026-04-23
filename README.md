# ai-fuelgauge

> Peek at your AI subscription fuel gauge — remaining quota for OpenAI Codex + Anthropic Claude, side by side.

A small cross-platform Python CLI that shows how much of your 5-hour and weekly
rate-limit windows you've already burned through, for both subscriptions at once.
Optional system-tray mode with threshold notifications.

## Why

If you pay for both a ChatGPT subscription (for Codex) and a Claude subscription
(for Claude Code), you currently have to click into two different web dashboards
to see how much quota is left. This tool gives you one glance.

## Example output

```
Codex plus
  5h         29%  [========>                     ]  reset 3h03m
  week       25%  [=======>                      ]  reset 5d17h

Claude
  5h         29%  [========>                     ]  reset 1h35m
  week       15%  [===>                          ]  reset 15h35m
```

Colors: green &lt; 70 %, orange 70–89 %, red ≥ 90 %.

## Install

```bash
git clone https://github.com/k7631159/ai-fuelgauge.git
cd ai-fuelgauge
```

Everything else is standard library — no install needed for the CLI.
For tray mode, install extra deps:

```bash
pip install --user -r requirements-tray.txt
```

## Usage

### CLI (one-shot glance)

```bash
python ai_fuelgauge.py                 # plain output
python ai_fuelgauge.py --json          # machine-readable JSON
python ai_fuelgauge.py --no-cache      # force fresh probe
python ai_fuelgauge.py --debug         # dump raw API responses
```

Wrap it in a shell alias (Windows `usage.cmd` / Linux `usage` symlink) so you
can just type `usage`.

### Tray app (always-visible)

```bash
python ai_fuelgauge.py --tray          # runs until you right-click → Quit
python ai_fuelgauge.py --tray --interval 600   # poll every 10 min
```

The tray icon shows a coloured dot reflecting your highest utilization across
both subscriptions. Right-click for the detailed menu and a **Refresh now**
option. A desktop notification fires when:

- 5-hour window hits 80 %
- Weekly window hits 90 %

Notifications use Windows toast (`winotify`) or `plyer` elsewhere.

## Support matrix (v0.1.0-preview)

| Environment | CLI | Tray | Notes |
|---|---|---|---|
| Windows 10/11 + ChatGPT Plus + Claude Pro | ✅ tested | ✅ tested | author's daily driver |
| Windows + Pro / Max | ✅ expected to work | ✅ expected to work | same headers & JSON-RPC |
| macOS + Plus/Pro/Max | ⚠️ untested | ⚠️ untested | macOS Keychain path is coded but not verified — feedback wanted |
| Linux + any | ⚠️ untested | ⚠️ untested — GNOME Shell no longer shows trays by default |
| Any OS + ChatGPT Business/Enterprise | ⚠️ partial | ⚠️ partial | token-credit plans show credit balance instead of 5h/weekly bars |
| Any OS + Claude Team/Enterprise | ❌ likely unsupported | — | Anthropic may not emit the same headers for org plans |
| Any OS + Anthropic API-key (not subscription) | ❌ unsupported | — | API keys get different rate-limit headers |

If you try it on an untested combination, please open an issue with the output
of `python ai_fuelgauge.py --debug --json`.

## How it works

### Codex side — official-ish

Spawns `codex app-server` as a subprocess and sends a JSON-RPC request:

```json
{"jsonrpc":"2.0","method":"account/rateLimits/read","params":{}}
```

This method is defined in the Codex CLI's own protocol schema (generated via
`codex app-server generate-json-schema`). The `app-server` command is marked
`[experimental]`, so it may change — but because it's maintained by the Codex
team alongside the CLI, it's more stable than reverse-engineering HTTP endpoints.

### Claude side — undocumented but stable

Makes one minimal `/v1/messages` API call (`claude-haiku-4-5`, `max_tokens=1`,
content `"hi"`) using the OAuth access token stored by Claude Code, and reads
the following response headers:

- `anthropic-ratelimit-unified-5h-utilization`
- `anthropic-ratelimit-unified-5h-reset`
- `anthropic-ratelimit-unified-7d-utilization`
- `anthropic-ratelimit-unified-7d-reset`

These headers are **not in Anthropic's public API docs**, but Claude Code itself
depends on them to display quota, so they're stable in practice.

### Credential resolution order (Claude)

1. `$CLAUDE_CODE_OAUTH_TOKEN` environment variable (direct token)
2. `$CLAUDE_CONFIG_DIR/.credentials.json`
3. `~/.claude/.credentials.json`
4. macOS Keychain service `Claude Code-credentials`

### Cache

A 30-second cache (`~/.cache/usage-quota.json`) prevents repeated `usage` calls
from hammering the APIs. Use `--no-cache` to force a fresh fetch.

## Known limitations

- Each Claude probe consumes **~1 token** from your 5-hour budget. The 30 s
  cache keeps this negligible (&lt; 50 tokens/hour even under heavy polling).
- The Codex `app-server` protocol is experimental; a future Codex release
  could rename the method.
- The Anthropic unified headers are undocumented; a future API change could
  break the Claude side silently.
- Linux tray support depends on your desktop environment. Upstream GNOME no
  longer shows system-tray icons; try an extension like *AppIndicator Support*.

## Requirements

- Python **3.7+**
- OpenAI **Codex CLI** installed and logged in (`codex login`) — for Codex quota
- **Claude Code** CLI installed and logged in (`claude` then `/login`) — for Claude quota
- For tray mode only: `pystray`, `Pillow`, and one of `winotify` (Windows) or `plyer`

## Contributing

- Issues and pull requests welcome, especially:
  - Reports from macOS or Linux (attach `python ai_fuelgauge.py --debug --json` output)
  - Business / Enterprise / Team plan output samples (to improve rendering)
  - Translations of the README

## License

MIT — see [LICENSE](LICENSE).

---

## 中文快速入門

**這是什麼**：一次看兩個 AI 訂閱的剩餘配額（ChatGPT / Claude），CLI 或系統匣。

**為什麼要用**：每次想知道今天還能跑多少，要開兩個瀏覽器分頁。這個工具一行指令看完。

**安裝**：
```bash
git clone https://github.com/k7631159/ai-fuelgauge.git
cd ai-fuelgauge
pip install --user -r requirements-tray.txt   # 系統匣模式才需要
```

**常用指令**：
```bash
python ai_fuelgauge.py          # 看一次
python ai_fuelgauge.py --tray   # 常駐系統匣
```

**前提**：你要已經用 `codex login` 登入 Codex CLI、用 `claude` 登入 Claude Code CLI，這個工具才抓得到認證。

**已知限制**：
- 只實測過 Windows + ChatGPT Plus + Claude Pro 組合，其他平台/方案歡迎回報
- 每次查 Claude 會消耗約 1 token（小，有 30 秒快取保護）
- 依賴的 API 都是非官方文件化的，未來有可能因為更新而壞掉
