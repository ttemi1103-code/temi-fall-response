# CLAUDE.md

Temi 跌倒監測與應變系統，人因工程專題，8 人共同開發。安裝、執行與畫面說明見 [README.md](README.md)；本檔只記錄修改程式前需要知道的事。

## 專案目標與架構

固定鏡頭偵測長者跌倒後，安排 Temi 到場，依序做語音、肢體確認，再通知緊急聯絡人。偵測與流程控制都在電腦上以 Python 執行；Temi 端（Java／Android）與語音模組由其他組員負責，透過介面串接。

```text
VideoSource → PoseEstimator → FallDetector ─(FallEvent)→ IncidentManager（背景執行緒）
  影像輸入       MediaPipe 關節點    狀態機判斷跌倒              選點 → 前往 → 語音 → 肢體 → 通知
```

`IncidentManager` 的流程規則（依簡報第 14 頁）：

- **有語音回應：** 做肢體確認 → 傳送摘要 → 聯絡人已查看就結束，未查看則撥打。
- **無語音回應、語音出錯或逾時、Temi 無法到場：** 直接撥打，不做肢體確認。
- 流程由程式規則決定。LLM 只整理長者描述，不判斷傷勢嚴重程度、不診斷，也不決定是否撥打。

## 模組與依賴

| 模組 | 功能 | 依賴 |
|---|---|---|
| `models.py` | 共用資料：`Frame`、`Landmark`、`Pose`、`FallEvent`，以及關節點編號 | 無 |
| `video_input.py` | 鏡頭、串流網址、影片檔；即時來源只留最新畫面並自動重連 | models、cv2 |
| `pose_estimator.py` | MediaPipe Pose Landmarker，回傳像素座標的 `Pose` | models、mediapipe、cv2 |
| `fall_detector.py` | 跌倒狀態機，門檻在 `FallConfig` | models |
| `gesture_recognizer.py` | 舉手、揮手、點頭、搖頭，門檻在 `GestureConfig` | models |
| `incident.py` | 應變流程、逾時保護、`build_summary()`、JSONL 紀錄，以及各介面的 `Protocol` 與模擬版本 | models |
| `response_layers.py` | `VoiceLayer`（轉接語音組）、`BodyLayer`（用影像辨識指令後的動作） | gesture_recognizer、incident |
| `app.py` | 主迴圈 `FallMonitorApp`、動作測試 `run_gesture_demo()` | fall_detector、gesture_recognizer、incident |
| `main.py` | 命令列入口、`build_incidents()` 組裝各模組、畫面顯示 | 以上全部、Pillow |

`cv2`、`mediapipe`、Pillow 都在函式內才匯入，所以單元測試不需要鏡頭或模型。

## 功能狀態

**已實作（實際運作）：** 影像輸入、MediaPipe 關節點、跌倒狀態機、動作辨識、應變流程與逾時保護、事件摘要、JSONL 紀錄、預覽畫面與中文提示。全部只用合成資料與少量實拍測過；門檻尚未用正式實驗校正，也還沒統計誤報率和漏報率。

**模擬（介面已定，內容是假的）：** 預設都在 `main.py` 的 `build_incidents()` 組裝。

| 介面（`Protocol`） | 目前使用 | 實際行為 |
|---|---|---|
| `ObservationPlanner.choose()` | `SimulatedPlanner` | 固定回傳 `"home base"` |
| `RobotController.go_to()` | `SimulatedRobot` | 只寫紀錄，回傳已到達 |
| `VoiceClient.confirm_voice()` | `SimulatedVoiceClient` | 依 `--simulate-voice-response` 回傳固定內容 |
| `Speaker.speak()` | `SimulatedSpeaker` | 只寫紀錄，Temi 不會說話 |
| `Notifier` | `SimulatedNotifier` | 只寫紀錄，不會通知或撥打；`wait_for_ack()` 固定回傳已查看 |
| 肢體層 | 未設 `--temi-camera` 時為 `SimulatedLayer` | 視為未完成動作 |

**尚未實作：** 跌倒區域判斷與候選觀察點、Temi 實機控制、Temi 鏡頭影像來源、語音辨識（含台語）、LLM、實際通知與撥號、多人追蹤。已知會漏報很慢的癱倒與跌坐，見 `tests/test_fall_detector.py` 的 `KnownLimits`。

## 介面與串接注意事項

- **`FallEvent`：** `wall_time` 是開始跌倒的時間；`onset_delay_s` 是從開始跌倒到判定經過的秒數，`timeline["total_s"]` 會把它加進總應變時間；`position_px` 是畫面像素座標，不是 Temi 地圖座標。
- **`ResponseLayer.confirm(record, timeout_s) -> LayerResult`：** 必須在 `timeout_s` 內返回。`IncidentManager` 會在逾時加 `grace_s` 後放棄等待，但無法終止該執行緒。丟出例外會被記錄，流程繼續。
- **`VoiceClient.confirm_voice()`：** 只有 `"responded": true`（布林值）才算有回應；欄位格式見 README「語音模組串接方式」。`language` 為 `zh-TW` 或 `nan-TW`。
- **肢體結果：** 長者沒做到是 `BodyStatus.NOT_COMPLETED`（未能完成肢體指令）；鏡頭或程式出錯是 `UNAVAILABLE`，兩者不可混用。只接受舉手、揮手、點頭，搖頭不算完成指令。
- **`RobotController.go_to()`：** 只在確定到達時回傳 `True`。`ObservationPlanner.choose()` 回傳的名稱要和 Temi 地圖上存的地點名稱一致。
- **`Notifier.wait_for_ack()`：** 只有確定聯絡人已查看才回傳 `True`，否則流程會改為撥打。
- **`PoseEstimator`：** 使用 VIDEO 模式，時間戳必須遞增，所以每個影像來源要用各自的實例；肢體層不能和固定鏡頭共用。
- **事件摘要：** `build_summary()` 的中文鍵名有測試依賴，修改時要一起更新測試和通知端。
- **畫面門檻文字：** `main.py` 的 `monitor_lines()` 把門檻寫死在字串裡（例如 `fall >= 1.50`），修改 `FallConfig` 時要一起更新。
- **隱私：** 系統不儲存影像。`fall_response/logs/`、模型檔（`*.task`）和影片都已列在 `.gitignore`，不要上傳。

## 協作規範

- **修改前：** 先讀要改的模組、它的測試，以及會呼叫它的模組（見上方依賴表）。
- **不破壞其他模組：** 不要更改別人負責的介面簽名、回傳格式或流程規則；需要更改時先和負責人討論，並同步更新測試、README 與本檔。
- **新功能優先用介面接入：** 例如新增實作 `RobotController` 或 `VoiceClient` 的類別，在 `build_incidents()` 替換模擬版本，不要改 `IncidentManager` 的流程。
- **修改後：** 執行全部單元測試並確認 `OK`；新增或修改行為時要補測試。
- **分支與合併：** 每項工作開功能分支（例如 `fall-threshold`、`voice-taiwanese`），透過 Pull Request 請組員看過再合併。不要直接推送到 `main`。
- **不要上傳：** 帳號密碼、受測者影片或照片、實驗紀錄。

## 環境與常用指令

Windows、PowerShell，目前只在 Python 3.15 測過。虛擬環境放在專案外的 `$env:USERPROFILE\venvs\temi`；第一次安裝與下載模型的步驟見 README。

```powershell
& $env:USERPROFILE\venvs\temi\Scripts\Activate.ps1          # 啟用虛擬環境
python -m unittest discover -s fall_response/tests -t .        # 全部單元測試
python -m fall_response.main --source 0                        # 跌倒偵測
python -m fall_response.main --mode gesture --source 0         # 動作辨識
python -m fall_response.main --source 0 --simulate-voice-response  # 走有語音回應的流程
```

測試輸出中的 `RuntimeError`、「無法連線影像來源」是測試刻意製造的故障，只要最後是 `OK` 就代表通過。
