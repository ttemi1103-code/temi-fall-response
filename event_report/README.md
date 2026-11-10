# C2 事件資訊整合與緊急通知系統

Temi 跌倒監測與應變系統的**第三階段：事件回報**。整合 A 組（跌倒時間、位置）、B 組（語音結果）、C1（肢體辨識結果），判斷長者狀況並產生事件摘要，通知緊急聯絡人，必要時請 Temi 撥視訊給家屬。

> **目前進度：** 接收資料、狀態判斷、事件摘要、撥號指令與「沒接通改打下一位」都已完成，並用假 Temi 測試過。Email／Telegram 通知已寫好但**尚未設定帳號**；Temi 撥視訊是**模擬版本**，還沒接上真正的 Temi。目前是獨立執行的 HTTP 版本，尚未整合進 `fall_response` 的應變流程。

## 系統流程

```text
A 組（跌倒時間、位置） ─┐
B 組（語音結果）       ─┼→ 接收資料 → 判斷狀態 → 產生摘要 → 通知聯絡人 ─ 緊急 → Temi 撥視訊
C1（肢體辨識結果）     ─┘                                                    │
                                                                    ├ 接通 → 結束
                                                                    ├ 沒接 → 改打下一位聯絡人
                                                                    └ 全部沒接或 Temi 無法撥出 → 再次通知
```

- 三份資料用同一個 `event_id` 串起來；收到跌倒後 60 秒內沒到齊，就用現有資料判斷。
- 先送文字摘要，再請 Temi 撥視訊，讓家屬接電話前就知道狀況。
- 狀態由程式規則決定；B 組的 LLM 只提供語音意圖，沒有時改用國語／台語關鍵字判斷。

## 狀態判斷

| 情況 | 結果 |
|---|---|
| 語音出現嚴重症狀（流血、胸口痛、喘不過、動不了等） | 🚨 緊急 → Temi 撥視訊 |
| 沒有語音也沒有肢體回應 | 🚨 緊急 → Temi 撥視訊 |
| 語音和肢體都表示需要幫忙 | 🚨 緊急 → Temi 撥視訊 |
| 只有一項表示需要幫忙 | ⚠️ 需關注 |
| 說沒事、搖頭 | ℹ️ 安全 |
| 跌倒超過 5 分鐘 | 狀態升一級 |

## 環境需求

Python 3.9 以上，只用標準函式庫，不需安裝套件。

## 檔案說明

| 檔案 | 功能 |
|---|---|
| `event_reporter.py` | 主程式：接收資料、判斷狀態、產生摘要、通知聯絡人、送撥號指令給 Temi |
| `mock_temi.py` | 假的 Temi：接收撥號指令，幾秒後回報接通或沒接 |
| `send_test_event.py` | 模擬 A 組、B 組、C1 傳送資料 |

## 執行方式

在 `event_report` 資料夾執行。Windows 中文顯示亂碼時，先執行 `$env:PYTHONIOENCODING='utf-8'`。

| 想做的事 | 指令 |
|---|---|
| 跑內建的四個測試情境 | `python event_reporter.py demo` |
| 啟動主程式（port 8000） | `python event_reporter.py server` |
| 啟動假 Temi，每通都接通（port 9000） | `python mock_temi.py` |
| 假 Temi：第一位沒接、第二位接通 | `python mock_temi.py first_no_answer` |
| 假 Temi：全部沒接 | `python mock_temi.py no_answer` |
| 送緊急情境資料 | `python send_test_event.py` |
| 送安全情境資料 | `python send_test_event.py safe` |

完整測試要開三個終端機：依序啟動主程式、假 Temi，再送測試資料。

## 串接介面

用 HTTP POST 傳 JSON 到 `http://<C2電腦IP>:8000`，同一次跌倒要用同一個 `event_id`。

| 路徑 | 傳送者 | 內容範例 |
|---|---|---|
| `/fall` | A 組 | `{"event_id": "e001", "fall_time": "2026-11-08T14:32:10", "location": "客廳"}` |
| `/speech` | B 組 | `{"event_id": "e001", "text": "我腳好痛", "language": "zh-TW", "llm_intent": "need_help"}` |
| `/gesture` | C1 | `{"event_id": "e001", "gestures": ["raise_hand"]}` |
| `/call_status` | Temi | `{"event_id": "e001", "contact": "女兒", "status": "connected"}` |
| `/event` | 任何人 | 三份資料合成一包：`{"fall": {...}, "speech": {...}, "gesture": {...}}` |

- `language`：`zh-TW`（國語）或 `nan-TW`（台語）。
- `llm_intent`：`need_help`、`ok` 或留空。
- `gestures`：`raise_hand`（舉手）、`nod`（點頭）、`shake_head`（搖頭）。
- `status`：`connected`（接通）、`no_answer`（沒接）、`rejected`（拒接）。

C2 送給 Temi 的撥號指令：

```json
{"action": "call_contact", "contact": "女兒", "event_id": "e001"}
```

## 設定

在 `event_reporter.py` 的 `CONFIG` 修改，或用環境變數設定。

| 設定 | 說明 |
|---|---|
| `TEMI_URL` | Temi 接收撥號指令的網址，預設是假 Temi |
| `TEMI_CONTACTS` | 撥打的聯絡人，用逗號分隔，依序撥打（預設 `女兒,兒子`） |
| `CONTACT_EMAIL`、`SMTP_USER`、`SMTP_PASSWORD` | Email 通知；Gmail 要用「應用程式密碼」 |
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | Telegram 通知 |
| `WAIT_TIMEOUT_SEC` | 等待 B 組、C1 資料的秒數（預設 60） |
| `LONG_LIE_MINUTES` | 跌倒超過幾分鐘升級狀態（預設 5） |

帳號密碼請用環境變數設定，不要寫進程式上傳。

## 功能狀態

| 項目 | 狀態 |
|---|---|
| 接收 A 組、B 組、C1 資料 | ✅ 完成 |
| 狀態判斷（緊急／需關注／安全） | ✅ 完成，四個情境測過 |
| 中文事件摘要 | ✅ 完成 |
| 撥號指令、沒接改打下一位、全部沒接再次通知 | ✅ 完成，用假 Temi 測過 |
| Email／Telegram 通知 | ⚠️ 程式已寫，尚未設定帳號實測 |
| Temi 實際撥視訊 | ❌ 未完成，待確認用 RIM2 或 Temi Java App |
| 與 A 組、B 組、C1 實際串接 | ❌ 未完成，目前用模擬資料 |
| 整合進 `fall_response` 的 `Notifier` 介面 | ❌ 未完成，待與組員討論 |

## 待討論

- **Temi 怎麼撥視訊：** 透過 RIM2 API／eRoute，或在 Temi 上寫 Java App 接收指令。確定後修改 `send_to_temi()`。
- **與主程式整合：** `fall_response/incident.py` 已有事件摘要和 `Notifier` 介面，功能和本模組重疊；之後可把通知與撥號改寫成 `Notifier` 的實作，不另外啟動伺服器。
- **event_id 由誰產生：** 建議由 A 組偵測到跌倒時產生，再傳給 B 組與 C1。
