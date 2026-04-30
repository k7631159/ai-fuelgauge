# ai-fuelgauge

> 瞄一眼你的 AI 訂閱油表 —— 把 OpenAI Codex 和 Anthropic Claude 的剩餘配額並排放在一起。

[English](./README.md) · **繁體中文**

## 重要聲明 —— 使用前請先看

這個 repo 是一個**個人用途**的 AI 訂閱用量查看工具原始碼，公開是為了透明度和技術參考，
並不是作為產品發布。它依賴**消費者 OAuth 行為和未公開的 API endpoint**，隨時可能
改變、失效，或不在服務供應商支援範圍內。這不是 OpenAI 或 Anthropic 任何一方官方、
受支援、或被推薦的 client。Anthropic 的文件說明消費者 OAuth 是保留給自家原生
application 使用的，這個工具顯然不在那條被明確祝福的路上。如果你選擇執行或修改它，
請自行承擔風險，並檢視程式碼、API 行為，以及適用於你自己帳號的服務條款。

本專案刻意**不發布到 PyPI** —— 只能從這個 repo `git clone`。

## 為什麼有這個工具

我同時付費訂閱 ChatGPT（用來跑 Codex）和 Claude（用來跑 Claude Code）。要查各自
剩餘多少配額，我得在不同介面之間切換 —— ChatGPT 和 Claude 的桌面 app、網頁儀表板、
還有 Claude Code 的 `/usage` 指令。我想要一眼就看完。這是我自己寫來用的工具，
原始碼放出來是想說搞不好可以當參考。

一個小小的跨平台 Python CLI，一次顯示兩個訂閱的 5 小時和每週 rate-limit window
你已經用掉多少。另外有選用的系統匣（tray）模式，可以在超過門檻時跳通知。

## 範例輸出

```
Codex
  5h         29%  [========>                     ]  reset 3h03m
  week       25%  [=======>                      ]  reset 5d17h

Claude
  5h         29%  [========>                     ]  reset 1h35m
  week       15%  [===>                          ]  reset 15h35m
```

顏色：綠色 &lt; 70 %、橘色 70–89 %、紅色 ≥ 90 %。

## 從原始碼執行

```bash
git clone https://github.com/k7631159/ai-fuelgauge.git
cd ai-fuelgauge
```

其他都是標準函式庫 —— CLI 不需要安裝任何東西。
如果要用 tray 模式，再裝額外套件：

```bash
pip install --user -r requirements-tray.txt
```

刻意不做 PyPI 套件（見「重要聲明」）。`pip install git+https://...` 技術上可行，
但不建議一般使用。

## 使用方式

### CLI（看一眼就走）

```bash
python ai_fuelgauge.py                 # 純文字輸出
python ai_fuelgauge.py --json          # 機器可讀的 JSON（純原始 probe
                                       # 結果，stale bar 不會合併進來）
python ai_fuelgauge.py --no-cache      # 略過結果 cache（auth 安全檢查仍可能
                                       # 跳過網路請求）
python ai_fuelgauge.py --debug         # 把原始 API 回應全部 dump 出來
```

`--json` 輸出的是 raw probe 狀態。Claude token 過期時人類介面（CLI/tray）
會額外用 last-known-good 顯示 stale bar，但這些**不會**合併進 JSON payload
—— 程式化消費者拿到的是同樣的 probe error 欄位；如果要 stale 快照，自行讀
`~/.cache/usage-quota-last-claude.json`。

包一個 shell alias（Windows 用 `usage.cmd` / Linux 用 `usage` symlink），
這樣只要打 `usage` 就好。

### Tray app（常駐可見）

```bash
python ai_fuelgauge.py --tray          # 一直跑，直到你右鍵 → Quit
python ai_fuelgauge.py --tray --interval 600   # 每 10 分鐘 poll 一次
```

Tray 圖示會顯示一個彩色圓點，反映你兩個訂閱中使用率最高的那一邊。右鍵可以看
詳細選單和 **Refresh now** 選項。當以下情況發生時會跳桌面通知：

- 5 小時 window 達到 80 %
- 每週 window 達到 90 %

Claude 認證失敗時 tray 會顯示不同 label —— `auth`（refresh 失敗，請跑 `claude`）、
`expired`（proactive 主動跳過，請跑 `claude`）、`envtok`（請更換
`$CLAUDE_CODE_OAUTH_TOKEN`）—— 並在右鍵選單給你具體該怎麼修，不用翻 log。
Token 過期但 24 小時內有成功 probe 過時，tray 會把那次的數值標成 stale 繼續
顯示；window 已 reset 過的 bar 會明確標出已 omit，並另外加一行選單給回復動作。

通知在 Windows 用 `winotify`，其他平台用 `plyer`。

## 支援情況（v0.1.0-preview）

| 環境 | CLI | Tray | 備註 |
|---|---|---|---|
| Windows 10/11 + ChatGPT Plus + Claude Max | ✅ tested | ✅ tested | 作者自己每天在用 |
| Windows + Pro / Max | ✅ expected to work | ✅ expected to work | 同樣的 endpoint 和 JSON-RPC |
| macOS + Plus/Pro/Max | ⚠️ untested | ⚠️ untested | macOS Keychain 那條路徑程式碼有寫但沒驗證 —— 歡迎回報 |
| Linux + any | ⚠️ untested | ⚠️ untested | GNOME Shell 預設已經不顯示 tray 了 |
| Any OS + ChatGPT Business/Enterprise | ⚠️ partial | ⚠️ partial | 代幣計費方案會顯示 credit 餘額，而不是 5h/每週進度條 |
| Any OS + Claude Team/Enterprise | ❌ likely unsupported | — | OAuth usage endpoint 對組織方案可能行為不同 |
| Any OS + Anthropic API-key (not subscription) | ❌ unsupported | — | API key 不走消費者 OAuth —— 需要不同的流程 |

如果你在沒測過的組合上試了，麻煩開一個 issue，附上 `python ai_fuelgauge.py --debug --json`
的輸出。**張貼到公開 issue 前**：`--debug` 會 dump 出原始 OAuth usage endpoint
回應，裡面可能包含帳號層級的欄位 —— 不想公開的欄位請先移除。

## 運作原理

### Codex 這邊 —— 半官方路線

把 `codex app-server` 當成 subprocess 生出來，然後送一個 JSON-RPC request：

```json
{"jsonrpc":"2.0","method":"account/rateLimits/read","params":{}}
```

這個 method 定義在 Codex CLI 自己的協定 schema 裡（可以用
`codex app-server generate-json-schema` 重新產生）。`app-server` 指令被標註為
`[experimental]`，所以有可能會變 —— 但因為它是 Codex 團隊和 CLI 一起維護的，
比起去逆向他們的 HTTP endpoint，這條路相對穩定。

### Claude 這邊 —— 未公開的 OAuth endpoint

呼叫前先讀取 Claude Code 的 OAuth credentials，在本地檢查 `expiresAt`。
如果 token 還健康，就對 `GET https://api.anthropic.com/api/oauth/usage` 打一次，
搭配 header `anthropic-beta: oauth-2025-04-20`；endpoint 會回一個結構化 JSON，
包含 `five_hour` 和 `seven_day` 兩個區塊（使用率百分比 + reset 時間戳），
工具再把它對應到 5h / 每週的進度條上。

如果 token 已過期、或距離過期不到 60 秒，工具會先請本機的 `claude` CLI
（`claude auth status`）幫忙 refresh。如果 refresh 後仍然拿不到一個有效的新 token，
**就完全不會發出網路請求**，直接顯示本地的認證錯誤 —— 這是為了避免帶著已知壞掉
的 bearer 去打 upstream，造成共用流量被牽連。本地錯誤會把最近一次成功 probe
的使用率/reset 欄位（如果有的話）當成 stale bar 顯示，上限 24 小時；schema 不符、
時鐘漂移、或 window 已 roll-over 的情況會退回純文字「expired，請跑 `claude`」提示。

這個 endpoint **沒有出現在 Anthropic 的公開 API 文件**，是保留給 Anthropic
自家原生 application 的。本工具只為了**個人使用**去呼叫它；這不是官方認可的
整合路徑，隨時可能失效。Probe 不會消耗任何 API token —— OAuth usage endpoint
不會從 model rate-limit 預算裡扣。

### 認證讀取順序（Claude）

1. `$CLAUDE_CODE_OAUTH_TOKEN` 環境變數（直接給 token）
2. `$CLAUDE_CONFIG_DIR/.credentials.json`
3. `~/.claude/.credentials.json`
4. macOS Keychain service `Claude Code-credentials`

### Cache

`~/.cache/` 底下有兩個 cache：

- `usage-quota.json` —— 30 秒的結果 cache，避免連續打 `usage` 時每次都要重新經歷
  Codex `app-server` 的冷啟動（每次 spawn 大約 4 秒），同時也算是對 OAuth usage
  endpoint 的一點禮貌。`--no-cache` 用來略過這層；不過如果 Claude 的本地 token
  過期且無法 refresh，auth 安全檢查仍可能跳過網路請求。
- `usage-quota-last-claude.json` —— 最近一次成功 Claude probe 的快照，最久保留
  24 小時，只在 token 剛過期時用來把 stale bar 顯示出來，避免介面只剩一個
  「expired」字樣。`--no-cache` **不會**略過這層；下一次成功 probe 會直接覆蓋。

## 已知限制

- Anthropic 的 OAuth usage endpoint **未公開**，是保留給 Anthropic 自家原生
  app 用的；未來一個改動就可能讓 Claude 這邊默默壞掉。
- Codex `app-server` 協定是 **experimental**；未來 Codex 版本可能會把這個
  method 改名。
- Linux 的 tray 支援要看你用哪個桌面環境。上游 GNOME 已經不顯示 system tray
  圖示了；可以試試 *AppIndicator Support* 之類的擴充套件。
- `$CLAUDE_CODE_OAUTH_TOKEN` 被視為靜態 token，**無法自動 refresh**；過期時請手動
  輪替（或 unset 它，讓工具回去讀 credentials 檔案）。
- Claude 的自動 refresh 需要 `claude` CLI 在 `PATH` 上、且能改寫 Claude Code
  credentials。沒有的話，過期 token 會以 401 + 「請跑 `claude`」提示呈現，不會被
  自動修復。
- Proactive 過期檢查只在 Claude credentials 帶有可解析的 `expiresAt` 時才會運作。
  其他來源（環境變數、macOS Keychain 純 token 字串）會 fallback 到反應式 401
  處理流程。

## 環境需求

- Python **3.7+**
- 已安裝並登入 OpenAI **Codex CLI**（`codex login`）—— 用來抓 Codex 配額
- 已安裝、已登入、且在 `PATH` 上的 **Claude Code** CLI（`claude` 然後 `/login`）
  —— 抓 Claude 配額**和**自動 refresh 過期 token 都需要
- 如果你用 `$CLAUDE_CODE_OAUTH_TOKEN` 而不是 credentials 檔案，過期時請自行輪替；
  本工具無法 refresh 環境變數型 token
- 只有 tray 模式才需要：`pystray`、`Pillow`，再加上 `winotify`（Windows）或 `plyer` 其中一個

## 貢獻

這是個人興趣專案，不是受維護的產品。如果你開 issue 或 PR，我會看，但沒辦法
保證回應或合併會很快。最有幫助的回饋：

- 從 macOS 或 Linux 來的回報（附上 `python ai_fuelgauge.py --debug --json`
  的輸出 —— **張貼前請移除帳號 / 組織相關欄位**）
- Business / Enterprise / Team 方案的輸出樣本（用來改善顯示）

## 授權

MIT —— 請見 [LICENSE](LICENSE)。
