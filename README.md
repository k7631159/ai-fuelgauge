# ai-fuelgauge

> Peek at your AI subscription fuel gauge — remaining quota for OpenAI Codex + Anthropic Claude, side by side.

## Disclaimer — please read before using

This repository is source code for a **personal** AI-subscription quota viewer,
published for transparency and technical reference rather than as a product. It
relies on **consumer OAuth behaviour and undocumented API endpoints** that may
change, break, or fall outside provider-supported usage at any time. It is not an
official, supported, or recommended client for OpenAI or Anthropic. Anthropic's
documentation states that consumer OAuth is intended for native Anthropic
applications; this tool sits outside that clearly-blessed path. If you choose to
run or adapt it, you do so at your own risk and should review the code, the API
behaviour, and the Terms of Service that apply to your own account.

The project is intentionally **not published on PyPI** — `git clone` only, from
this repository.

## Why it exists

I pay for both a ChatGPT subscription (for Codex) and a Claude subscription
(for Claude Code). To check remaining quota, I was bouncing between different
interfaces — ChatGPT and Claude desktop apps, web dashboards, and Claude Code's
`/usage` command. I wanted one glance instead. This is the tool I built for
myself; the source is public in case it's useful as a reference.

A small cross-platform Python CLI that shows how much of your 5-hour and
weekly rate-limit windows you've already burned through, for both
subscriptions at once. Optional system-tray mode with threshold notifications.

## Example output

```
Codex
  5h         29%  [========>                     ]  reset 3h03m
  week       25%  [=======>                      ]  reset 5d17h

Claude
  5h         29%  [========>                     ]  reset 1h35m
  week       15%  [===>                          ]  reset 15h35m
```

Colors: green &lt; 70 %, orange 70–89 %, red ≥ 90 %.

## Run from source

```bash
git clone https://github.com/k7631159/ai-fuelgauge.git
cd ai-fuelgauge
```

Everything else is standard library — no install needed for the CLI.
For tray mode, install extra deps:

```bash
pip install --user -r requirements-tray.txt
```

There is no PyPI package on purpose (see Disclaimer). `pip install git+https://...`
is technically possible but not recommended for casual use.

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
| Windows 10/11 + ChatGPT Plus + Claude Max | ✅ tested | ✅ tested | author's daily driver |
| Windows + Pro / Max | ✅ expected to work | ✅ expected to work | same endpoints & JSON-RPC |
| macOS + Plus/Pro/Max | ⚠️ untested | ⚠️ untested | macOS Keychain path is coded but not verified — feedback wanted |
| Linux + any | ⚠️ untested | ⚠️ untested | GNOME Shell no longer shows trays by default |
| Any OS + ChatGPT Business/Enterprise | ⚠️ partial | ⚠️ partial | token-credit plans show credit balance instead of 5h/weekly bars |
| Any OS + Claude Team/Enterprise | ❌ likely unsupported | — | OAuth usage endpoint may behave differently for org plans |
| Any OS + Anthropic API-key (not subscription) | ❌ unsupported | — | API keys don't use consumer OAuth — different flow needed |

If you try it on an untested combination, please open an issue with the output
of `python ai_fuelgauge.py --debug --json`. **Before posting**: `--debug` dumps
the raw OAuth usage-endpoint response, which can include account-level fields —
redact anything you don't want in a public issue.

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

### Claude side — undocumented OAuth endpoint

Makes a single `GET https://api.anthropic.com/api/oauth/usage` using the OAuth
access token that Claude Code stores at `~/.claude/.credentials.json`, with
the header `anthropic-beta: oauth-2025-04-20`. The endpoint returns structured
JSON with `five_hour` and `seven_day` blocks (utilization percent +
reset timestamps), which the tool maps onto the 5h / weekly bars.

The endpoint is **not documented in Anthropic's public API docs** and is
reserved for native Anthropic applications. This tool consumes it for
**personal use only**; it is not a sanctioned integration path and could stop
working at any time. No API tokens are consumed by the probe — the OAuth
endpoint is not billed against the model rate-limit budget.

### Credential resolution order (Claude)

1. `$CLAUDE_CODE_OAUTH_TOKEN` environment variable (direct token)
2. `$CLAUDE_CONFIG_DIR/.credentials.json`
3. `~/.claude/.credentials.json`
4. macOS Keychain service `Claude Code-credentials`

### Cache

A 30-second cache (`~/.cache/usage-quota.json`) skips re-running the Codex
`app-server` cold-start (~4 s per spawn) on back-to-back `usage` calls, and is
a courtesy to the OAuth usage endpoint. Use `--no-cache` to force a fresh fetch.

## Known limitations

- The Anthropic OAuth usage endpoint is **undocumented** and reserved for
  native Anthropic apps; a future change could break the Claude side silently.
- The Codex `app-server` protocol is **experimental**; a future Codex release
  could rename the method.
- Linux tray support depends on your desktop environment. Upstream GNOME no
  longer shows system-tray icons; try an extension like *AppIndicator Support*.

## Requirements

- Python **3.7+**
- OpenAI **Codex CLI** installed and logged in (`codex login`) — for Codex quota
- **Claude Code** CLI installed and logged in (`claude` then `/login`) — for Claude quota
- For tray mode only: `pystray`, `Pillow`, and one of `winotify` (Windows) or `plyer`

## Contributing

This is a personal hobby project, not a maintained product. If you open an issue
or PR, I'll take a look, but I can't promise fast replies or merges. The most
useful input:

- Reports from macOS or Linux (attach `python ai_fuelgauge.py --debug --json`
  output — **redact account / org fields before posting**)
- Samples from Business / Enterprise / Team plan output (to improve rendering)

## License

MIT — see [LICENSE](LICENSE).

---

## 中文快速入門

**重要聲明**（請先看）：

本 repo 是一個**個人用途**的 AI 訂閱用量查看工具原始碼，公開是為了透明度和技術參考，
並非作為產品發布。它使用**消費者 OAuth 行為和未公開的 API 端點**，隨時可能失效、改變、
或與 OpenAI / Anthropic 的使用條款產生衝突。這不是任何一方官方、受支援、或被推薦的
客戶端。若你選擇使用或修改此程式碼，請自行評估風險，並檢視自身帳號所適用的服務條款。
**本專案刻意不發布到 PyPI**，只能從此 repo `git clone`。

**這是什麼**：一次看兩個 AI 訂閱的剩餘配額（ChatGPT / Claude），CLI 或系統匣。

**為什麼會做**：我同時付費訂閱 ChatGPT（用 Codex）和 Claude（用 Claude Code），
要查各自的剩餘用量得在多個介面之間切換 —— 兩家的桌面 App、網頁儀表板、Claude Code CLI 的
`/usage` 指令。我想要一個指令就全部看完。這是我自己寫來用的工具，原始碼放在這裡供參考。

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

**前提**：你要已經用 `codex login` 登入 Codex CLI、用 `claude` 登入 Claude Code CLI，
這個工具才抓得到認證。

**已知限制**：
- 只實測過 Windows + ChatGPT Plus + Claude Max 組合，其他平台/方案歡迎回報
- Claude 用的 `/api/oauth/usage` 端點**未公開**（Anthropic 保留給自家原生 app 使用），
  未來可能無預警失效
- Codex `app-server` 協定標註為 **experimental**，未來可能改名
