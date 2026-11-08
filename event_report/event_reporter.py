"""
第三階段：事件資訊整合與緊急通知系統（C2）

流程：
    A 組（跌倒時間、位置） ─┐
    B 組（語音結果）       ─┼→ 接收資料 → 判斷狀態 → 產生摘要 → 通知緊急聯絡人 → 必要時請 Temi 撥視訊
    C1（肢體辨識結果）     ─┘

使用方式：
    python event_reporter.py demo      # 跑內建情境測試
    python event_reporter.py server    # 啟動 HTTP 伺服器，讓 A / B / C1 用 POST 傳資料

HTTP 介面（JSON，同一事件用同一個 event_id 串起來）：
    POST /fall     {"event_id": "...", "fall_time": "2026-11-08T14:32:10", "location": "客廳", "confidence": 0.92}
    POST /speech   {"event_id": "...", "text": "我腳好痛起不來", "language": "zh-TW", "llm_intent": "need_help", "llm_summary": "..."}
    POST /gesture  {"event_id": "...", "gestures": ["raise_hand", "nod"]}
    POST /event    三者合併成一包也可以，直接處理
    POST /call_status  Temi 回報視訊結果 {"event_id": "...", "contact": "女兒", "status": "connected" | "no_answer"}

需要視訊時，本程式會送指令給 Temi（TEMI_URL）：
    {"action": "call_contact", "contact": "女兒", "event_id": "..."}
沒有 Temi 時可先執行 mock_temi.py 模擬。

只用 Python 標準函式庫，不需額外安裝套件。
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

# ============================================================
# 設定（可用環境變數覆蓋）
# ============================================================
CONFIG = {
    # 跌倒後等待 B、C1 結果的最長秒數，超過就用現有資料直接判斷
    "WAIT_TIMEOUT_SEC": int(os.getenv("WAIT_TIMEOUT_SEC", "60")),
    # 跌倒超過幾分鐘未處理，狀態升級
    "LONG_LIE_MINUTES": int(os.getenv("LONG_LIE_MINUTES", "5")),
    # 緊急聯絡人
    "CONTACT_NAME": os.getenv("CONTACT_NAME", "家屬"),
    "CONTACT_EMAIL": os.getenv("CONTACT_EMAIL", ""),
    # Email（Gmail 需用「應用程式密碼」）
    "SMTP_HOST": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "SMTP_PORT": int(os.getenv("SMTP_PORT", "587")),
    "SMTP_USER": os.getenv("SMTP_USER", ""),
    "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
    # Telegram Bot（選用）
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    # 遠端視訊：送「打電話」指令給 Temi，由 Temi 內建視訊通話打給聯絡人
    #   TEMI_URL      ：Temi App 接收指令的網址（測試時是 mock_temi.py）
    #   TEMI_CONTACTS ：要撥打的聯絡人（Temi 裡的名稱），用逗號分隔；第一位沒接就打下一位
    "TEMI_URL": os.getenv("TEMI_URL", "http://127.0.0.1:9000/command"),
    "TEMI_CONTACTS": [c.strip() for c in os.getenv("TEMI_CONTACTS", "女兒,兒子").split(",") if c.strip()],
    # HTTP 伺服器
    "HOST": os.getenv("HOST", "0.0.0.0"),
    "PORT": int(os.getenv("PORT", "8000")),
}


# ============================================================
# 資料結構
# ============================================================
class Status(Enum):
    EMERGENCY = "緊急"   # 立即通知 + 啟動視訊
    WARNING = "需關注"   # 通知家屬，由家屬決定是否視訊
    SAFE = "安全"        # 通知家屬「有跌倒但本人表示沒事」


@dataclass
class FallInfo:                      # 來自 A 組
    fall_time: datetime
    location: str
    confidence: float = 1.0


@dataclass
class SpeechInfo:                    # 來自 B 組
    text: str = ""
    language: str = ""               # zh-TW / nan-TW（台語）等
    llm_intent: str = ""             # need_help / ok / unknown（B 組 LLM 分析結果）
    llm_summary: str = ""


@dataclass
class GestureInfo:                   # 來自 C1
    gestures: list[str] = field(default_factory=list)   # raise_hand / nod / shake_head


@dataclass
class Incident:
    event_id: str
    fall: Optional[FallInfo] = None
    speech: Optional[SpeechInfo] = None
    gesture: Optional[GestureInfo] = None
    processed: bool = False


@dataclass
class Assessment:
    status: Status
    reasons: list[str]
    need_video: bool


# ============================================================
# 1. 接收資料：把 JSON 轉成資料結構
# ============================================================
def parse_time(value) -> datetime:
    if not value:
        return datetime.now()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    return datetime.fromisoformat(str(value))


def parse_fall(d: dict) -> FallInfo:
    return FallInfo(
        fall_time=parse_time(d.get("fall_time")),
        location=d.get("location", "未知位置"),
        confidence=float(d.get("confidence", 1.0)),
    )


def parse_speech(d: dict) -> SpeechInfo:
    return SpeechInfo(
        text=d.get("text", "") or "",
        language=d.get("language", "") or "",
        llm_intent=(d.get("llm_intent", "") or "").lower(),
        llm_summary=d.get("llm_summary", "") or "",
    )


def parse_gesture(d: dict) -> GestureInfo:
    g = d.get("gestures", [])
    if isinstance(g, str):
        g = [g]
    return GestureInfo(gestures=[x.lower() for x in g])


# ============================================================
# 2. 判斷狀態
# ============================================================
# 國語＋台語常見求救 / 沒事關鍵字（LLM 沒給結果時當備援）
HELP_KEYWORDS = [
    "救命", "救我", "幫忙", "幫我", "好痛", "很痛", "痛", "起不來", "站不起來", "動不了",
    "頭暈", "暈", "流血", "喘不過", "胸口", "不舒服", "叫救護車",
    "救人", "足痛", "真痛", "袂起來", "起袂來", "頭殼眩", "艱苦", "鬥相共",
]
OK_KEYWORDS = [
    "沒事", "沒關係", "我很好", "還好", "不用", "可以自己起來",
    "無代誌", "無要緊", "免啦", "我好好",
]
SEVERE_KEYWORDS = ["流血", "胸口", "喘不過", "叫救護車", "動不了", "暈"]

GESTURE_TEXT = {
    "raise_hand": "舉手（求助）",
    "nod": "點頭（需要幫忙）",
    "shake_head": "搖頭（不需要幫忙）",
}


def analyze_speech(s: Optional[SpeechInfo]) -> str:
    """回傳 need_help / severe / ok / none"""
    if s is None or (not s.text.strip() and not s.llm_intent):
        return "none"
    text = s.text
    if any(k in text for k in SEVERE_KEYWORDS):
        return "severe"
    if s.llm_intent == "need_help":
        return "need_help"
    if s.llm_intent == "ok":
        return "ok"
    # LLM 沒給明確結果 → 關鍵字備援（先判斷沒事，避免「不用」被「痛」誤判時可調整）
    help_hit = any(k in text for k in HELP_KEYWORDS)
    ok_hit = any(k in text for k in OK_KEYWORDS)
    if help_hit:
        return "need_help"
    if ok_hit:
        return "ok"
    return "unclear"


def analyze_gesture(g: Optional[GestureInfo]) -> str:
    """回傳 need_help / ok / none（Temi 問：「你需要幫忙嗎？」）"""
    if g is None or not g.gestures:
        return "none"
    if "raise_hand" in g.gestures or "nod" in g.gestures:
        return "need_help"
    if "shake_head" in g.gestures:
        return "ok"
    return "none"


def assess(inc: Incident, now: Optional[datetime] = None) -> Assessment:
    now = now or datetime.now()
    reasons: list[str] = []
    sp = analyze_speech(inc.speech)
    ge = analyze_gesture(inc.gesture)

    # 規則依嚴重度由高到低
    if sp == "severe":
        status = Status.EMERGENCY
        reasons.append("語音中出現嚴重症狀關鍵字")
    elif sp == "none" and ge == "none":
        status = Status.EMERGENCY
        reasons.append("跌倒者對 Temi 無語音也無肢體回應，可能失去意識")
    elif sp == "need_help" and ge == "need_help":
        status = Status.EMERGENCY
        reasons.append("語音與肢體皆表示需要幫忙")
    elif sp == "need_help" or ge == "need_help":
        status = Status.WARNING
        reasons.append("語音或肢體其中一項表示需要幫忙")
    elif sp == "ok" and ge in ("ok", "none") or ge == "ok" and sp in ("ok", "none"):
        status = Status.SAFE
        reasons.append("跌倒者表示沒事")
    else:
        status = Status.WARNING
        reasons.append("回應不明確，建議家屬確認")

    # 躺太久升級
    if inc.fall:
        lying_min = (now - inc.fall.fall_time).total_seconds() / 60
        if lying_min >= CONFIG["LONG_LIE_MINUTES"] and status != Status.EMERGENCY:
            reasons.append(f"跌倒已超過 {int(lying_min)} 分鐘")
            status = Status.EMERGENCY if status == Status.WARNING else Status.WARNING

    return Assessment(status=status, reasons=reasons, need_video=(status == Status.EMERGENCY))


# ============================================================
# 3. 產生事件摘要
# ============================================================
def build_summary(inc: Incident, a: Assessment, call_contact: str = "") -> str:
    icon = {Status.EMERGENCY: "🚨", Status.WARNING: "⚠️", Status.SAFE: "ℹ️"}[a.status]
    lines = [f"{icon}【跌倒事件通知｜{a.status.value}】"]

    if inc.fall:
        lines.append(f"時間：{inc.fall.fall_time:%Y-%m-%d %H:%M:%S}")
        lines.append(f"位置：{inc.fall.location}")
    else:
        lines.append("時間 / 位置：未取得（A 組資料缺漏）")

    if inc.speech and inc.speech.text:
        lang = {"nan-TW": "台語", "zh-TW": "國語"}.get(inc.speech.language, inc.speech.language)
        lines.append(f"語音回應：「{inc.speech.text}」" + (f"（{lang}）" if lang else ""))
        if inc.speech.llm_summary:
            lines.append(f"語音分析：{inc.speech.llm_summary}")
    else:
        lines.append("語音回應：無")

    if inc.gesture and inc.gesture.gestures:
        lines.append("肢體回應：" + "、".join(GESTURE_TEXT.get(g, g) for g in inc.gesture.gestures))
    else:
        lines.append("肢體回應：無")

    lines.append("判斷依據：" + "；".join(a.reasons))
    advice = {
        Status.EMERGENCY: "請立即確認狀況，必要時撥打 119。",
        Status.WARNING: "請盡快聯繫長者確認狀況。",
        Status.SAFE: "長者表示無大礙，仍建議稍後關心。",
    }[a.status]
    lines.append(f"建議：{advice}")
    if call_contact:
        lines.append(f"視訊通話：Temi 將撥打視訊給「{call_contact}」，請留意來電")
    lines.append(f"事件編號：{inc.event_id}")
    return "\n".join(lines)


# ============================================================
# 4. 通知緊急聯絡人（多管道，有設定才送）
# ============================================================
def notify_console(subject: str, body: str) -> bool:
    print("=" * 50)
    print(f"[通知] 給 {CONFIG['CONTACT_NAME']}｜{subject}")
    print(body)
    print("=" * 50)
    return True


def notify_email(subject: str, body: str) -> bool:
    if not (CONFIG["SMTP_USER"] and CONFIG["SMTP_PASSWORD"] and CONFIG["CONTACT_EMAIL"]):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = CONFIG["SMTP_USER"]
    msg["To"] = CONFIG["CONTACT_EMAIL"]
    try:
        with smtplib.SMTP(CONFIG["SMTP_HOST"], CONFIG["SMTP_PORT"], timeout=10) as s:
            s.starttls()
            s.login(CONFIG["SMTP_USER"], CONFIG["SMTP_PASSWORD"])
            s.send_message(msg)
        print("[通知] Email 已送出")
        return True
    except Exception as e:
        print(f"[通知] Email 失敗：{e}")
        return False


def notify_telegram(subject: str, body: str) -> bool:
    token, chat_id = CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"]
    if not (token and chat_id):
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": body}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10)
        print("[通知] Telegram 已送出")
        return True
    except Exception as e:
        print(f"[通知] Telegram 失敗：{e}")
        return False


NOTIFIERS = [notify_console, notify_email, notify_telegram]


def notify_contacts(subject: str, body: str) -> None:
    for fn in NOTIFIERS:
        fn(subject, body)


# ============================================================
# 5. 遠端視訊：送「打電話」指令給 Temi
# ============================================================
# Temi App 需要做的事（Android 端）：
#   1. 開一個接收指令的介面（例如 HTTP POST /command）
#   2. 收到 {"action": "call_contact", "contact": "女兒", "event_id": "..."} 後，
#      從 temi SDK 的聯絡人清單找到對應的人，用 temi 內建視訊通話撥打
#   3. 通話結果回報給本程式：POST http://<C2電腦IP>:8000/call_status
#      {"event_id": "...", "contact": "女兒", "status": "connected" | "no_answer" | "rejected"}

# 記錄每個事件撥到第幾位聯絡人：{event_id: {"index": 0, "summary": "..."}}
CALLS: dict[str, dict] = {}


def send_to_temi(command: dict) -> bool:
    data = json.dumps(command, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        CONFIG["TEMI_URL"], data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[Temi] 指令已送出：{command}（回應 {resp.status}）")
            return True
    except Exception as e:
        print(f"[Temi] 指令送出失敗（Temi 沒開或網路不通）：{e}")
        return False


def call_contact_via_temi(event_id: str, index: int) -> str:
    """請 Temi 撥給第 index 位聯絡人，成功送出回傳聯絡人名稱，失敗回傳空字串"""
    contacts = CONFIG["TEMI_CONTACTS"]
    if index >= len(contacts):
        return ""
    contact = contacts[index]
    ok = send_to_temi({"action": "call_contact", "contact": contact, "event_id": event_id})
    return contact if ok else ""


def handle_call_status(d: dict) -> dict:
    """Temi 回報通話結果；沒接通就改打下一位，全部沒接通就再發一次文字通知"""
    event_id = d.get("event_id", "")
    status = d.get("status", "")
    contact = d.get("contact", "")
    rec = CALLS.get(event_id)
    if rec is None:
        return {"event_id": event_id, "message": "找不到這個事件"}

    if status == "connected":
        print(f"[Temi] 事件 {event_id}：已與「{contact}」接通視訊")
        rec["done"] = True
        return {"event_id": event_id, "message": "已接通"}

    print(f"[Temi] 事件 {event_id}：「{contact}」未接通（{status}）")
    rec["index"] += 1
    next_contact = call_contact_via_temi(event_id, rec["index"])
    if next_contact:
        return {"event_id": event_id, "message": f"改撥給 {next_contact}"}

    rec["done"] = True
    notify_contacts(
        "⚠️ 跌倒事件：Temi 視訊無人接聽",
        rec["summary"] + "\n\n‼️ Temi 視訊通話所有聯絡人皆未接通，請立即回電或撥打 119。",
    )
    return {"event_id": event_id, "message": "所有聯絡人都未接通，已再次通知"}


# ============================================================
# 主流程：整合 → 判斷 → 摘要 → 通知 → 視訊
# ============================================================
def process_incident(inc: Incident) -> dict:
    a = assess(inc)
    first_contact = CONFIG["TEMI_CONTACTS"][0] if (a.need_video and CONFIG["TEMI_CONTACTS"]) else ""
    summary = build_summary(inc, a, first_contact)
    # 先送文字摘要，讓家屬接電話前就知道狀況
    notify_contacts(f"跌倒事件通知（{a.status.value}）", summary)
    called = ""
    if first_contact:
        CALLS[inc.event_id] = {"index": 0, "summary": summary, "done": False}
        called = call_contact_via_temi(inc.event_id, 0)
        if not called:
            notify_contacts("⚠️ 跌倒事件：Temi 無法撥打視訊",
                            summary + "\n\n‼️ Temi 無法撥出視訊，請立即自行聯繫長者或撥打 119。")
    inc.processed = True
    return {
        "event_id": inc.event_id,
        "status": a.status.name,
        "reasons": a.reasons,
        "temi_call": called,
        "summary": summary,
    }


def process_payload(payload: dict) -> dict:
    """一次收到完整資料時使用：{"fall": {...}, "speech": {...}, "gesture": {...}}"""
    inc = Incident(event_id=payload.get("event_id") or uuid.uuid4().hex[:8])
    if payload.get("fall"):
        inc.fall = parse_fall(payload["fall"])
    if payload.get("speech"):
        inc.speech = parse_speech(payload["speech"])
    if payload.get("gesture"):
        inc.gesture = parse_gesture(payload["gesture"])
    return process_incident(inc)


# ============================================================
# 事件彙整器：A / B / C1 分開送資料時，用 event_id 湊齊
# ============================================================
class IncidentAggregator:
    def __init__(self):
        self.incidents: dict[str, Incident] = {}
        self.lock = threading.Lock()

    def _get(self, event_id: str) -> Incident:
        if event_id not in self.incidents:
            self.incidents[event_id] = Incident(event_id=event_id)
        return self.incidents[event_id]

    def add(self, kind: str, d: dict) -> dict:
        event_id = d.get("event_id") or uuid.uuid4().hex[:8]
        with self.lock:
            inc = self._get(event_id)
            if inc.processed:
                return {"event_id": event_id, "message": "事件已處理"}
            if kind == "fall":
                inc.fall = parse_fall(d)
                # 收到跌倒後開始倒數，逾時就用現有資料判斷（例如長者完全沒回應）
                threading.Timer(CONFIG["WAIT_TIMEOUT_SEC"], self._timeout, args=[event_id]).start()
            elif kind == "speech":
                inc.speech = parse_speech(d)
            elif kind == "gesture":
                inc.gesture = parse_gesture(d)
            ready = inc.fall and inc.speech and inc.gesture
        if ready:
            return self._finish(event_id)
        return {"event_id": event_id, "message": f"已收到 {kind}，等待其他資料"}

    def _timeout(self, event_id: str):
        with self.lock:
            inc = self.incidents.get(event_id)
            if not inc or inc.processed:
                return
        print(f"[彙整] 事件 {event_id} 等待逾時，以現有資料判斷")
        self._finish(event_id)

    def _finish(self, event_id: str) -> dict:
        with self.lock:
            inc = self.incidents[event_id]
            if inc.processed:
                return {"event_id": event_id, "message": "事件已處理"}
            inc.processed = True
        return process_incident(inc)


# ============================================================
# HTTP 伺服器（給 A 組、B 組、C1 呼叫）
# ============================================================
aggregator = IncidentAggregator()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            path = self.path.rstrip("/")
            if path == "/event":
                self._reply(200, process_payload(data))
            elif path == "/call_status":
                self._reply(200, handle_call_status(data))
            elif path in ("/fall", "/speech", "/gesture"):
                self._reply(200, aggregator.add(path[1:], data))
            else:
                self._reply(404, {"error": "unknown path"})
        except Exception as e:
            self._reply(400, {"error": str(e)})


def run_server():
    srv = ThreadingHTTPServer((CONFIG["HOST"], CONFIG["PORT"]), Handler)
    print(f"事件整合伺服器啟動：http://{CONFIG['HOST']}:{CONFIG['PORT']}")
    srv.serve_forever()


# ============================================================
# 內建測試情境
# ============================================================
def run_demo():
    now = datetime.now().isoformat(timespec="seconds")
    cases = {
        "情境1：喊痛＋舉手": {
            "fall": {"fall_time": now, "location": "客廳沙發旁"},
            "speech": {"text": "我腳好痛，起不來", "language": "zh-TW", "llm_intent": "need_help",
                       "llm_summary": "長者表示腳部疼痛、無法自行站起"},
            "gesture": {"gestures": ["raise_hand"]},
        },
        "情境2：台語說沒事＋搖頭": {
            "fall": {"fall_time": now, "location": "臥室"},
            "speech": {"text": "無代誌啦，我家己會起來", "language": "nan-TW"},
            "gesture": {"gestures": ["shake_head"]},
        },
        "情境3：完全沒回應": {
            "fall": {"fall_time": now, "location": "浴室門口"},
        },
        "情境4：只點頭、沒說話": {
            "fall": {"fall_time": now, "location": "廚房"},
            "gesture": {"gestures": ["nod"]},
        },
    }
    for name, payload in cases.items():
        print(f"\n######## {name} ########")
        r = process_payload(payload)
        print(f"→ 結果：{r['status']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    run_server() if mode == "server" else run_demo()
