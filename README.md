# Temi 跌倒監測與應變系統

人因工程專題（B 班第三組）的程式。系統用固定鏡頭偵測跌倒，再依流程安排 Temi 到場確認，並把結果通知緊急聯絡人。

> **目前進度：** 影像輸入、人體關節點辨識、跌倒偵測、跌倒區域判斷和主程式架構已完成。偵測到跌倒後可以透過 RIM2 派遣 Temi（需加上 `--robot rim`，尚未在實機上測試）。語音、肢體確認、LLM、通知仍是**模擬版本**，由其他組員負責，不會發出通知或撥打電話。

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

### 其他常用指令

| 用途 | 指令 |
|---|---|
| 改用手機串流（網址依 App 而定） | `python -m fall_response.main --source "http://手機IP:8080/video"` |
| 用影片檔測試 | `python -m fall_response.main --source 實驗影片.mp4` |
| 模擬長者有語音回應 | `python -m fall_response.main --source 0 --simulate-voice-response` |
| 設定互動語言為台語 | `python -m fall_response.main --source 0 --language nan-TW` |
| 試跑派遣，Temi 不會移動 | `python -m fall_response.main --source 0 --robot dry-run` |
| 偵測到跌倒時真的派遣 Temi | `python -m fall_response.main --source 0 --robot rim`（設定方式見「派遣 Temi」） |
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

所有門檻都在 `fall_response/fall_detector.py` 的 `FallConfig`，目前都是起始值，尚未用正式實驗校正。

### 已知限制

- 很慢的癱倒，以及跌坐（軀幹仍直立）目前會漏報。
- 在地上伸展等持續動作，超過 8 秒可能誤報；躺在沙發和倒在地上也還無法區分。
- 目前一次只追蹤一個人，測試時建議只讓一人入鏡。
- 跌倒區域依畫面上的髖部位置判斷，鏡頭移動後要重新框選；遮擋時使用最後看到的位置。
- 尚未統計實際的誤報率和漏報率。

## 程式架構

| 檔案 | 功能 |
|---|---|
| `fall_response/video_input.py` | 影像輸入：鏡頭、手機串流、影片檔，即時來源會自動重連 |
| `fall_response/pose_estimator.py` | 用 MediaPipe 取得 33 個人體關節點 |
| `fall_response/fall_detector.py` | 跌倒偵測 |
| `fall_response/response_layers.py` | 語音確認層（轉接語音組） |
| `fall_response/incident.py` | 應變流程、逾時處理、事件摘要與紀錄 |
| `fall_response/temi_rim.py` | 透過 RIM2 API 派遣 Temi |
| `fall_response/temi_app.py` | 透過 Temi 上的 Java App 派遣 Temi，並等待抵達 |
| `temi_app/FallResponseServer.java` | 加進 Temi App 的 Java 類別，接收派遣指令並回報導航狀態 |
| `fall_response/zones.py` | 跌倒區域判斷、選擇觀察點、框選區域的工具 |
| `fall_response/overlay.py` | 預覽畫面上的中文文字與區域框 |
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

## 跌倒區域與觀察點

在鏡頭畫面上框出幾個區域，每個區域對應一個 Temi 上已儲存的觀察點。偵測到跌倒時，程式依髖部位置判斷在哪個區域，再派 Temi 前往對應的觀察點。不在任何區域內時，選最近的區域；沒有設定區域時，前往預設觀察點。

### 1. 框出區域

鏡頭要固定在偵測時的位置，並使用相同的 `--source`：

```powershell
python -m fall_response.zones --source 0
```

1. 在影像視窗中拖曳滑鼠，框出一個區域，例如沙發前的地板。
2. 放開滑鼠後，回到 PowerShell 輸入區域名稱，例如 `沙發前`。
3. 再輸入 Temi 觀察點名稱，例如 `客廳`；這個名稱要和 Temi 上儲存的地點、`temi_config.json` 的 `routes` 一致。直接按 Enter 表示與區域同名。
4. 重複以上步驟，框出所有區域。
5. 每新增一個區域就會自動儲存。框錯時點一下影像視窗並按 `z` 移除上一個；全部框完後按 `×` 關閉影像視窗，或在 PowerShell 按 `Ctrl+C` 結束。

區域會存在 `zones.json`，以畫面比例記錄，換解析度也能使用；但鏡頭移動或角度改變後要重新框選。

**不在現場時：** 請現場的組員用固定好的鏡頭拍一張照片或一段影片，傳給你後執行下面的指令，畫面會停在第一張讓你框選：

```powershell
python -m fall_response.zones --source 現場照片.jpg
```

照片必須是**同一個鏡頭、同一個位置、同樣橫直方向**拍的，框選結果才能套用到實際偵測。

**只想練習流程時：** 另存成練習用檔案，避免覆蓋正式的 `zones.json`：

```powershell
python -m fall_response.zones --source 0 --zones practice_zones.json
```

```powershell
python -m fall_response.main --source 0 --zones practice_zones.json
```

### 2. 確認判斷是否正確

```powershell
python -m fall_response.main --source 0
```

畫面上會顯示所有區域；人所在的區域會變成紅色框。偵測到跌倒時，終端機會顯示「跌倒位置在「沙發前」，派往觀察點「客廳」」。這時使用的是模擬版 Temi，不會連線，也不會移動。

## 派遣 Temi（RIM2）

偵測到跌倒後，程式會呼叫博鈞 RIM2 API，執行事先建立好的 eRoute 任務，讓 Temi 前往指定地點。預設使用模擬版本，加上 `--robot rim` 才會真的派遣。

### 1. 在 RIM2 網站建立任務

1. 先在 Temi 上儲存地點，例如 `客廳`。
2. 登入 [rim2.mobisnet.com](https://rim2.mobisnet.com)，建立一個 eRoute，例如命名為 `跌倒應變-客廳`。
3. 任務內容加入「前往地點」，選擇 `客廳`。

### 2. 建立設定檔

把 `temi_config.example.json` 複製一份，改名為 `temi_config.json`，填入 RIM2 的授權代碼、帳號和密碼：

```powershell
Copy-Item temi_config.example.json temi_config.json
```

| 欄位 | 說明 |
|---|---|
| `authsid`、`username`、`password` | RIM2 登入資料 |
| `robot_name` | 機器人名稱，正式派遣時必填；`null` 只能用於 `--list`、`--check` 和試跑 |
| `default_location` | 偵測到跌倒時前往的地點 |
| `routes` | 地點對應的 eRoute 名稱，也可以直接填 eRoute 的數字 ID |
| `arrival_wait_s` | 派遣後等幾秒才開始語音確認，請依實際移動時間調整 |

`temi_config.json` 已列在 `.gitignore`，不會被 Git 上傳；用網頁上傳時也不要拖這個檔案。

### 3. 確認連線與派遣

先列出機器人和 eRoute，這一步不會移動 Temi：

```powershell
python -m fall_response.temi_rim --list
```

確認地點對應的任務存在，這一步也不會移動 Temi：

```powershell
python -m fall_response.temi_rim --check 客廳
```

### 4. 試跑：不移動 Temi

```powershell
python -m fall_response.main --source 0 --robot dry-run
```

偵測到跌倒時，程式會登入 RIM2 並找出要執行的任務，但**不會送出指令**。終端機顯示「【試跑】將派遣 Temi 前往 客廳（eRoute …），未送出指令」就代表區域、觀察點和任務都串接成功。

### 5. 真的派遣

**以下會讓 Temi 移動。** 先確認沒有其他組正在使用同一台 Temi，清空路線並注意周圍的人。

```powershell
python -m fall_response.temi_rim --go 客廳
```

```powershell
python -m fall_response.main --source 0 --robot rim
```

偵測到跌倒後，終端機會顯示「派遣 Temi 前往 客廳」。

### 限制

- **無法確認是否抵達：** RIM2 只回覆任務已送到機器人，不回報 Temi 何時抵達。程式會等待 `arrival_wait_s` 秒後繼續，事件摘要標示為「已派遣（未確認抵達）」。若要真正確認抵達，需要在 Temi 上用 Java App 監聽導航狀態。
- **共用帳號：** 這個 RIM2 帳號下有多台 Temi 和其他組的任務，不要修改或刪除別組的 eRoute。**`--robot rim` 和 `--go` 一定要填 `robot_name`**，沒填會拒絕派遣，避免派到別台 Temi；`--list`、`--check` 和試跑沒填時會暫時使用第一台。
- **啟動時先檢查：** 使用 `dry-run` 或 `rim` 時，程式一啟動就會登入 RIM2，並確認每個觀察點都有對應任務。名稱寫錯會立刻顯示錯誤並停止，不用等到跌倒才發現。
- **派遣失敗會直接撥打：** 例如 RIM2 連不上或 Temi 沒有回應時，會視為 Temi 無法到場，直接撥打聯絡人。

## 派遣 Temi（Java App）

RIM2 的備案。偵測到跌倒後，電腦透過 Wi-Fi 直接通知 Temi 上的 Java App，App 用 Temi SDK 前往觀察點，**抵達後回報**，不用固定等待。

1. **App 端：** 把 `temi_app/FallResponseServer.java` 加進組員的 Temi App，步驟見 [temi_app/README.md](temi_app/README.md)。
2. **確認連線：** 在 Temi 的「設定 → 開發者工具」找到 IP，執行（不會移動 Temi）：

```powershell
python -m fall_response.temi_app --url http://TemiIP:8080 --list
```

3. **框選定點：** 觀察點名稱要和 `--list` 列出的地點相同。
4. **搭配跌倒偵測（Temi 會移動）：**

```powershell
python -m fall_response.main --source 手機串流網址 --robot app --temi-app-url http://TemiIP:8080
```

啟動時會先確認所有觀察點都在 Temi 上；名稱錯誤會立刻停止。電腦和 Temi 要連同一個 Wi-Fi。

## 肢體確認模組串接方式

肢體確認由其他組員負責，目前使用模擬版本（一律視為未完成動作）。完成後在 `main.py` 的 `build_incidents()` 把 `SimulatedLayer("肢體")` 換成實作的物件，它要提供：

```python
name = "肢體"

def confirm(self, record, timeout_s: float) -> LayerResult:
    ...
```

- 長者完成動作時回傳 `LayerResult(True, {...})`，沒完成時回傳 `LayerResult(False, {...})`，系統會記錄為「未能完成肢體指令」。
- 鏡頭或程式出錯時請丟出例外，系統會記錄為「無法進行肢體確認」，不會算成長者沒做到。
- 請在 `timeout_s` 內返回。

## 常見問題

| 狀況 | 處理方式 |
|---|---|
| 找不到姿勢模型 | 回到「下載姿勢模型」執行一次 |
| 一直出現「無法連線影像來源」 | 按 `Ctrl+C` 結束，把 `--source 0` 改成 `--source 1`，或關閉正在使用鏡頭的程式 |
| 畫面顯示 `shoulders/hips NOT VISIBLE` | 肩膀或髖部沒有入鏡，把鏡頭拉遠 |
| 有摔倒但沒有觸發 | 確認全身入鏡，並看摔倒當下的 `peak descent`、`angle` 和 `FPS` |
