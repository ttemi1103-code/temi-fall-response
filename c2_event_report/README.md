# C2 事件資訊整合與緊急通知

Temi 跌倒監測與應變系統的**事件回報**。整合 A 組（跌倒時間、位置）、B 組（語音結果）與 C1（肢體反應確認，見 `c1_gesture`）的結果，產生事件摘要通知緊急聯絡人，並依流程決定是否請 Temi 撥打。

> **目前進度：** 資料整合、緊急程度判斷、事件摘要、「已查看」確認、無語音直接撥打、未查看逾時撥打、沒接改打下一位都已完成，並用假 Temi 測試過。Email／Telegram 通知已寫好但**尚未設定帳號**；Temi 撥打是**模擬版本**。目前是獨立執行的 HTTP 版本，尚未整合進 `fall_response` 的應變流程。

## 系統流程

```text
語音互動 ─ 有語音回應 → C1 肢體反應確認 → 整理語音＋肢體資訊 → 傳送事件摘要給緊急聯絡人
         │                                                       ├ 時限內按「已查看」→ 聯絡人決定後續處理
         │                                                       └ 時限內沒查看       → Temi 主動撥打聯絡人
         └ 無語音回應 ──────────────────────────────────────────→ 直接請 Temi 撥打聯絡人
```

- 三份資料用同一個 `event_id` 串起來。無語音回應時不等肢體確認，立即處理；收到跌倒後 60 秒內資料沒到齊，就用現有資料處理。
- 摘要附「已查看」連結，聯絡人點了就不撥打，預設等 120 秒。
- Temi 撥打時，聯絡人沒接會依序改撥下一位；全部沒接或 Temi 無法撥出，會再傳一次通知。
- **撥打與否依流程決定**；緊急程度只寫在摘要上，讓家屬判斷輕重。
- 狀態由程式規則決定；B 組的 LLM 只整理語音內容，沒有時改用國語／台語關鍵字判斷。

## 緊急程度（寫在摘要上）

| 語音 | 肢體確認 | 緊急程度 |
|---|---|---|
| 無語音回應 | 不做 | 🚨 緊急（直接撥打） |
| 出現嚴重症狀（流血、胸口痛、喘不過、動不了等） | 不論 | 🚨 緊急 |
| 有回應 | 點頭（需要打電話） | 🚨 緊急 |
| 提到不適 | 手或腳有一項無法活動 | 🚨 緊急 |
| 不論 | 手、腳中有兩項以上無法活動 | 🚨 緊急 |
| 沒提到不適 | 手或腳有一項無法活動（例如只能舉一隻手） | ⚠️ 需關注 |
| 提到不適 | 手腳都能動 | ⚠️ 需關注 |
| 表示沒事 | 手腳都能動，搖頭 | ℹ️ 安全 |
| 回應不明確 | — | ⚠️ 需關注 |

跌倒超過 5 分鐘：需關注升為緊急、安全升為需關注。

## 事件摘要範例

```text
🚨【跌倒事件通知｜緊急】
時間：2026-11-10 23:15:43
位置：客廳沙發旁
語音回應：「我腳好痛，起不來」（國語）
語音整理：長者表示腳部疼痛、無法自行站起
肢體確認：手：雙手都能舉起；腳：腳無法活動；是否需要打電話：點頭（需要）
判斷依據：長者點頭，表示需要幫忙打電話；無法依指令動腳
建議：請立即確認狀況，必要時撥打 119。
事件編號：d0707cd4

👉 請點此確認已查看：http://127.0.0.1:8000/ack?event_id=d0707cd4
若 120 秒內未確認，Temi 將主動撥打給「女兒」。
```

## 環境需求

Python 3.9 以上，只用標準函式庫，不需安裝套件。

## 檔案說明

| 檔案 | 功能 |
|---|---|
| `event_reporter.py` | 主程式：接收資料、判斷緊急程度、產生摘要、通知聯絡人、等「已查看」、送撥打指令給 Temi |
| `mock_temi.py` | 假的 Temi：接收撥打指令，幾秒後回報接通或沒接 |
| `send_test_event.py` | 模擬 A 組、B 組、C1 傳送資料 |

## 執行方式

在 `c2_event_report` 資料夾執行。Windows 中文顯示亂碼時，先執行 `$env:PYTHONIOENCODING='utf-8'`。

| 想做的事 | 指令 |
|---|---|
| 跑內建測試情境（約 5 秒） | `python event_reporter.py demo` |
| 啟動主程式（port 8000） | `python event_reporter.py server` |
| 假 Temi，每通都接通（port 9000） | `python mock_temi.py` |
| 假 Temi：第一位沒接、第二位接通 | `python mock_temi.py first_no_answer` |
| 假 Temi：全部沒接 | `python mock_temi.py no_answer` |
| 送「有語音、點頭」情境資料 | `python send_test_event.py` |
| 送「有語音、說沒事」情境資料 | `python send_test_event.py safe` |
| 送「無語音回應」情境資料 | `python send_test_event.py silent` |

完整測試要開三個終端機：依序啟動主程式、假 Temi，再送測試資料。有語音的情境會印出「已查看」連結，時限內用瀏覽器打開就不會撥打。測試時可以先執行 `$env:ACK_TIMEOUT_SEC='15'` 縮短等待時間。

## 串接介面

用 HTTP POST 傳 JSON 到 `http://<C2電腦IP>:8000`，同一次跌倒要用同一個 `event_id`。

| 路徑 | 傳送者 | 內容範例 |
|---|---|---|
| `/fall` | A 組 | `{"event_id": "e001", "fall_time": "2026-11-08T14:32:10", "location": "客廳"}` |
| `/speech` | B 組 | `{"event_id": "e001", "responded": true, "text": "我腳好痛", "language": "zh-TW", "llm_intent": "need_help"}` |
| `/gesture` | C1 | `{"event_id": "e001", "arms": "both", "leg_moved": true, "call_consent": "nod"}` |
| `/call_status` | Temi | `{"event_id": "e001", "contact": "女兒", "status": "connected"}` |
| `/ack?event_id=e001` | 聯絡人（GET，點摘要裡的連結） | 表示已查看摘要 |
| `/event` | 任何人 | 三份資料合成一包：`{"fall": {...}, "speech": {...}, "gesture": {...}}` |

- `responded`：長者有沒有語音回應（`true`／`false`）。`false` 時直接撥打，不等肢體確認。
- `language`：`zh-TW`（國語）或 `nan-TW`（台語）。
- `llm_intent`：`need_help`、`ok` 或留空。
- `arms`、`leg_moved`、`call_consent`：見 `c1_gesture/README.md`。
- `status`：`connected`（接通）、`no_answer`（沒接）、`rejected`（拒接）。

C2 送給 Temi 的撥打指令：

```json
{"action": "call_contact", "contact": "女兒", "event_id": "e001"}
```

## 設定

在 `event_reporter.py` 的 `CONFIG` 修改，或用環境變數設定。

| 設定 | 說明 |
|---|---|
| `ACK_TIMEOUT_SEC` | 傳送摘要後等「已查看」的秒數，超過就請 Temi 撥打（預設 120） |
| `PUBLIC_URL` | 「已查看」連結的網址開頭；聯絡人手機要連得到，請改成 C2 電腦的區網 IP，例如 `http://192.168.1.10:8000` |
| `TEMI_URL` | Temi 接收撥打指令的網址，預設是假 Temi |
| `TEMI_CONTACTS` | 撥打的聯絡人，用逗號分隔，依序撥打（預設 `女兒,兒子`） |
| `CONTACT_EMAIL`、`SMTP_USER`、`SMTP_PASSWORD` | Email 通知；Gmail 要用「應用程式密碼」 |
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | Telegram 通知 |
| `WAIT_TIMEOUT_SEC` | 等待 B 組、C1 資料的秒數（預設 60） |
| `LONG_LIE_MINUTES` | 跌倒超過幾分鐘升級緊急程度（預設 5） |

帳號密碼請用環境變數設定，不要寫進程式上傳。

## 功能狀態

| 項目 | 狀態 |
|---|---|
| 接收 A 組、B 組、C1 資料 | ✅ 完成 |
| 緊急程度判斷與中文摘要 | ✅ 完成 |
| 無語音回應直接撥打 | ✅ 完成，用假 Temi 測過 |
| 「已查看」連結與逾時撥打 | ✅ 完成；⚠️ 聯絡人手機須和 C2 在同一網路才點得到 |
| 沒接改打下一位、全部沒接再次通知 | ✅ 完成，用假 Temi 測過 |
| Email／Telegram 通知 | ⚠️ 程式已寫，尚未設定帳號實測 |
| Temi 實際撥打、聯絡人遠端連線 Temi | ❌ 未完成，待確認用 RIM2 或 Temi Java App |
| 與 A 組、B 組、Temi 實際串接 | ❌ 未完成，目前用模擬資料 |
| 整合進 `fall_response` 的 `Notifier` 介面 | ❌ 未完成，待與組員討論 |

## 待討論

- **Temi 怎麼撥打：** 透過 RIM2 API／eRoute，或在 Temi 上寫 Java App 接收指令。確定後修改 `send_to_temi()`。
- **「已查看」的方式：** 目前是區網連結，聯絡人不在家時點不到。可改成 Telegram 按鈕、LINE 或雲端網址。
- **與主程式整合：** `fall_response/incident.py` 已有事件摘要和 `Notifier` 介面（含 `wait_for_ack()`），功能和本模組重疊；之後可把通知、已查看與撥打改寫成 `Notifier` 的實作。
- **event_id 由誰產生：** 建議由 A 組偵測到跌倒時產生，再傳給 B 組與 C1。
