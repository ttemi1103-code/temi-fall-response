"""
假的 Temi：在還沒接上真正的 Temi 之前，用來測試 event_reporter.py 送出的指令。

它會：
  1. 在 port 9000 接收指令（POST /command）
  2. 印出「Temi 正在撥打給 XXX」
  3. 幾秒後把通話結果回報給 event_reporter.py（POST /call_status）

使用方式：
    python mock_temi.py                # 每通都接通
    python mock_temi.py no_answer      # 每通都沒人接（測試改打下一位、再次通知）
    python mock_temi.py first_no_answer  # 第一位沒接，第二位接通
"""

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9000
C2_URL = "http://127.0.0.1:8000/call_status"   # event_reporter.py 的位址
RING_SECONDS = 3                               # 模擬響鈴幾秒後回報結果
MODE = sys.argv[1] if len(sys.argv) > 1 else "connected"

call_count: dict[str, int] = {}


def report_result(event_id: str, contact: str):
    time.sleep(RING_SECONDS)
    n = call_count.get(event_id, 0)
    if MODE == "no_answer":
        status = "no_answer"
    elif MODE == "first_no_answer":
        status = "no_answer" if n == 1 else "connected"
    else:
        status = "connected"
    print(f"[假Temi] 「{contact}」→ {status}，回報給 C2")
    body = json.dumps({"event_id": event_id, "contact": contact, "status": status}, ensure_ascii=False)
    req = urllib.request.Request(C2_URL, data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[假Temi] C2 回應：{resp.read().decode('utf-8')}")
    except Exception as e:
        print(f"[假Temi] 回報失敗：{e}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        cmd = json.loads(self.rfile.read(length) or b"{}")
        print(f"\n[假Temi] 收到指令：{cmd}")

        if cmd.get("action") == "call_contact":
            event_id, contact = cmd.get("event_id", ""), cmd.get("contact", "")
            call_count[event_id] = call_count.get(event_id, 0) + 1
            print(f"[假Temi] 📞 正在用 Temi 視訊撥打給「{contact}」…")
            threading.Thread(target=report_result, args=(event_id, contact), daemon=True).start()
            reply = {"ok": True, "message": f"calling {contact}"}
        else:
            reply = {"ok": False, "message": "unknown action"}

        body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 關掉預設的連線紀錄，畫面比較乾淨


if __name__ == "__main__":
    print(f"假 Temi 啟動：http://127.0.0.1:{PORT}/command（模式：{MODE}）")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
