"""
第三階段：事件資訊整合與緊急通知系統（C2）

流程（依專題流程圖）：
    語音互動 ─ 有語音回應 → 肢體反應確認（C1）→ 整理語音＋肢體資訊 → 傳送事件摘要給緊急聯絡人
             │                                                      ├ 時限內按「已查看」→ 由聯絡人決定後續
             │                                                      └ 時限內沒查看       → Temi 主動撥打聯絡人
             └ 無語音回應 ─────────────────────────────────────────→ 直接請 Temi 撥打聯絡人（同時傳送摘要）

使用方式：
    python event_reporter.py demo      # 跑內建情境測試（約 5 秒，會示範「已查看」和「未查看」兩種結果）
    python event_reporter.py server    # 啟動 HTTP 伺服器，讓 A / B / C1 / Temi 傳資料

HTTP 介面（JSON，同一事件用同一個 event_id 串起來）：
    POST /fall     A 組 {"event_id": "...", "fall_time": "2026-11-08T14:32:10", "location": "客廳"}
    POST /speech   B 組 {"event_id": "...", "responded": true, "text": "我腳好痛", "language": "zh-TW",
                         "llm_intent": "need_help", "llm_summary": "..."}
    POST /gesture  C1   {"event_id": "...", "arms": "both", "leg_moved": true, "call_consent": "nod"}
    POST /event    三者合併成一包：{"fall": {...}, "speech": {...}, "gesture": {...}}
    POST /call_status  Temi 回報通話結果 {"event_id": "...", "contact": "女兒", "status": "connected" | "no_answer"}
    GET  /ack?event_id=...  聯絡人點摘要裡的連結，表示「已查看」

需要撥打時，本程式會送指令給 Temi（TEMI_URL）：
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
import time
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
    # 跌倒後等待 B、C1 結果的最長秒數，超過就用現有資料直接處理（沒收到語音視為無語音回應）
    "WAIT_TIMEOUT_SEC": int(os.getenv("WAIT_TIMEOUT_SEC", "60")),
    # 傳送摘要後，等聯絡人按「已查看」的秒數；超過就請 Temi 主動撥打
    "ACK_TIMEOUT_SEC": int(os.getenv("ACK_TIMEOUT_SEC", "120")),
    # 跌倒超過幾分鐘，緊急程度升級
    "LONG_LIE_MINUTES": int(os.getenv("LONG_LIE_MINUTES", "5")),
    # 摘要裡「已查看」連結的網址開頭；聯絡人手機要連得到，請改成 C2 電腦的區網 IP
    "PUBLIC_URL": os.getenv("PUBLIC_URL", "http://127.0.0.1:8000"),
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
    # 撥打：送「打電話」指令給 Temi，由 Temi 內建視訊通話打給聯絡人
    #   TEMI_URL      ：Temi 接收指令的網址（測試時是 mock_temi.py）
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
    """緊急程度：寫在摘要上給家屬參考，不決定要不要撥打（撥打依流程圖：無語音或未查看）"""
    EMERGENCY = "緊急"
    WARNING = "需關注"
    SAFE = "安全"


@dataclass
class FallInfo:                      # 來自 A 組
    fall_time: datetime
    location: str
    confidence: float = 1.0


@dataclass
class SpeechInfo:                    # 來自 B 組
    responded: bool = False          # 長者有沒有語音回應
    text: str = ""
    language: str = ""               # zh-TW / nan-TW（台語）
    llm_intent: str = ""             # need_help / ok / 空白（B 組 LLM 整理的意圖）
    llm_summary: str = ""


@dataclass
class GestureInfo:                   # 來自 C1（只在有語音回應時做）
    arms: str = ""                       # 「請把兩隻手都舉起來」：both / left / right / none；"" 沒做這題
    leg_moved: Optional[bool] = None     # 「請動一動你的腳」：True 有動、False 沒動、None 沒做這題
    call_consent: str = ""               # 「需要幫你打電話嗎？」：nod 點頭 / shake_head 搖頭 / "" 沒反應

    @property
    def arm_raised(self) -> Optional[bool]:
        return None if not self.arms else self.arms != "none"


@dataclass
class Incident:
    event_id: str
    fall: Optional[FallInfo] = None
    speech: Optional[SpeechInfo] = None
    gesture: Optional[GestureInfo] = None
    processed: bool = False

    @property
    def has_voice(self) -> bool:
        return bool(self.speech and self.speech.responded)


@dataclass
class Assessment:
    status: Status
    reasons: list[str]
    direct_call: bool                # True = 無語音回應，直接撥打


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
    text = d.get("text", "") or ""
    intent = (d.get("llm_intent", "") or "").lower()
    responded = d.get("responded")
    if responded is None:            # 沒給 responded 時，有文字就當作有回應
        responded = bool(text.strip() or intent)
    return SpeechInfo(
        responded=responded is True,
        text=text,
        language=d.get("language", "") or "",
        llm_intent=intent,
        llm_summary=d.get("llm_summary", "") or "",
    )


def parse_gesture(d: dict) -> GestureInfo:
    arms = (d.get("arms", "") or "").lower()
    if arms not in ("both", "left", "right", "none"):
        arms = ""
    if not arms and isinstance(d.get("arm_raised"), bool):     # 只給 arm_raised 時
        arms = "both" if d["arm_raised"] else "none"
    leg = d.get("leg_moved")
    consent = (d.get("call_consent", "") or "").lower()
    # 相容舊格式 {"gestures": ["raise_hand", "nod"]}
    old = d.get("gestures")
    if old is not None:
        old = [old] if isinstance(old, str) else [x.lower() for x in old]
        if not arms and "raise_hand" in old:
            arms = "both"
        if not consent:
            consent = next((x for x in old if x in ("nod", "shake_head")), "")
    return GestureInfo(arms=arms, leg_moved=leg if isinstance(leg, bool) else None, call_consent=consent)


# ============================================================
# 2. 判斷緊急程度
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


def analyze_speech(s: Optional[SpeechInfo]) -> str:
    """回傳 none（無語音回應）/ severe / need_help / ok / unclear"""
    if s is None or not s.responded:
        return "none"
    text = s.text
    if any(k in text for k in SEVERE_KEYWORDS):
        return "severe"
    if s.llm_intent == "need_help":
        return "need_help"
    if s.llm_intent == "ok":
        return "ok"
    if any(k in text for k in HELP_KEYWORDS):
        return "need_help"
    if any(k in text for k in OK_KEYWORDS):
        return "ok"
    return "unclear"


def assess(inc: Incident, now: Optional[datetime] = None) -> Assessment:
    now = now or datetime.now()
    reasons: list[str] = []
    sp = analyze_speech(inc.speech)
    g = inc.gesture

    if sp == "none":
        reasons.append("長者無語音回應，直接撥打聯絡人")
        return Assessment(Status.EMERGENCY, reasons, direct_call=True)

    if g is None:
        reasons.append("未收到肢體確認結果")

    # 肢體活動受限的項目
    limits = []
    if g:
        if g.arms == "none":
            limits.append("無法依指令舉手")
        elif g.arms in ("left", "right"):
            other = "右" if g.arms == "left" else "左"
            limits.append(f"只能舉起{'左' if g.arms == 'left' else '右'}手，{other}手無法舉起")
        if g.leg_moved is False:
            limits.append("無法依指令動腳")

    if sp == "severe":
        status = Status.EMERGENCY
        reasons.append("語音中出現嚴重症狀")
        reasons.extend(limits)
    elif g and g.call_consent == "nod":
        status = Status.EMERGENCY
        reasons.append("長者點頭，表示需要幫忙打電話")
        reasons.extend(limits)
    elif limits and sp == "need_help":
        status = Status.EMERGENCY
        reasons.append("語音提到不適，且" + "、".join(limits))
    elif len(limits) >= 2:
        status = Status.EMERGENCY
        reasons.append("、".join(limits) + "，肢體活動明顯受限")
    elif limits:
        status = Status.WARNING
        reasons.append(limits[0] + "，肢體活動可能受限")
    elif sp == "need_help":
        status = Status.WARNING
        reasons.append("語音提到不適")
    elif sp == "ok" and g and g.call_consent == "shake_head":
        status = Status.SAFE
        reasons.append("長者表示沒事，並搖頭表示不需要打電話")
    elif sp == "ok":
        status = Status.SAFE if g else Status.WARNING
        reasons.append("長者表示沒事")
    else:
        status = Status.WARNING
        reasons.append("回應不明確，建議家屬確認")

    # 躺太久升級
    if inc.fall:
        lying_min = (now - inc.fall.fall_time).total_seconds() / 60
        if lying_min >= CONFIG["LONG_LIE_MINUTES"] and status != Status.EMERGENCY:
            reasons.append(f"跌倒已超過 {int(lying_min)} 分鐘")
            status = Status.EMERGENCY if status == Status.WARNING else Status.WARNING

    return Assessment(status, reasons, direct_call=False)


# ============================================================
# 3. 產生事件摘要
# ============================================================
def build_summary(inc: Incident, a: Assessment) -> str:
    icon = {Status.EMERGENCY: "🚨", Status.WARNING: "⚠️", Status.SAFE: "ℹ️"}[a.status]
    lines = [f"{icon}【跌倒事件通知｜{a.status.value}】"]

    if inc.fall:
        lines.append(f"時間：{inc.fall.fall_time:%Y-%m-%d %H:%M:%S}")
        lines.append(f"位置：{inc.fall.location}")
    else:
        lines.append("時間 / 位置：未取得（A 組資料缺漏）")

    sp = inc.speech
    if sp and sp.responded:
        lang = {"nan-TW": "台語", "zh-TW": "國語"}.get(sp.language, sp.language)
        lines.append(f"語音回應：「{sp.text}」" + (f"（{lang}）" if lang else "") if sp.text else "語音回應：有")
        if sp.llm_summary:
            lines.append(f"語音整理：{sp.llm_summary}")
    else:
        lines.append("語音回應：無")

    g = inc.gesture
    if g:
        arm = {"both": "雙手都能舉起", "left": "只能舉起左手", "right": "只能舉起右手",
               "none": "無法舉手", "": "未確認"}[g.arms]
        leg = {True: "腳能活動", False: "腳無法活動", None: "未確認"}[g.leg_moved]
        consent = {"nod": "點頭（需要）", "shake_head": "搖頭（不需要）"}.get(g.call_consent, "未回應")
        lines.append(f"肢體確認：手：{arm}；腳：{leg}；是否需要打電話：{consent}")
    elif inc.has_voice:
        lines.append("肢體確認：未取得")

    lines.append("判斷依據：" + "；".join(a.reasons))
    if not a.direct_call:
        advice = {
            Status.EMERGENCY: "請立即確認狀況，必要時撥打 119。",
            Status.WARNING: "請盡快聯繫長者確認狀況。",
            Status.SAFE: "長者表示無大礙，仍建議稍後關心。",
        }[a.status]
        lines.append(f"建議：{advice}")
    lines.append(f"事件編號：{inc.event_id}")
    return "\n".join(lines)


def ack_url(event_id: str) -> str:
    return f"{CONFIG['PUBLIC_URL'].rstrip('/')}/ack?event_id={urllib.parse.quote(event_id)}"


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
# 5. 「已查看」確認：摘要附連結，聯絡人點了就不撥打
# ============================================================
# {event_id: {"acked": bool, "summary": str, "timer": Timer}}
ACKS: dict[str, dict] = {}
ACK_LOCK = threading.Lock()


def wait_for_ack(event_id: str, summary: str) -> None:
    timer = threading.Timer(CONFIG["ACK_TIMEOUT_SEC"], ack_timeout, args=[event_id])
    timer.daemon = True
    with ACK_LOCK:
        ACKS[event_id] = {"acked": False, "summary": summary, "timer": timer}
    timer.start()


def handle_ack(event_id: str) -> str:
    """聯絡人點了「已查看」；回傳要顯示給聯絡人的訊息"""
    with ACK_LOCK:
        rec = ACKS.get(event_id)
        if rec is None:
            return "找不到這個事件。"
        if rec["acked"]:
            return "已經確認過了。"
        if rec.get("called"):
            return "已超過確認時限，Temi 已主動撥打給聯絡人。"
        rec["acked"] = True
        rec["timer"].cancel()
    print(f"[查看] 事件 {event_id}：聯絡人已查看摘要，由聯絡人決定後續處理")
    return "已確認收到通知。請自行聯繫長者，或遠端連線 Temi 查看狀況。"


def ack_timeout(event_id: str) -> None:
    with ACK_LOCK:
        rec = ACKS.get(event_id)
        if rec is None or rec["acked"]:
            return
        rec["called"] = True
    print(f"[查看] 事件 {event_id}：{CONFIG['ACK_TIMEOUT_SEC']} 秒內未查看，請 Temi 主動撥打")
    start_calling(event_id, rec["summary"], "聯絡人未查看摘要")


# ============================================================
# 6. 撥打：送「打電話」指令給 Temi
# ============================================================
# Temi 端需要做的事（Temi App 或 RIM2，待組員確認）：
#   1. 接收指令 {"action": "call_contact", "contact": "女兒", "event_id": "..."}
#   2. 用 Temi 內建視訊通話撥給該聯絡人
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


def start_calling(event_id: str, summary: str, reason: str) -> str:
    CALLS[event_id] = {"index": 0, "summary": summary, "done": False}
    called = call_contact_via_temi(event_id, 0)
    if not called:
        notify_contacts("⚠️ 跌倒事件：Temi 無法撥打",
                        summary + f"\n\n‼️ {reason}，但 Temi 無法撥出，請立即自行聯繫長者或撥打 119。")
    return called


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
        "⚠️ 跌倒事件：Temi 撥打無人接聽",
        rec["summary"] + "\n\n‼️ Temi 撥打所有聯絡人皆未接通，請立即回電或撥打 119。",
    )
    return {"event_id": event_id, "message": "所有聯絡人都未接通，已再次通知"}


# ============================================================
# 主流程：整合 → 判斷 → 摘要 → 通知 →（無語音：直接撥打／有語音：等「已查看」）
# ============================================================
def process_incident(inc: Incident) -> dict:
    a = assess(inc)
    summary = build_summary(inc, a)
    inc.processed = True
    first = CONFIG["TEMI_CONTACTS"][0] if CONFIG["TEMI_CONTACTS"] else "聯絡人"

    if a.direct_call:
        notify_contacts(f"跌倒事件通知（{a.status.value}）",
                        summary + f"\n\n📞 長者無語音回應，Temi 正在撥打給「{first}」，請留意來電。")
        called = start_calling(inc.event_id, summary, "長者無語音回應")
        action = "direct_call"
    else:
        wait_for_ack(inc.event_id, summary)
        notify_contacts(
            f"跌倒事件通知（{a.status.value}）",
            summary + f"\n\n👉 請點此確認已查看：{ack_url(inc.event_id)}"
                      f"\n若 {CONFIG['ACK_TIMEOUT_SEC']} 秒內未確認，Temi 將主動撥打給「{first}」。",
        )
        called = ""
        action = "wait_ack"

    return {
        "event_id": inc.event_id,
        "status": a.status.name,
        "action": action,
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
                # 收到跌倒後開始倒數，逾時就用現有資料處理
                t = threading.Timer(CONFIG["WAIT_TIMEOUT_SEC"], self._timeout, args=[event_id])
                t.daemon = True
                t.start()
            elif kind == "speech":
                inc.speech = parse_speech(d)
            elif kind == "gesture":
                inc.gesture = parse_gesture(d)
            # 無語音回應：不等肢體確認；有語音回應：等 C1 結果
            ready = inc.fall and inc.speech and (not inc.speech.responded or inc.gesture)
        if ready:
            return self._finish(event_id)
        return {"event_id": event_id, "message": f"已收到 {kind}，等待其他資料"}

    def _timeout(self, event_id: str):
        with self.lock:
            inc = self.incidents.get(event_id)
            if not inc or inc.processed:
                return
        print(f"[彙整] 事件 {event_id} 等待逾時，以現有資料處理")
        self._finish(event_id)

    def _finish(self, event_id: str) -> dict:
        with self.lock:
            inc = self.incidents[event_id]
            if inc.processed:
                return {"event_id": event_id, "message": "事件已處理"}
            inc.processed = True
        return process_incident(inc)


# ============================================================
# HTTP 伺服器
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

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path.rstrip("/") != "/ack":
            return self._reply(404, {"error": "unknown path"})
        event_id = urllib.parse.parse_qs(url.query).get("event_id", [""])[0]
        msg = handle_ack(event_id)
        page = (f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                f"<title>跌倒事件</title><body style='font-family:sans-serif;padding:24px;font-size:20px'>"
                f"<p>{msg}</p><p style='color:#888;font-size:14px'>事件編號：{event_id}</p></body>")
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            elif path == "/ack":
                self._reply(200, {"message": handle_ack(data.get("event_id", ""))})
            elif path in ("/fall", "/speech", "/gesture"):
                self._reply(200, aggregator.add(path[1:], data))
            else:
                self._reply(404, {"error": "unknown path"})
        except Exception as e:
            self._reply(400, {"error": str(e)})

    def log_message(self, *args):
        pass  # 關掉預設的連線紀錄，畫面比較乾淨


def run_server():
    srv = ThreadingHTTPServer((CONFIG["HOST"], CONFIG["PORT"]), Handler)
    print(f"事件整合伺服器啟動：http://{CONFIG['HOST']}:{CONFIG['PORT']}"
          f"（「已查看」等待 {CONFIG['ACK_TIMEOUT_SEC']} 秒）")
    srv.serve_forever()


# ============================================================
# 內建測試情境
# ============================================================
def run_demo():
    CONFIG["ACK_TIMEOUT_SEC"] = 3      # demo 縮短等待時間
    now = datetime.now().isoformat(timespec="seconds")
    cases = {
        "情境1：喊痛，雙手能舉、腳沒動，點頭要打電話（聯絡人有查看）": ({
            "fall": {"fall_time": now, "location": "客廳沙發旁"},
            "speech": {"responded": True, "text": "我腳好痛，起不來", "language": "zh-TW",
                       "llm_intent": "need_help", "llm_summary": "長者表示腳部疼痛、無法自行站起"},
            "gesture": {"arms": "both", "leg_moved": False, "call_consent": "nod"},
        }, True),
        "情境2：台語說沒事，手腳都能動，搖頭不用打（聯絡人沒查看）": ({
            "fall": {"fall_time": now, "location": "臥室"},
            "speech": {"responded": True, "text": "無代誌啦，我家己會起來", "language": "nan-TW"},
            "gesture": {"arms": "both", "leg_moved": True, "call_consent": "shake_head"},
        }, False),
        "情境3：說沒事，但只能舉右手，搖頭不用打（聯絡人有查看）": ({
            "fall": {"fall_time": now, "location": "書房"},
            "speech": {"responded": True, "text": "我還好", "language": "zh-TW"},
            "gesture": {"arms": "right", "leg_moved": True, "call_consent": "shake_head"},
        }, True),
        "情境4：無語音回應（直接撥打）": ({
            "fall": {"fall_time": now, "location": "浴室門口"},
            "speech": {"responded": False},
        }, False),
    }
    for name, (payload, will_ack) in cases.items():
        print(f"\n######## {name} ########")
        r = process_payload(payload)
        print(f"→ 緊急程度：{r['status']}，處理方式：{r['action']}")
        if will_ack and r["action"] == "wait_ack":
            print(f"（模擬聯絡人點了「已查看」）→ {handle_ack(r['event_id'])}")
    wait = CONFIG["ACK_TIMEOUT_SEC"] + 1
    print(f"\n######## 等待 {wait} 秒，看沒查看的事件會不會撥打 ########")
    time.sleep(wait)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    run_server() if mode == "server" else run_demo()
