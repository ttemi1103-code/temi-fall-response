# C1 肢體反應確認 ＋ C2 事件資訊整合與緊急通知

Temi 跌倒監測與應變系統的**肢體確認（C1）**與**事件回報（C2）**。C1 在長者有語音回應後提出肢體指令、用鏡頭辨識回應；C2 整合 A 組（跌倒時間、位置）、B 組（語音結果）與 C1 的結果，產生事件摘要通知緊急聯絡人，依流程決定是否請 Temi 撥打。

> **目前進度：** C1 提問與舉雙手／動腳／點頭／搖頭辨識、C2 資料整合、摘要、「已查看」確認、Temi 撥打指令都已完成，並用假資料與假 Temi 測試過。C1 **尚未用實際鏡頭充分測試**，靈敏度可能要調整；Email／Telegram 通知已寫好但**尚未設定帳號**；Temi 撥打是**模擬版本**。目前是獨立執行的 HTTP 版本，尚未整合進 `fall_response` 的應變流程。

## 系統流程

```text
語音互動 ─ 有語音回應 → 肢體反應確認（C1）→ 整理語音＋肢體資訊 → 傳送事件摘要給緊急聯絡人
         │                                                          ├ 時限內按「已查看」→ 聯絡人決定後續處理
         │                                                          └ 時限內沒查看       → Temi 主動撥打聯絡人
         └ 無語音回應 ─────────────────────────────────────────────→ 直接請 Temi 撥打聯絡人
```

- 三份資料用同一個 `event_id` 串起來；無語音回應時不等肢體確認，立即處理。收到跌倒後 60 秒內資料沒到齊，就用現有資料處理。
- 摘要附「已查看」連結，聯絡人點了就不撥打；預設等 120 秒。
- Temi 撥打時，聯絡人沒接會依序改撥下一位；全部沒接或 Temi 無法撥出，會再傳一次通知。
- 撥打與否依流程決定；緊急程度只寫在摘要上，讓家屬判斷輕重。
- 狀態由程式規則決定；B 組的 LLM 只整理語音內容，沒有時改用國語／台語關鍵字判斷。

## 肢體反應確認（C1）

只有在**有語音回應**時進行。長者能說話，所以重點是確認身體能不能動，以及要不要協助：

| 順序 | Temi 提問 | 辨識 | 結果 |
|---|---|---|---|
| 1 | 「請把兩隻手都舉起來。」 | 左右手各自有沒有舉起 | 雙手／只有左手／只有右手／沒有舉手 |
| 2 | 「請動一動你的腳。」 | 腳踝相對臀部有沒有移動 | 有動／沒動 |
| 3 | 「需要幫你打電話給家人嗎？需要請點頭，不需要請搖頭。」 | 點頭／搖頭 | 需要／不需要／沒反應 |

- 每題最多看 10 秒，辨識到完整動作就提早進下一題；每題只算該題相關的動作。
- 用身體與頭部自己的方向判斷，坐著、躺著也能辨識；靈敏度在 `gesture_recognizer.py` 的 `PARAMS` 調整。
- 電腦測試時用 Windows 內建中文語音唸問題；接上 Temi 後改由 Temi 唸。

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

## 環境需求

- C2（`event_reporter.py`、`mock_temi.py`、`send_test_event.py`）：Python 3.9 以上，只用標準函式庫。
- C1（`gesture_recognizer.py`）：開鏡頭時需要 `python -m pip install opencv-python mediapipe`；第一次執行會自動下載姿勢模型到 `models/`。`demo` 模式不需安裝套件。

## 檔案說明

| 檔案 | 功能 |
|---|---|
| `gesture_recognizer.py` | C1：依序提出三個肢體指令，用鏡頭辨識舉雙手／動腳／點頭／搖頭，結果送給 C2 |
| `event_reporter.py` | C2：接收資料、判斷緊急程度、產生摘要、通知聯絡人、等「已查看」、送撥打指令給 Temi |
| `mock_temi.py` | 假的 Temi：接收撥打指令，幾秒後回報接通或沒接 |
| `send_test_event.py` | 模擬 A 組、B 組、C1 傳送資料 |

## 執行方式

在 `event_report` 資料夾執行。Windows 中文顯示亂碼時，先執行 `$env:PYTHONIOENCODING='utf-8'`。

| 想做的事 | 指令 |
|---|---|
| C1：測試辨識邏輯（不開鏡頭） | `python gesture_recognizer.py demo` |
| C1：開鏡頭看辨識效果 | `python gesture_recognizer.py camera` |
| C1：三題依序問完 | `python gesture_recognizer.py ask` |
| C1：只測一題 | `python gesture_recognizer.py ask --purpose legs`（`arms`／`legs`／`call_consent`） |
| C1：三題問完，結果送給 C2 | `python gesture_recognizer.py ask --event-id e001` |
| C1：等待 `/start` 指令再提問（port 8001） | `python gesture_recognizer.py server` |
| C2：跑內建測試情境（約 5 秒） | `python event_reporter.py demo` |
| C2：啟動主程式（port 8000） | `python event_reporter.py server` |
| 假 Temi，每通都接通（port 9000） | `python mock_temi.py` |
| 假 Temi：第一位沒接、第二位接通 | `python mock_temi.py first_no_answer` |
| 送「有語音、點頭」情境資料 | `python send_test_event.py` |
| 送「有語音、說沒事」情境資料 | `python send_test_event.py safe` |
| 送「無語音回應」情境資料 | `python send_test_event.py silent` |

完整測試要開三個終端機：依序啟動 C2 主程式、假 Temi，再送測試資料。有語音的情境會印出「已查看」連結，時限內用瀏覽器打開就不會撥打。測試時可以先執行 `$env:ACK_TIMEOUT_SEC='15'` 縮短等待時間。

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

- `responded`：長者有沒有語音回應（`true`／`false`）。`false` 時 C2 直接撥打，不等肢體確認。
- `language`：`zh-TW`（國語）或 `nan-TW`（台語）。
- `llm_intent`：`need_help`、`ok` 或留空。
- `arms`：`both`、`left`、`right`、`none`。`leg_moved`：`true`／`false`。`call_consent`：`nod`、`shake_head` 或 `""`。
- `status`：`connected`（接通）、`no_answer`（沒接）、`rejected`（拒接）。

C1 server 模式的啟動指令（B 組或 Temi 在語音確認「有回應」後呼叫）：

```text
POST http://<C1電腦IP>:8001/start  {"event_id": "e001", "seconds": 10}
```

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
| C1 提問（電腦語音）＋舉雙手／動腳／點頭／搖頭辨識 | ✅ 邏輯用假資料測過（22 項）；⚠️ 實際鏡頭需測試、調整靈敏度 |
| C2 接收 A 組、B 組、C1 資料 | ✅ 完成 |
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
- **C1 與 `fall_response/gesture_recognizer.py`：** 主程式已有一份肢體辨識模組，之後需討論要保留哪一份。
- **event_id 由誰產生：** 建議由 A 組偵測到跌倒時產生，再傳給 B 組與 C1。
