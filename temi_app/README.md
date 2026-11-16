# Temi Java App 串接說明

讓電腦上的 Python 跌倒偵測程式透過 Wi-Fi 派遣 Temi，並在 Temi 抵達時收到通知。

```text
電腦 Python ──POST /goto {"location": "vr"}──▶ Temi App ──▶ robot.goTo("vr")
電腦 Python ◀──GET /status → "complete"──────── App 監聽 SDK 的導航狀態
```

> 這份 Java 程式碼尚未編譯測試，第一次加入時若出現錯誤，請把 Android Studio 的錯誤訊息貼出來。

## 檔案

| 檔案 | 功能 |
|---|---|
| `FallResponseServer.java` | 和電腦溝通的 HTTP 服務（port 8080），收到派遣就呼叫 `robot.goTo()`，並記錄 SDK 回報的導航狀態 |
| `MainActivity.java` | App 畫面：Temi 準備好後啟動服務，顯示電腦要連的網址和 Temi 上的地點 |

## 1. 建立專案並加入檔案

用架好 Temi 開發環境的那台電腦操作。

步驟和課程的「temi 程式教學投影片」第 2～3 章相同，詳細畫面可以對照投影片。

1. Android Studio 建立新專案（Empty Activity、語言選 Java、Minimum SDK 選 **API 23：Android 6.0**，因為 Temi 是 Android 6.0），或使用現有的 Temi 專案。專案名稱和路徑只能用英文和數字。
2. 在 `app/build.gradle` 的 `dependencies` 加入 Temi SDK，再按 Sync Now（投影片用的是 0.10.75，現有專案若用別的版本就照現有的）：
   ```gradle
   implementation 'com.robotemi:sdk:0.10.75'
   ```
3. 把這個資料夾的 2 個 `.java` 檔複製到 `app/src/main/java/你的套件路徑/`。新專案自動產生的 `MainActivity.java` 用這裡的取代。
4. 把兩個檔案第一行的 `package com.example.fallresponse;` 改成專案實際的套件名稱。
5. 如果現有專案已經有自己的 `MainActivity`，就不要取代，把 `onRobotReady()` 和 `startServer()` 的內容合併進去。

## 2. 修改 AndroidManifest.xml

在 `<manifest>` 裡、`<application>` 外面加入網路權限：

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

在 `<application>` 裡加入下面這段，Temi 才會把這個 App 當成 Temi App：

```xml
<meta-data
    android:name="com.robotemi.sdk.metadata.SKILL"
    android:value="@string/app_name" />
```

## 3. 安裝到 Temi

1. Temi 沒有 USB，要用 Wi-Fi 安裝（投影片第 3 章）：
   - 在 Temi 的「設定 → 開發人員工具」按「打開 ADB Port」，記下右上角的 IP。
   - 電腦執行 `adb connect TemiIP`，顯示 `connected to TemiIP:5555` 就成功；一次只能有一台電腦連。
   - Android Studio 的裝置選單會變成 `rockchip rk3288`，按綠色三角形執行，就會安裝到 Temi 上。
2. Temi 準備好後，畫面會顯示「跌倒派遣服務已啟動」、電腦連線網址（例如 `http://192.168.50.20:8080`）和 Temi 上的地點。
3. 電腦和 Temi 要連同一個 Wi-Fi。

**App 要一直開在前景。** App 放到背景或被其他 App 取代時，電腦就連不到。

## 4. 實驗室測試步驟

以下指令在 `temi-fall-response` 資料夾執行，先啟用虛擬環境。`TemiIP` 換成 App 畫面上的 IP。由簡單到完整，前一步成功再做下一步。

| 步驟 | 指令 | 成功時 | Temi 會動嗎 |
|---|---|---|---|
| 1. 確認連得上 | 瀏覽器開 `http://TemiIP:8080/locations` | 顯示 Temi 上的地點 | 不會 |
| 2. 列出地點 | `python -m fall_response.temi_app --url http://TemiIP:8080 --list` | 列出地點，名稱要和 `zones.json` 的觀察點一致 | 不會 |
| 3. 派遣一個點 | `python -m fall_response.temi_app --url http://TemiIP:8080 --go vr` | Temi 走到 vr，電腦顯示「已抵達，花了 X 秒」 | **會** |
| 4. 中途停止 | 步驟 3 走到一半時，另開視窗執行 `python -m fall_response.temi_app --url http://TemiIP:8080 --stop` | Temi 停下，步驟 3 顯示「導航中止」 | **會** |
| 5. 完整流程 | `python -m fall_response.main --source 手機串流網址 --robot app --temi-app-url http://TemiIP:8080` | 在區域內跌倒後，Temi 走到該區域的觀察點，log 出現「Temi 已抵達」 | **會** |

步驟 3 之後請把電腦畫面上的 log 拍下來，特別是「Temi 導航狀態」那幾行，可以確認 Temi 實際回報的狀態順序。步驟 4 如果沒有顯示「導航中止」也記下來，代表 Temi 停止時回報的狀態和預期不同。

## 通訊格式

| 請求 | 內容 | 回傳 |
|---|---|---|
| `GET /locations` | 無 | `{"locations": ["vr", "zone2"]}` |
| `POST /goto` | `{"location": "vr"}` | `{"request_id": "3", "location": "vr"}`；地點不存在時回 404 |
| `GET /status` | 無 | `{"request_id": "3", "location": "vr", "status": "going", "description": "", "updated_ms": 0}` |
| `POST /stop` | 無 | `{"ok": true}` |

`status` 是 Temi SDK 的導航狀態：`requested`（已收到、尚未開始）、`start`、`calculating`、`going`、`complete`（已抵達）、`abort`（中止）、`reposing`。

修改通訊格式時，Java（`FallResponseServer.java`）和 Python（`fall_response/temi_app.py`）要一起改。

## 常見問題

| 狀況 | 處理方式 |
|---|---|
| App 一直顯示「等待 Temi 準備好」 | 確認 App 是裝在 Temi 上，且 `AndroidManifest.xml` 有加 `com.robotemi.sdk.metadata.SKILL` |
| 電腦顯示「無法連線 Temi App」 | 確認 App 在前景、電腦和 Temi 在同一個 Wi-Fi、IP 正確 |
| `Temi 上沒有地點` | 地點名稱要和 Temi 上存的一樣；Temi 會把英文轉成小寫 |
| 已送出但 Temi 不動 | 確認 Temi 目前的地圖有這個地點，且 Temi 沒有在執行其他任務 |
| 一直沒有 `complete` | 看 Android Studio 的 Logcat，搜尋 `FallResponseServer` 的導航狀態紀錄 |
