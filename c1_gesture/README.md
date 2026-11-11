# C1 肢體反應確認

Temi 跌倒監測與應變系統的**肢體反應確認**。長者有語音回應後，Temi 依序提出三個肢體指令，用鏡頭辨識長者的回應，再把結果送給 C2（`c2_event_report`）。

> **目前進度：** 提問（電腦語音）、舉雙手／動腳／點頭／搖頭辨識、結果送給 C2 都已完成，辨識邏輯用假資料測過（22 項）。**尚未用實際鏡頭充分測試**，靈敏度可能需要調整；問題目前由電腦語音唸出，還沒改成由 Temi 唸。

## 在流程中的位置

```text
語音互動 ─ 有語音回應 → 【C1 肢體反應確認】→ C2 整理並傳送摘要
         └ 無語音回應 → 不做肢體確認，C2 直接撥打
```

長者已經能說話，所以 C1 確認的是**身體能不能動**，以及**長者要不要協助**。

## 三個肢體指令

| 順序 | Temi 提問 | 辨識 | 結果 |
|---|---|---|---|
| 1 | 「請把兩隻手都舉起來。」 | 左右手各自有沒有舉起 | 雙手／只有左手／只有右手／沒有舉手 |
| 2 | 「請動一動你的腳。」 | 腳踝相對臀部有沒有移動 | 有動／沒動 |
| 3 | 「需要幫你打電話給家人嗎？需要請點頭，不需要請搖頭。」 | 點頭／搖頭 | 需要／不需要／沒反應 |

- 每題最多看 10 秒，辨識到完整動作就提早進下一題。
- 每題只算該題相關的動作，例如問要不要打電話時舉手不算。
- 問題文字在 `gesture_recognizer.py` 的 `QUESTIONS` 修改。

## 辨識方式

使用 MediaPipe Pose 的 33 個人體關鍵點：

| 動作 | 判斷方式 |
|---|---|
| 舉手 | 手腕沿「臀部→肩膀」的身體方向，比肩膀高出「肩寬 × 0.5」，並維持 0.6 秒；左右手分開判斷 |
| 動腳 | 腳踝（看不到就用膝蓋）相對臀部的位置，2 秒內移動超過「軀幹長度 × 0.15」 |
| 點頭 | 鼻子相對兩耳連線上下來回 3 次以上（2.5 秒內） |
| 搖頭 | 鼻子相對兩耳連線左右來回 3 次以上（2.5 秒內） |

- 用身體與頭部自己的方向判斷，所以坐著、躺著也能辨識。
- 以臀部為基準看腳，人整個移動不會被當成動腳。
- 靈敏度在 `gesture_recognizer.py` 的 `PARAMS` 調整。

## 環境需求

```powershell
python -m pip install opencv-python mediapipe
```

第一次開鏡頭時會自動下載姿勢模型（約 5MB）到 `models/`，模型檔已列在 `.gitignore`，不會上傳。`demo` 模式不需安裝套件。

## 執行方式

在 `c1_gesture` 資料夾執行。Windows 中文顯示亂碼時，先執行 `$env:PYTHONIOENCODING='utf-8'`。

| 想做的事 | 指令 |
|---|---|
| 測試辨識邏輯（不開鏡頭） | `python gesture_recognizer.py demo` |
| 開鏡頭看辨識效果（不問問題） | `python gesture_recognizer.py camera` |
| 三題依序問完 | `python gesture_recognizer.py ask` |
| 只測一題 | `python gesture_recognizer.py ask --purpose legs`（`arms`／`legs`／`call_consent`） |
| 三題問完，結果送給 C2 | `python gesture_recognizer.py ask --event-id e001` |
| 等待 `/start` 指令再提問（port 8001） | `python gesture_recognizer.py server` |

其他選項：`--seconds 15`（每題觀察秒數）、`--source 影片檔或串流網址`、`--no-voice`（不唸問題）、`--no-window`（不顯示畫面）。開鏡頭時在畫面上按 `q` 可以跳過該題。

## 串接介面

**啟動確認**（B 組或 Temi 在語音確認「有回應」後呼叫）：

```text
POST http://<C1電腦IP>:8001/start
{"event_id": "e001", "seconds": 10}
```

**送給 C2 的結果**：

```text
POST http://<C2電腦IP>:8000/gesture
{"event_id": "e001", "arms": "both", "arm_raised": true, "leg_moved": true, "call_consent": "nod"}
```

| 欄位 | 值 |
|---|---|
| `arms` | `both`（雙手）、`left`（只有左手）、`right`（只有右手）、`none`（沒有舉手） |
| `arm_raised` | 至少一隻手有舉起為 `true` |
| `leg_moved` | `true`／`false` |
| `call_consent` | `nod`（需要打電話）、`shake_head`（不需要）、`""`（沒反應） |

## 設定

| 設定（環境變數） | 說明 |
|---|---|
| `C2_URL` | C2 的位址（預設 `http://127.0.0.1:8000`） |
| `CAMERA_SOURCE` | 攝影機編號，或影片檔／串流網址（預設 `0`） |
| `OBSERVE_SECONDS` | 每題最多觀察秒數（預設 10） |
| `C1_VOICE` | `1` 用電腦語音唸問題，`0` 只顯示文字 |
| `C1_PORT` | server 模式的 port（預設 8001） |

## 功能狀態

| 項目 | 狀態 |
|---|---|
| 三題提問（Windows 中文語音） | ✅ 完成 |
| 舉手（左右分開）、動腳、點頭、搖頭辨識 | ✅ 假資料測過（站姿、躺姿、雜訊共 22 項）；⚠️ 實際鏡頭待測試 |
| 結果送給 C2、server 模式 | ✅ 完成 |
| 由 Temi 唸問題、使用 Temi 鏡頭 | ❌ 未完成，待 Temi 端串接 |
| 台語版問題 | ❌ 未完成，用字待 B 組確認 |

## 待討論

- **與 `fall_response/gesture_recognizer.py`：** 主程式已有一份肢體辨識模組，之後需討論要保留哪一份或合併。
- **主程式的肢體規則：** `fall_response` 目前只接受舉手、揮手、點頭；本模組多了雙手分開判斷、動腳和搖頭，需和負責人對齊。
