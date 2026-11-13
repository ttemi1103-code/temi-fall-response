# Temi Java App 串接說明

讓電腦上的 Python 跌倒偵測程式透過 Wi-Fi 派遣 Temi，並在 Temi 抵達時收到通知。
只要把 `FallResponseServer.java` 加進現有的 Temi App，不需要另外建立新的 App。

```text
電腦 Python ──POST /goto {"location": "vr"}──▶ Temi App ──▶ robot.goTo("vr")
電腦 Python ◀──GET /status → "complete"──────── App 監聽 SDK 的導航狀態
```

> 這份 Java 程式碼尚未在實機上編譯測試，第一次加入時若出現錯誤，請把 Android Studio 的錯誤訊息貼出來。

## 1. 加入檔案

1. 把 `FallResponseServer.java` 複製到 App 的 `app/src/main/java/你的套件路徑/` 資料夾。
2. 把檔案第一行的 `package com.example.fallresponse;` 改成 App 實際的套件名稱，和同資料夾的 `MainActivity.java` 第一行相同。

## 2. 加入網路權限

在 `AndroidManifest.xml` 的 `<manifest>` 裡、`<application>` 外面加入：

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

## 3. 在 MainActivity 啟動

在 Robot 準備好之後啟動，App 關閉時停止。以下是需要加入的部分，請合併到現有的 `MainActivity`：

```java
import com.robotemi.sdk.Robot;
import com.robotemi.sdk.listeners.OnRobotReadyListener;
import java.io.IOException;

public class MainActivity extends AppCompatActivity implements OnRobotReadyListener {
    private Robot robot;
    private FallResponseServer fallServer;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        robot = Robot.getInstance();
        fallServer = new FallResponseServer(robot);   // 預設 port 8080
    }

    @Override
    protected void onStart() {
        super.onStart();
        robot.addOnRobotReadyListener(this);
    }

    @Override
    protected void onStop() {
        super.onStop();
        robot.removeOnRobotReadyListener(this);
    }

    @Override
    public void onRobotReady(boolean isReady) {
        if (isReady) {
            try {
                fallServer.start();
            } catch (IOException e) {
                Log.e("MainActivity", "跌倒派遣服務啟動失敗", e);
            }
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        fallServer.stop();
    }
}
```

如果 `MainActivity` 已經實作 `OnRobotReadyListener`，只要在原本的 `onRobotReady` 裡加上 `fallServer.start()` 即可。

**服務要在 App 被關閉前一直存在。** App 放到背景或被其他 App 取代時，電腦就連不到。

## 4. 找到 Temi 的 IP

在 Temi 的「設定 → 開發者工具」可以看到 Temi 的 Wi-Fi IP，例如 `192.168.50.20`。電腦和 Temi 要連同一個 Wi-Fi。

## 5. 從電腦測試

以下指令在 `temi-fall-response` 資料夾執行，先啟用虛擬環境。

列出 Temi 上的地點（不會移動）：

```powershell
python -m fall_response.temi_app --url http://TemiIP:8080 --list
```

派遣並等待抵達（**Temi 會移動**）：

```powershell
python -m fall_response.temi_app --url http://TemiIP:8080 --go vr
```

成功時會顯示「已抵達，花了 X 秒」。也可以用瀏覽器開 `http://TemiIP:8080/locations`、`http://TemiIP:8080/status` 檢查。

## 通訊格式

| 請求 | 內容 | 回傳 |
|---|---|---|
| `GET /locations` | 無 | `{"locations": ["vr", "zone2"]}` |
| `POST /goto` | `{"location": "vr"}` | `{"request_id": "3", "location": "vr"}`；地點不存在時回 404 |
| `GET /status` | 無 | `{"request_id": "3", "location": "vr", "status": "going", "description": "", "updated_ms": 0}` |
| `POST /stop` | 無 | `{"ok": true}` |

`status` 是 Temi SDK 的導航狀態：`requested`（已收到、尚未開始）、`start`、`calculating`、`going`、`complete`（已抵達）、`abort`（中止）、`reposing`。

## 常見問題

| 狀況 | 處理方式 |
|---|---|
| 電腦顯示「無法連線 Temi App」 | 確認 App 在前景、電腦和 Temi 在同一個 Wi-Fi、IP 正確 |
| `Temi 上沒有地點` | 地點名稱要和 Temi 上存的一樣；Temi 會把英文轉成小寫 |
| 已送出但 Temi 不動 | 確認 Temi 目前的地圖有這個地點，且 Temi 沒有在執行其他任務 |
| 一直沒有 `complete` | 看 Android Studio 的 Logcat，搜尋 `FallResponseServer` 的導航狀態紀錄 |
