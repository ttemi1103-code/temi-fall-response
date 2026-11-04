# Temi 跌倒監測與應變系統

人因工程專題（B 班第三組）的程式。系統用固定鏡頭偵測跌倒，再依流程安排 Temi 到場確認，並把結果通知緊急聯絡人。

> **目前進度：** 影像輸入、肢體辨識、跌倒偵測和主程式架構已完成。Temi 移動、語音、LLM、通知都還是**模擬版本**，不會移動機器人、發出通知或撥打電話。

## 系統流程

```text
跌倒偵測 → Temi 前往 → 語音確認 ─ 有回應 → 肢體確認 → 傳送摘要 ─ 已查看 → 聯絡人決定後續
                                │                              └ 未查看 → Temi 撥打聯絡人
                                └ 無回應 → 直接撥打聯絡人
```

- Temi 無法到場，或語音模組出錯、逾時，都視為無語音回應並直接撥打。
- 長者未完成動作時，標記「未能完成肢體指令」；鏡頭故障則標記「無法進行肢體確認」。
- 系統只記錄可觀察到的回應，不判斷傷勢嚴重程度，也不做診斷。
- 流程由程式規則決定，不交給 LLM。

## 環境需求

- Windows 10 或 11（中文提示使用 Windows 內建的微軟正黑體）
- Python 3（目前只在 Python 3.15 上測試過）
- 電腦鏡頭、手機串流或影片檔

## 第一次使用

以下指令都在 PowerShell 執行，一次貼上一行。

### 1. 開啟 PowerShell

1. 用檔案總管開啟這個專案資料夾，也就是看得到 `README.md` 和 `fall_response` 的那一層。
2. 點一下上方的網址列，輸入 `powershell`，按 Enter。

開啟後，游標前面應該會顯示這個資料夾的路徑。

### 2. 建立並啟用虛擬環境

```powershell
python -m venv $env:USERPROFILE\venvs\temi
```

```powershell
& $env:USERPROFILE\venvs\temi\Scripts\Activate.ps1
```

成功後，每行開頭會出現 `(temi)`。虛擬環境放在專案外面，避免大量檔案被 OneDrive 或 Git 納入。

如果出現「已停用指令碼執行」的錯誤，先執行下面這行，再重新啟用。它只影響目前這個視窗：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 3. 安裝套件

可能需要幾分鐘：

```powershell
pip install -r requirements.txt
```

### 4. 下載姿勢模型

模型沒有放進 GitHub，只要下載一次：

```powershell
New-Item -ItemType Directory -Force -Path fall_response\models | Out-Null
```

```powershell
Invoke-WebRequest -UseBasicParsing -Uri "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task" -OutFile "fall_response\models\pose_landmarker_full.task"
```

### 5. 確認程式正常

```powershell
python -m unittest discover -s fall_response/tests -t .
```

最後出現 `OK` 就是正常。過程中的 `RuntimeError`、「無法連線影像來源」是測試故意製造的故障，不是程式壞掉。這一步不會開啟鏡頭。

## 之後每次使用

開啟 PowerShell（步驟 1）後，只要先啟用虛擬環境：

```powershell
& $env:USERPROFILE\venvs\temi\Scripts\Activate.ps1
```

## 執行程式

預覽視窗中按 `q` 結束（要先點一下視窗）。

### 測試跌倒偵測

```powershell
python -m fall_response.main --source 0
```

1. 站著約 2 秒，讓全身入鏡。
2. 快速倒到防護墊上，並請人在旁邊注意安全。
3. 躺著不動約 3 秒。

畫面下方會先出現橘色「疑似摔倒，確認中」，判定後變成紅色「偵測到摔倒」，並停留 5 秒。

### 測試肢體動作

```powershell
python -m fall_response.main --mode gesture --source 0
```

可以偵測舉手、揮手、點頭、搖頭。文字顯示 2 秒後會清除，可以接著測下一次。

### 其他常用指令

| 用途 | 指令 |
|---|---|
| 改用手機串流（網址依 App 而定） | `python -m fall_response.main --source "http://手機IP:8080/video"` |
| 用影片檔測試 | `python -m fall_response.main --source 實驗影片.mp4` |
| 模擬長者有語音回應 | `python -m fall_response.main --source 0 --simulate-voice-response` |
| 設定互動語言為台語 | `python -m fall_response.main --source 0 --language nan-TW` |
| 不開預覽視窗 | 在指令最後加上 `--no-display` |

### 畫面上的數值

| 文字 | 意思 |
|---|---|
| `state` | 目前狀態：`monitoring`、`candidate`、`confirming`、`fallen` |
| `descent` | 頭、肩、髖的平均下降速度（軀幹長度／秒） |
| `angle` | 軀幹與垂直線的夾角，0 度是站直，90 度是平躺 |
| `ratio` | 身體外框的寬高比，只拍到上半身時不會用來判斷 |
| `peak descent` | 最近 5 秒的最高下降速度 |
| `FPS` | 每秒處理的畫面張數，太低時較難抓到快速摔倒 |

每次判定跌倒的結果會寫入 `fall_response/logs/incidents.jsonl`，不會儲存影像。

## 跌倒判定邏輯

```text
監測中 ──快速下降──▶ 候選 ──倒地姿勢或人被遮擋──▶ 確認中 ──維持 3 秒且幾乎不動──▶ 判定跌倒
```

- **快速下降：** 平均下降速度達每秒 1.5 個軀幹長；或離開站姿 2 秒內就倒地，且髖部至少低 0.6 個軀幹長。
- **倒地姿勢：** 軀幹傾斜至少 55 度，或寬高比至少 1.3。
- **確認：** 維持倒地 3 秒且幾乎不動就判定。如果一直在動會延後，但最多 8 秒就會判定。中途站起來則取消。

所有門檻都在 `fall_response/fall_detector.py` 的 `FallConfig`，動作辨識的門檻在 `gesture_recognizer.py` 的 `GestureConfig`。目前都是起始值，尚未用正式實驗校正。

### 已知限制

- 很慢的癱倒，以及跌坐（軀幹仍直立）目前會漏報。
- 在地上伸展等持續動作，超過 8 秒可能誤報；躺在沙發和倒在地上也還無法區分。
- 目前一次只追蹤一個人，測試時建議只讓一人入鏡。
- 跌倒區域和候選觀察點尚未製作，觀察點暫時固定回傳 `home base`。
- 尚未統計實際的誤報率和漏報率。

## 程式架構

| 檔案 | 功能 |
|---|---|
| `fall_response/video_input.py` | 影像輸入：鏡頭、手機串流、影片檔，即時來源會自動重連 |
| `fall_response/pose_estimator.py` | 用 MediaPipe 取得 33 個人體關節點 |
| `fall_response/fall_detector.py` | 跌倒偵測 |
| `fall_response/gesture_recognizer.py` | 肢體動作辨識 |
| `fall_response/response_layers.py` | 語音層與肢體層 |
| `fall_response/incident.py` | 應變流程、逾時處理、事件摘要與紀錄 |
| `fall_response/app.py`、`main.py` | 主迴圈與命令列入口 |
| `fall_response/tests/` | 單元測試 |

## 語音模組串接方式

在 `main.py` 的 `build_incidents()` 把 `SimulatedVoiceClient` 換成語音組的物件，它要提供：

```python
def confirm_voice(self, incident_time: str, location: str, language: str, timeout_s: float) -> dict:
    ...
```

`language` 是 `zh-TW`（國語）或 `nan-TW`（台語）。回傳格式：

```json
{
  "responded": true,
  "transcript": "我右膝很痛，站不起來",
  "pain_location": "右膝",
  "can_stand_up": false,
  "needs_help": true,
  "language": "zh-TW",
  "response_latency_s": 2.4
}
```

- 只有確實聽到長者回應才回傳 `"responded": true`；資訊不完整的欄位填 `null`。
- 請在 `timeout_s` 內回傳。逾時或丟出例外時，系統會視為無回應並直接撥打。
- 回傳內容只整理長者描述，不判斷傷勢。

## Temi 鏡頭串接方式

肢體層只需要一個 OpenCV 能開啟的影像來源，用 `--temi-camera` 指定網址，並搭配有語音回應的流程：

```powershell
python -m fall_response.main --source 0 --simulate-voice-response --temi-camera "http://串流網址"
```

Temi 鏡頭要如何變成這個來源，還需要實機驗證。

## 常見問題

| 狀況 | 處理方式 |
|---|---|
| 找不到姿勢模型 | 回到「下載姿勢模型」執行一次 |
| 一直出現「無法連線影像來源」 | 按 `Ctrl+C` 結束，把 `--source 0` 改成 `--source 1`，或關閉正在使用鏡頭的程式 |
| 畫面顯示 `shoulders/hips NOT VISIBLE` | 肩膀或髖部沒有入鏡，把鏡頭拉遠 |
| 有摔倒但沒有觸發 | 確認全身入鏡，並看摔倒當下的 `peak descent`、`angle` 和 `FPS` |
