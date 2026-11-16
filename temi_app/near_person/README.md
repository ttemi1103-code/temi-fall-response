# 派 Temi 到傷患旁（未來版本）

原本的版本只能派 Temi 到事先存好的幾個觀察點。這個版本依跌倒位置算出傷患在 Temi 地圖上的座標，派 Temi 停在傷患旁約 1 公尺、面向傷患的地方。

> **先用 `temi_app/` 原本的版本測試成功，再換成這個版本。** 這份程式尚未編譯，也還沒在實機測過。

## 和原本版本的差別

只有 `FallResponseServer.java` 不同，`MainActivity.java` 不用改。多了兩個功能：

| 請求 | 內容 | 回傳 |
|---|---|---|
| `GET /position` | 無 | `{"x": 1.2, "y": 3.4, "yaw": 0.5, "tilt": 0}`：Temi 目前的地圖座標（公尺），`yaw` 為弧度 |
| `POST /goto_position` | `{"x": 1.2, "y": 3.4, "yaw": 0.5}` | `{"request_id": "4", "location": "(1.20, 3.40)"}`，之後一樣用 `GET /status` 等待 `complete` |

原本的 `/locations`、`/goto`、`/status`、`/stop` 都保留。

## 換成這個版本

1. 用這個資料夾的 `FallResponseServer.java` 取代專案裡原本的檔案，第一行 `package` 改成專案的套件名稱。
2. 重新安裝到 Temi。
3. 如果 `Position` 或 `OnCurrentPositionChangedListener` 出現紅字，代表這版 SDK 的套件路徑不同：把游標放在紅字上按 `Alt + Enter`，選 Import class，Android Studio 會自動改成正確的路徑。

## 實驗室測試步驟

以下指令在 `temi-fall-response` 資料夾執行，先啟用虛擬環境。前一步成功再做下一步。

| 步驟 | 指令 | 成功時 | Temi 會動嗎 |
|---|---|---|---|
| 1. 讀 Temi 座標 | `python -m fall_response.temi_app --url http://TemiIP:8080 --position` | 顯示 Temi 目前的 x、y；推 Temi 換個位置再查一次，數字要跟著變 | 不會 |
| 2. 依座標移動 | 先查一次座標，再執行 `python -m fall_response.temi_app --url http://TemiIP:8080 --go-xy 查到的x+1,查到的y,0` | Temi 往前約 1 公尺，電腦顯示「已抵達」 | **會** |
| 3. 量地板座標 | `python -m fall_response.floor_map --source 手機串流網址 --temi-app-url http://TemiIP:8080` | 量好 4～6 個點，畫面顯示換算誤差 | 要手動推或遙控 Temi |
| 4. 完整流程 | `python -m fall_response.main --source 手機串流網址 --robot app --temi-app-url http://TemiIP:8080 --near-person` | 跌倒後 Temi 停在傷患旁、面向傷患 | **會** |

### 步驟 3：量地板座標

1. 在地板上貼 4～6 個標記，分散在鏡頭看得到的地板四個角落，不要排成一直線。
2. 把 Temi 停在第一個標記上。
3. 在影像視窗用滑鼠**左鍵**點 Temi 底部的中心，程式會自動向 Temi 讀取目前座標，記成一組點並存到 `floor_map.json`。
4. 換下一個標記，重複步驟 2、3。
5. 量到第 5 個點以後，終端機會顯示換算誤差。平均誤差最好在 0.3 公尺以內，太大就按 `z` 移除量錯的點重量。
6. 用滑鼠**右鍵**點地板任一處，會顯示換算後的地圖座標，可以把 Temi 推過去用步驟 1 對照。

鏡頭移動後要重新量。

## 需要在實機確認的事

- `/position` 回傳的 `yaw` 是不是弧度；如果 Temi 停下後面向的方向不對，就是單位或方向不同。
- 依座標移動時，`OnGoToLocationStatusChangedListener` 是否一樣會回報 `going`、`complete`；如果一直沒有 `complete`，請看 Logcat 搜尋 `FallResponseServer`。
- Temi 走不到傷患旁時（例如被家具擋住）會回報 `abort`，電腦會在剩下的時間內改去該區域的觀察點。
