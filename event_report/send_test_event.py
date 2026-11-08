"""
模擬 A 組、B 組、C1 分別把資料傳給 event_reporter.py。

使用方式（先開好 event_reporter.py server 和 mock_temi.py）：
    python send_test_event.py            # 緊急情境：喊痛＋舉手 → 會請 Temi 撥視訊
    python send_test_event.py safe       # 安全情境：說沒事＋搖頭 → 只發文字通知
"""

import json
import sys
import urllib.request
import uuid
from datetime import datetime

C2 = "http://127.0.0.1:8000"
case = sys.argv[1] if len(sys.argv) > 1 else "emergency"
event_id = uuid.uuid4().hex[:8]


def post(path: str, data: dict):
    data["event_id"] = event_id
    req = urllib.request.Request(C2 + path, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    print(f"{path:9} → {result.get('message') or result.get('status')}")


print(f"事件編號：{event_id}（情境：{case}）")
post("/fall", {"fall_time": datetime.now().isoformat(timespec="seconds"), "location": "客廳"})

if case == "safe":
    post("/speech", {"text": "無代誌啦", "language": "nan-TW", "llm_intent": "ok"})
    post("/gesture", {"gestures": ["shake_head"]})
else:
    post("/speech", {"text": "我腳好痛，起不來", "language": "zh-TW", "llm_intent": "need_help"})
    post("/gesture", {"gestures": ["raise_hand"]})
