# 跌倒事件資訊整合與緊急通知系統（C2）

Temi 跌倒照護專案的**第三階段：事件回報**。
整合 A 組、B 組、C1 的結果，判斷跌倒者狀況，產生事件摘要通知緊急聯絡人，必要時請 Temi 撥視訊給家屬。

## 系統流程

```
A 組（跌倒時間、位置） ─┐
B 組（語音結果）       ─┼→ C2：接收資料 → 判斷狀態 → 產生摘要 → 通知聯絡人 → 請 Temi 撥視訊
C1（肢體辨識結果）     ─┘
```

## 檔案說明

| 檔案 | 功能 |
|---|---|
| `event_reporter.py` | 主程式。接收 A／B／C1 的資料，判斷狀態（緊急／需關注／安全），產生中文摘要，用 Email／Telegram 通知聯絡人；緊急時送指令請 Temi 撥視訊，沒接通會改打下一位聯絡人 |
| `mock_temi.py` | 假的 Temi，還沒接上真的 Temi 前用來測試「打電話」指令與通話結果回報 |
| `send_test_event.py` | 模擬 A 組、B 組、C1 傳送資料，用來測試整個流程 |

## 狀態判斷規則

| 情況 | 結果 |
|---|---|
| 語音出現嚴重症狀（流血、胸口痛、喘不過等） | 🚨 緊急 → Temi 撥視訊 |
| 沒有語音也沒有肢體回應 | 🚨 緊急 → Temi 撥視訊 |
| 語音和肢體都表示需要幫忙 | 🚨 緊急 → Temi 撥視訊 |
| 只有一項表示需要幫忙 | ⚠️ 需關注 |
| 說沒事、搖頭 | ℹ️ 安全 |
| 跌倒超過 5 分鐘 | 狀態升一級 |

## 執行方式

只需要 Python 3.9 以上，不用另外安裝套件。

```bash
python event_reporter.py demo      # 跑內建測試情境
```

完整串接測試（開三個終端機）：

```bash
python event_reporter.py server    # 1. 主程式（port 8000）
python mock_temi.py                # 2. 假 Temi（port 9000）
python send_test_event.py          # 3. 送測試資料
```

> Windows 若中文顯示亂碼，先執行 `$env:PYTHONIOENCODING='utf-8'`

## 給其他組的介面

用 HTTP POST 傳 JSON 到 `http://<C2電腦IP>:8000`，同一次跌倒要用同一個 `event_id`：

| 路徑 | 傳送者 | 內容範例 |
|---|---|---|
| `/fall` | A 組 | `{"event_id": "e001", "fall_time": "2026-11-08T14:32:10", "location": "客廳"}` |
| `/speech` | B 組 | `{"event_id": "e001", "text": "我腳好痛", "language": "zh-TW", "llm_intent": "need_help"}` |
| `/gesture` | C1 | `{"event_id": "e001", "gestures": ["raise_hand"]}`（`raise_hand`／`nod`／`shake_head`） |
| `/call_status` | Temi | `{"event_id": "e001", "contact": "女兒", "status": "connected"}`（`connected`／`no_answer`） |

C2 送給 Temi 的指令：`{"action": "call_contact", "contact": "女兒", "event_id": "e001"}`

## 設定

在 `event_reporter.py` 的 `CONFIG` 修改，或用環境變數設定：

- `TEMI_URL`：Temi 接收指令的網址
- `TEMI_CONTACTS`：要撥打的聯絡人，依序撥打（例如 `女兒,兒子`）
- `CONTACT_EMAIL`、`SMTP_USER`、`SMTP_PASSWORD`：Email 通知（密碼請用環境變數，不要寫進程式上傳）
- `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`：Telegram 通知

## 目前進度

- ✅ 接收資料、狀態判斷、事件摘要、Temi 撥號指令（已用假 Temi 測試）
- ⏳ Email／Telegram 實際寄送（待設定帳號）
- ⏳ 與 A 組、B 組、C1、真正的 Temi App 串接
