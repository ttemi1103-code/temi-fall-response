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

## 肢體確認（C1）

語音確認結束後，C1 依語音結果提出不同的問題，再用鏡頭辨識長者的動作：

```text
語音有回應 →「需要幫你打電話給家人嗎？需要請點頭，不需要請搖頭」→ 只看點頭／搖頭
語音沒回應 →「如果你聽得到，請舉起你的手」                      → 只看舉手（確認意識）
```

## 狀態判斷

| 語音 | 肢體確認 | 結果 |
|---|---|---|
| 出現嚴重症狀（流血、胸口痛、喘不過、動不了等） | 不論 | 🚨 緊急 → Temi 撥視訊 |
| 有回應 | 點頭（需要打電話） | 🚨 緊急 → Temi 撥視訊 |
| 有回應、提到不適 | 搖頭（不需要打電話） | ⚠️ 需關注 → 只送文字通知 |
| 有回應、表示沒事 | 搖頭（不需要打電話） | ℹ️ 安全 |
| 有回應、提到不適 | 沒點頭也沒搖頭 | 🚨 緊急 → Temi 撥視訊 |
| 有回應、沒提到不適 | 沒點頭也沒搖頭 | ⚠️ 需關注 |
| 沒回應 | 舉手（有意識） | 🚨 緊急 → Temi 撥視訊，摘要註明「有意識，可能無法說話」 |
| 沒回應 | 沒舉手 | 🚨 緊急 → Temi 撥視訊，摘要註明「可能失去意識」 |

跌倒超過 5 分鐘：需關注升為緊急、安全升為需關注。C1 沒有傳 `purpose`（舊格式）時，沿用原本「語音與肢體是否表示需要幫忙」的規則。

## 環境需求

- C2（`event_reporter.py`、`mock_temi.py`、`send_test_event.py`）：Python 3.9 以上，只用標準函式庫，不需安裝套件。
- C1（`gesture_recognizer.py`）：開鏡頭時需要 `pip install opencv-python mediapipe`（MediaPipe 建議 Python 3.10～3.12）；第一次執行會自動下載姿勢模型到 `models/`。`demo` 模式不需安裝套件。

## 檔案說明

| 檔案 | 功能 |
|---|---|
| `event_reporter.py` | C2 主程式：接收資料、判斷狀態、產生摘要、通知聯絡人、送撥號指令給 Temi |
| `gesture_recognizer.py` | C1 肢體確認：提出問題（電腦語音唸出），用鏡頭辨識舉手／點頭／搖頭，結果送給 C2 |
| `mock_temi.py` | 假的 Temi：接收撥號指令，幾秒後回報接通或沒接 |
| `send_test_event.py` | 模擬 A 組、B 組、C1 傳送資料 |

## 執行方式

在 `event_report` 資料夾執行。Windows 中文顯示亂碼時，先執行 `$env:PYTHONIOENCODING='utf-8'`。

| 想做的事 | 指令 |
|---|---|
| C2：跑內建的五個測試情境 | `python event_reporter.py demo` |
| C1：測試辨識邏輯（不開鏡頭） | `python gesture_recognizer.py demo` |
| C1：開鏡頭看辨識效果 | `python gesture_recognizer.py camera` |
| C1：問「要不要打電話」，看點頭／搖頭 | `python gesture_recognizer.py ask` |
| C1：問「聽得到請舉手」，看舉手 | `python gesture_recognizer.py ask --purpose consciousness` |
| C1：測試用，兩題依序都問 | `python gesture_recognizer.py ask --purpose all` |
| C1：問完把結果送給 C2 | `python gesture_recognizer.py ask --event-id e001` |
| C1：等待 `/start` 指令再提問（port 8001） | `python gesture_recognizer.py server` |
| 啟動主程式（port 8000） | `python event_reporter.py server` |
| 啟動假 Temi，每通都接通（port 9000） | `python mock_temi.py` |
| 假 Temi：第一位沒接、第二位接通 | `python mock_temi.py first_no_answer` |
| 假 Temi：全部沒接 | `python mock_temi.py no_answer` |
| 送緊急情境資料 | `python send_test_event.py` |
| 送安全情境資料 | `python send_test_event.py safe` |
| 送「沒說話但能舉手」情境資料 | `python send_test_event.py silent` |

完整測試要開三個終端機：依序啟動主程式、假 Temi，再送測試資料。

## 串接介面

用 HTTP POST 傳 JSON 到 `http://<C2電腦IP>:8000`，同一次跌倒要用同一個 `event_id`。

| 路徑 | 傳送者 | 內容範例 |
|---|---|---|
| `/fall` | A 組 | `{"event_id": "e001", "fall_time": "2026-11-08T14:32:10", "location": "客廳"}` |
| `/speech` | B 組 | `{"event_id": "e001", "text": "我腳好痛", "language": "zh-TW", "llm_intent": "need_help"}` |
| `/gesture` | C1 | `{"event_id": "e001", "purpose": "call_consent", "gestures": ["nod"]}` |
| `/call_status` | Temi | `{"event_id": "e001", "contact": "女兒", "status": "connected"}` |
| `/event` | 任何人 | 三份資料合成一包：`{"fall": {...}, "speech": {...}, "gesture": {...}}` |

- `language`：`zh-TW`（國語）或 `nan-TW`（台語）。
- `llm_intent`：`need_help`、`ok` 或留空。
- `purpose`：`call_consent`（問要不要打電話）或 `consciousness`（請舉手確認意識）。
- `gestures`：`raise_hand`（舉手）、`nod`（點頭）、`shake_head`（搖頭）；沒做出指定動作送空清單 `[]`。

C1 server 模式的啟動指令（B 組或 Temi 在語音確認結束後呼叫）：

```text
POST http://<C1電腦IP>:8001/start  {"event_id": "e001", "purpose": "call_consent", "seconds": 10}
```
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
| 狀態判斷（緊急／需關注／安全） | ✅ 完成，五個情境測過 |
| C1 提問＋辨識舉手／點頭／搖頭 | ✅ 邏輯用假資料測過（15 項）；⚠️ 實際鏡頭需自行測試、調整靈敏度 |
| 中文事件摘要 | ✅ 完成 |
| 撥號指令、沒接改打下一位、全部沒接再次通知 | ✅ 完成，用假 Temi 測過 |
| Email／Telegram 通知 | ⚠️ 程式已寫，尚未設定帳號實測 |
| Temi 實際撥視訊 | ❌ 未完成，待確認用 RIM2 或 Temi Java App |
| 與 A 組、B 組、C1 實際串接 | ❌ 未完成，目前用模擬資料 |
| 整合進 `fall_response` 的 `Notifier` 介面 | ❌ 未完成，待與組員討論 |

## 待討論

- **Temi 怎麼撥視訊：** 透過 RIM2 API／eRoute，或在 Temi 上寫 Java App 接收指令。確定後修改 `send_to_temi()`。
- **與主程式整合：** `fall_response/incident.py` 已有事件摘要和 `Notifier` 介面，功能和本模組重疊；之後可把通知與撥號改寫成 `Notifier` 的實作，不另外啟動伺服器。
- **語音沒回應時的流程：** 主程式 `fall_response` 的規則是「無語音回應 → 直接撥打，不做肢體確認」；本模組依簡報改為先請長者舉手，不論有沒有舉手都會撥打，只差在摘要會註明是否有意識。需與負責主流程的組員對齊。
- **C1 與 `fall_response/gesture_recognizer.py`：** 主程式已有一份肢體辨識模組，本資料夾的版本是獨立測試用，之後需討論要保留哪一份。
- **event_id 由誰產生：** 建議由 A 組偵測到跌倒時產生，再傳給 B 組與 C1。
