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
| `incident.py` | 應變流程、逾時保護、`build_summary()`、JSONL 紀錄，以及各介面的 `Protocol` 與模擬版本 | models |
| `response_layers.py` | `VoiceLayer`（轉接語音組）與 `SimulatedVoiceClient` | incident |
| `temi_rim.py` | `RimRobotController`：透過 RIM2 執行 eRoute 派遣 Temi（可試跑）；`ConfiguredPlanner`；設定檔讀取 | 只用標準函式庫（urllib） |
| `zones.py` | 跌倒區域 `Zone`、`ZonePlanner`（依跌倒位置選觀察點）、框選區域工具 | overlay、video_input |
| `overlay.py` | 預覽畫面的中文文字與區域框 | cv2、Pillow |
| `app.py` | 主迴圈 `FallMonitorApp` | fall_detector、incident |
| `main.py` | 命令列入口、`build_incidents()` 組裝各模組、畫面顯示 | 以上全部、Pillow |

`cv2`、`mediapipe`、Pillow 都在函式內才匯入，所以單元測試不需要鏡頭或模型。

## 功能狀態

**已實作（實際運作）：** 影像輸入、MediaPipe 關節點、跌倒狀態機、跌倒區域判斷、應變流程與逾時保護、事件摘要、JSONL 紀錄、預覽畫面與中文提示。全部只用合成資料與少量實拍測過；門檻尚未用正式實驗校正，也還沒統計誤報率和漏報率。

**模擬（介面已定，內容是假的）：** 預設都在 `main.py` 的 `build_incidents()` 組裝。

| 介面（`Protocol`） | 目前使用 | 實際行為 |
|---|---|---|
| `ObservationPlanner.choose()` | 有 `zones.json` 時為 `ZonePlanner`（實際運作）；否則為 `SimulatedPlanner`，或 `--robot dry-run`／`rim` 時的 `ConfiguredPlanner` | 依跌倒區域選觀察點；沒有區域時用 `"home base"` 或設定檔的 `default_location` |
| `RobotController.go_to()` | `SimulatedRobot`；`--robot dry-run`／`rim` 時為 `RimRobotController` | 模擬版只寫紀錄；`dry-run` 只查詢 RIM2 任務、不送出；`rim` 真的派遣，但尚未在實機測試 |
| `VoiceClient.confirm_voice()` | `SimulatedVoiceClient` | 依 `--simulate-voice-response` 回傳固定內容 |
| `Notifier` | `SimulatedNotifier` | 只寫紀錄，不會通知或撥打；`wait_for_ack()` 固定回傳已查看 |
| 肢體層（`ResponseLayer`） | `SimulatedLayer("肢體")` | 視為未完成動作；肢體確認由其他組員負責，程式已移除，介面保留 |

**尚未實作：** 依現場遮擋自動評估最佳觀察點（目前是人工框選區域對應觀察點）、Temi 抵達確認（RIM2 只回報任務已送出）、肢體確認、語音辨識（含台語）、LLM、實際通知與撥號、多人追蹤。已知會漏報很慢的癱倒與跌坐，見 `tests/test_fall_detector.py` 的 `KnownLimits`。

## 介面與串接注意事項

- **`FallEvent`：** `wall_time` 是開始跌倒的時間；`onset_delay_s` 是從開始跌倒到判定經過的秒數，`timeline["total_s"]` 會把它加進總應變時間；`position_px` 是畫面像素座標，不是 Temi 地圖座標，重置後從未看到髖部時為 `None`（此時前往預設觀察點）；`frame_size` 由 `FallMonitorApp` 補上，`ZonePlanner` 用它換算成畫面比例。
- **跌倒區域：** `zones.json` 的區域以畫面比例（0～1）記錄，`location` 要和 Temi 上的地點及 `temi_config.json` 的 `routes` 一致；鏡頭移動後要用 `python -m fall_response.zones` 重新框選。
- **`ResponseLayer.confirm(record, timeout_s) -> LayerResult`：** 必須在 `timeout_s` 內返回。`IncidentManager` 會在逾時加 `grace_s` 後放棄等待，但無法終止該執行緒。丟出例外會被記錄，流程繼續。
- **`VoiceClient.confirm_voice()`：** 只有 `"responded": true`（布林值）才算有回應；欄位格式見 README「語音模組串接方式」。`language` 為 `zh-TW` 或 `nan-TW`。
- **肢體結果：** 肢體層回傳 `responded=False` 時記錄為 `BodyStatus.NOT_COMPLETED`（未能完成肢體指令）；丟出例外或逾時記錄為 `UNAVAILABLE`，兩者不可混用。
- **`RobotController.go_to()`：** 回傳 `True` 代表 Temi 已到場或已派遣；無法回報實際抵達的實作要設 `confirms_arrival = False`，摘要會顯示「已派遣（未確認抵達）」。`ObservationPlanner.choose()` 回傳的名稱要和設定檔 `routes` 的鍵、Temi 地圖上的地點一致。
- **RIM2：** `eroute/execute` 只代表任務送到機器人，失敗時會回 `Timeout`（code 10012）。`RimRobotController` 送出後等待 `arrival_wait_s` 秒，但 `go_to()` 一定在 `timeout_s` 內返回，否則流程會判定 Temi 無法到場，背景卻仍在派遣。`--robot rim` 必須設定 `robot_name`；`main()` 啟動時會呼叫 `prepare()` 先查好所有觀察點的任務 ID。收到 HTTP 401 時會重新登入一次再試。帳密放在根目錄的 `temi_config.json`（已列入 `.gitignore`），範本是 `temi_config.example.json`。
- **`Notifier.wait_for_ack()`：** 只有確定聯絡人已查看才回傳 `True`，否則流程會改為撥打。
- **`PoseEstimator`：** 使用 VIDEO 模式，時間戳必須遞增，所以每個影像來源要用各自的實例。
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
python -m fall_response.main --source 0 --simulate-voice-response  # 走有語音回應的流程
python -m fall_response.zones --source 0                       # 框選跌倒區域與觀察點
python -m fall_response.temi_rim --list                        # 列出 RIM2 機器人與 eRoute，不會移動 Temi
python -m fall_response.temi_rim --check 客廳                  # 確認地點對應的 eRoute，不會移動 Temi
python -m fall_response.main --source 0 --robot dry-run        # 試跑派遣，不會移動 Temi
python -m fall_response.main --source 0 --robot rim            # 偵測到跌倒時真的派遣 Temi
```

`temi_rim --go` 和 `--robot rim` 會讓 Temi 真的移動，執行前要先問過使用者。RIM2 是全班共用帳號，有多台 Temi 和其他組的 eRoute，不要修改或刪除別組的任務。

測試輸出中的 `RuntimeError`、「無法連線影像來源」是測試刻意製造的故障，只要最後是 `OK` 就代表通過。
