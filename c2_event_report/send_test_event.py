"""
模擬 A 組、B 組、C1 分別把資料傳給 event_reporter.py。

使用方式（先開好 event_reporter.py server 和 mock_temi.py）：
    python send_test_event.py            # 有語音：喊痛、腳不能動、點頭 → 傳摘要，等聯絡人「已查看」
    python send_test_event.py safe       # 有語音：說沒事、手腳都能動、搖頭 → 傳摘要，等聯絡人「已查看」
    python send_test_event.py silent     # 無語音回應 → 不做肢體確認，直接請 Temi 撥打

有語音的情境：在時限內打開終端機印出的「已查看」連結，就不會撥打；不點就會由 Temi 撥打。
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
    return result


print(f"事件編號：{event_id}（情境：{case}）")
post("/fall", {"fall_time": datetime.now().isoformat(timespec="seconds"), "location": "客廳"})

if case == "silent":
    r = post("/speech", {"responded": False, "language": "zh-TW"})
else:
    if case == "safe":
        post("/speech", {"responded": True, "text": "無代誌啦", "language": "nan-TW", "llm_intent": "ok"})
        r = post("/gesture", {"arms": "both", "leg_moved": True, "call_consent": "shake_head"})
    else:
        post("/speech", {"responded": True, "text": "我腳好痛，起不來", "language": "zh-TW",
                         "llm_intent": "need_help"})
        r = post("/gesture", {"arms": "both", "leg_moved": False, "call_consent": "nod"})
    if r.get("action") == "wait_ack":
        print(f"\n要模擬聯絡人「已查看」，請在時限內用瀏覽器打開：\n{C2}/ack?event_id={event_id}")
