"""
C1：肢體確認（先提出問題，再辨識長者的肢體回應）

依語音確認的結果，提出不同的問題：
    語音有回應 → purpose = call_consent
        「需要幫你打電話給家人嗎？需要請點頭，不需要請搖頭」
        只看 nod 點頭（要打電話）／ shake_head 搖頭（不用打）
    語音沒回應 → purpose = consciousness
        「如果你聽得到，請舉起你的手」
        只看 raise_hand 舉手（還有意識）
辨識結果送給 C2（event_reporter.py）：
    POST http://<C2>/gesture  {"event_id": "...", "purpose": "call_consent", "gestures": ["nod"]}
沒做出指定動作就送空清單 []。

使用方式：
    python gesture_recognizer.py demo                                   # 用假資料測試辨識邏輯（不需攝影機、不需安裝套件）
    python gesture_recognizer.py camera                                 # 開攝影機看辨識效果（不問問題、不送 C2）
    python gesture_recognizer.py ask                                    # 問「要不要打電話」，看點頭／搖頭
    python gesture_recognizer.py ask --purpose consciousness            # 問「聽得到請舉手」，看舉手
    python gesture_recognizer.py ask --event-id e001                    # 問完把結果送給 C2
    python gesture_recognizer.py server                                 # 等待指令（port 8001），收到就提問並觀察

server 模式的介面（給 B 組或 Temi 呼叫，語音確認結束後呼叫）：
    POST /start  {"event_id": "e001", "purpose": "call_consent", "seconds": 10}

提問方式：電腦上用 Windows 內建語音唸出問題（沒有中文語音就只顯示文字，可用 --no-voice 關掉）。
之後接上 Temi 時，改由 Temi 唸問題，這裡只負責觀察。

需要套件：pip install opencv-python mediapipe
（demo 模式只用標準函式庫）
第一次執行 camera / server 會自動下載 MediaPipe 姿勢模型（約 5MB）到 models/ 資料夾。

辨識原理（MediaPipe Pose 33 個關鍵點）：
    舉手：手腕沿著「臀部→肩膀」的身體方向，比肩膀高出一段距離，並維持一小段時間。
          用身體方向而不是畫面上下，所以坐著、半躺著也能判斷。
    點頭／搖頭：以兩耳連線建立頭部座標，追蹤鼻子相對兩耳的位置。
          左右來回擺動 → 搖頭；上下來回擺動 → 點頭。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

# ============================================================
# 設定（可用環境變數覆蓋）
# ============================================================
CONFIG = {
    "C2_URL": os.getenv("C2_URL", "http://127.0.0.1:8000"),   # event_reporter.py 的位址
    "SOURCE": os.getenv("CAMERA_SOURCE", "0"),                 # 攝影機編號，或影片檔／串流網址
    "OBSERVE_SECONDS": float(os.getenv("OBSERVE_SECONDS", "10")),  # 每次觀察幾秒
    "EARLY_STOP": os.getenv("EARLY_STOP", "1") == "1",         # 辨識到動作就提早結束
    "VOICE": os.getenv("C1_VOICE", "1") == "1",                # 用電腦語音唸出問題
    "HOST": os.getenv("HOST", "0.0.0.0"),
    "PORT": int(os.getenv("C1_PORT", "8001")),
    "MODEL_PATH": os.getenv(
        "POSE_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "pose_landmarker_lite.task")
    ),
    "MODEL_URL": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
                 "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
}

# 辨識參數（想調靈敏度改這裡）
PARAMS = {
    "MIN_VISIBILITY": 0.5,      # 關鍵點可信度低於此值就不用
    "RAISE_RATIO": 0.5,         # 手腕比肩膀高出「肩寬 × 此值」才算舉手
    "RAISE_HOLD_SEC": 0.6,      # 舉手要維持幾秒
    "YAW_THRESHOLD": 0.12,      # 搖頭：鼻子左右擺動幅度（以兩耳距離為單位）
    "PITCH_THRESHOLD": 0.08,    # 點頭：鼻子上下擺動幅度（以兩耳距離為單位）
    "MIN_SWINGS": 3,            # 來回擺動幾次才算（例如 左→右→左 = 3 次）
    "SWING_WINDOW_SEC": 2.5,    # 這些擺動要在幾秒內完成
    "DOMINANCE": 1.3,           # 主要方向的擺動幅度要比另一方向大幾倍（避免亂動被誤判）
    "SMOOTHING": 0.5,           # 數值平滑（0~1，越大越平滑）
}

GESTURE_TEXT = {"raise_hand": "舉手", "nod": "點頭", "shake_head": "搖頭"}

# 每種確認要問的問題，以及這個問題只接受哪些動作（想改問法改這裡）
QUESTIONS = {
    "call_consent": {
        "prompt": "需要幫你打電話給家人嗎？需要請點頭，不需要請搖頭。",
        "accept": {"nod", "shake_head"},
    },
    "consciousness": {
        "prompt": "如果你聽得到，請舉起你的手。",
        "accept": {"raise_hand"},
    },
}


def speak(text: str) -> None:
    """用 Windows 內建語音唸出問題；失敗（例如沒有中文語音、非 Windows）就只顯示文字"""
    print(f"\n🔊 Temi：「{text}」")
    if not CONFIG["VOICE"] or sys.platform != "win32":
        return
    import subprocess
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name); $s.Speak($env:C1_SPEAK_TEXT) }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=20,
                       env=dict(os.environ, C1_SPEAK_TEXT=text), capture_output=True)
    except Exception as e:
        print(f"[C1] 語音播放失敗，只顯示文字：{e}")

# MediaPipe Pose 關鍵點編號
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 2, 5, 7, 8
L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST, L_HIP, R_HIP = 11, 12, 15, 16, 23, 24


@dataclass
class Point:
    x: float          # 像素座標（不是 0~1，避免畫面長寬比造成角度誤差）
    y: float
    visibility: float = 1.0


# ============================================================
# 向量小工具
# ============================================================
def sub(a: Point, b: Point) -> tuple[float, float]:
    return a.x - b.x, a.y - b.y


def mid(a: Point, b: Point) -> Point:
    return Point((a.x + b.x) / 2, (a.y + b.y) / 2, min(a.visibility, b.visibility))


def norm(v: tuple[float, float]) -> float:
    return math.hypot(v[0], v[1])


def unit(v: tuple[float, float]) -> tuple[float, float]:
    n = norm(v)
    return (v[0] / n, v[1] / n) if n > 1e-6 else (0.0, 0.0)


def dot(a: tuple[float, float], b: tuple[float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1]


# ============================================================
# 擺動偵測：找數值「來回」的次數（之字形偵測）
# ============================================================
class SwingCounter:
    def __init__(self, threshold: float, window: float):
        self.threshold = threshold
        self.window = window
        self.direction = 0          # +1 上升中，-1 下降中，0 還沒開始
        self.hi = self.lo = None    # 方向未定時的最高、最低值
        self.extreme = 0.0          # 目前方向上的極值
        self.swings: deque[float] = deque()   # 每次轉向的時間

    def reset(self):
        self.direction, self.hi, self.lo = 0, None, None
        self.swings.clear()

    def update(self, v: float, t: float) -> int:
        thr = self.threshold
        if self.direction == 0:
            self.hi = v if self.hi is None else max(self.hi, v)
            self.lo = v if self.lo is None else min(self.lo, v)
            if v - self.lo >= thr:
                self.direction, self.extreme = 1, v
                self.swings.append(t)
            elif self.hi - v >= thr:
                self.direction, self.extreme = -1, v
                self.swings.append(t)
        elif self.direction == 1:
            if v > self.extreme:
                self.extreme = v
            elif self.extreme - v >= thr:
                self.direction, self.extreme = -1, v
                self.swings.append(t)
        else:
            if v < self.extreme:
                self.extreme = v
            elif v - self.extreme >= thr:
                self.direction, self.extreme = 1, v
                self.swings.append(t)
        while self.swings and t - self.swings[0] > self.window:
            self.swings.popleft()
        return len(self.swings)


# ============================================================
# 肢體辨識器：每一幀丟關鍵點進來，回傳這一幀新辨識到的動作
# ============================================================
class GestureDetector:
    def __init__(self, params: Optional[dict] = None):
        self.p = dict(PARAMS, **(params or {}))
        self.raise_since: Optional[float] = None
        self.yaw_counter = SwingCounter(self.p["YAW_THRESHOLD"], self.p["SWING_WINDOW_SEC"])
        self.pitch_counter = SwingCounter(self.p["PITCH_THRESHOLD"], self.p["SWING_WINDOW_SEC"])
        self.yaw_hist: deque[tuple[float, float]] = deque()
        self.pitch_hist: deque[tuple[float, float]] = deque()
        self.yaw_s: Optional[float] = None
        self.pitch_s: Optional[float] = None
        self.detected: list[str] = []     # 整段觀察累積辨識到的動作（不重複，依出現順序）
        self.debug: dict = {}             # 給畫面顯示用

    def _ok(self, pt: Optional[Point]) -> bool:
        return pt is not None and pt.visibility >= self.p["MIN_VISIBILITY"]

    # ---------- 舉手 ----------
    def _hand_raised(self, lm: dict[int, Point]) -> bool:
        ls, rs = lm.get(L_SHOULDER), lm.get(R_SHOULDER)
        if not (self._ok(ls) and self._ok(rs)):
            return False
        shoulder_w = norm(sub(ls, rs))
        if shoulder_w < 1e-3:
            return False
        lh, rh = lm.get(L_HIP), lm.get(R_HIP)
        if self._ok(lh) and self._ok(rh):
            up = unit(sub(mid(ls, rs), mid(lh, rh)))        # 臀部 → 肩膀 = 身體的「上」
        else:
            up = (0.0, -1.0)                                # 看不到臀部就用畫面的上方
        best = -9.0
        for wrist_id, shoulder in ((L_WRIST, ls), (R_WRIST, rs)):
            w = lm.get(wrist_id)
            if self._ok(w):
                best = max(best, dot(sub(w, shoulder), up) / shoulder_w)
        self.debug["raise"] = best
        return best >= self.p["RAISE_RATIO"]

    # ---------- 頭部方向 ----------
    def _head_pose(self, lm: dict[int, Point]) -> Optional[tuple[float, float]]:
        """回傳 (yaw, pitch)：鼻子相對兩耳中點的偏移，以兩耳距離為單位、以兩耳連線為座標軸"""
        nose, le, ree = lm.get(NOSE), lm.get(L_EAR), lm.get(R_EAR)
        if not (self._ok(nose) and self._ok(le) and self._ok(ree)):
            return None
        ear_vec = sub(le, ree)
        ear_d = norm(ear_vec)
        if ear_d < 1e-3:
            return None
        x_axis = unit(ear_vec)                 # 沿兩耳方向（左右）
        y_axis = (-x_axis[1], x_axis[0])       # 垂直兩耳方向（上下），躺著時也會跟著轉
        off = sub(nose, mid(le, ree))
        return dot(off, x_axis) / ear_d, dot(off, y_axis) / ear_d

    def _smooth(self, prev: Optional[float], v: float) -> float:
        a = self.p["SMOOTHING"]
        return v if prev is None else a * prev + (1 - a) * v

    @staticmethod
    def _range(hist: deque, t: float, window: float) -> float:
        while hist and t - hist[0][0] > window:
            hist.popleft()
        vals = [v for _, v in hist]
        return (max(vals) - min(vals)) if vals else 0.0

    def _add(self, g: str, new: list[str]):
        if g not in self.detected:
            self.detected.append(g)
        new.append(g)

    def update(self, lm: Optional[dict[int, Point]], t: float) -> list[str]:
        """lm：{關鍵點編號: Point}，沒偵測到人就傳 None；t：秒。回傳這一幀觸發的動作。"""
        new: list[str] = []
        if not lm:
            self.raise_since = None
            return new

        # 舉手：要持續一段時間，避免揮過去就算
        if self._hand_raised(lm):
            if self.raise_since is None:
                self.raise_since = t
            elif t - self.raise_since >= self.p["RAISE_HOLD_SEC"]:
                self._add("raise_hand", new)
                self.raise_since = None
        else:
            self.raise_since = None

        # 點頭／搖頭
        hp = self._head_pose(lm)
        if hp is None:
            return new
        self.yaw_s = self._smooth(self.yaw_s, hp[0])
        self.pitch_s = self._smooth(self.pitch_s, hp[1])
        win = self.p["SWING_WINDOW_SEC"]
        self.yaw_hist.append((t, self.yaw_s))
        self.pitch_hist.append((t, self.pitch_s))
        yaw_range = self._range(self.yaw_hist, t, win)
        pitch_range = self._range(self.pitch_hist, t, win)
        yaw_swings = self.yaw_counter.update(self.yaw_s, t)
        pitch_swings = self.pitch_counter.update(self.pitch_s, t)
        self.debug.update(yaw=self.yaw_s, pitch=self.pitch_s, yaw_sw=yaw_swings, pitch_sw=pitch_swings)

        # 擺動幅度要「相對」另一方向明顯，才算數（以各自門檻正規化後比較）
        yaw_score = yaw_range / self.p["YAW_THRESHOLD"]
        pitch_score = pitch_range / self.p["PITCH_THRESHOLD"]
        dom = self.p["DOMINANCE"]
        if yaw_swings >= self.p["MIN_SWINGS"] and yaw_score > dom * pitch_score:
            self._add("shake_head", new)
            self.yaw_counter.reset()
            self.pitch_counter.reset()
        elif pitch_swings >= self.p["MIN_SWINGS"] and pitch_score > dom * yaw_score:
            self._add("nod", new)
            self.yaw_counter.reset()
            self.pitch_counter.reset()
        return new


# ============================================================
# 把結果送給 C2
# ============================================================
def send_to_c2(event_id: str, gestures: list[str], purpose: str = "") -> Optional[dict]:
    payload = {"event_id": event_id, "gestures": gestures}
    if purpose:
        payload["purpose"] = purpose
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CONFIG["C2_URL"].rstrip("/") + "/gesture", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        print(f"[C1] 已送給 C2：{gestures} → {result.get('message') or result.get('status')}")
        return result
    except Exception as e:
        print(f"[C1] 送給 C2 失敗（C2 沒開或網路不通）：{e}")
        return None


# ============================================================
# 攝影機 + MediaPipe
# ============================================================
def ensure_model() -> str:
    path = CONFIG["MODEL_PATH"]
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"[C1] 第一次執行，下載姿勢模型到 {path} …")
        urllib.request.urlretrieve(CONFIG["MODEL_URL"], path)
    return path


class PoseCamera:
    """開攝影機、跑 MediaPipe Pose，每次 read() 回傳 (畫面, 關鍵點 dict 或 None)"""

    def __init__(self, source: str):
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as e:
            sys.exit(f"缺少套件：{e}\n請先執行：pip install opencv-python mediapipe")
        self.cv2, self.mp = cv2, mp
        src = int(source) if str(source).isdigit() else source
        self.cap = cv2.VideoCapture(src)
        if not self.cap.isOpened():
            raise RuntimeError(f"無法開啟攝影機／影片：{source}")
        # MediaPipe 打不開含中文的路徑，所以先讀進記憶體再交給它
        with open(ensure_model(), "rb") as f:
            model_bytes = f.read()
        options = vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        )
        self.landmarker = vision.PoseLandmarker.create_from_options(options)
        self.t0 = time.monotonic()
        self.last_ts = -1

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None, None
        h, w = frame.shape[:2]
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        ts = int((time.monotonic() - self.t0) * 1000)
        ts = max(ts, self.last_ts + 1)          # MediaPipe 要求時間戳嚴格遞增
        self.last_ts = ts
        result = self.landmarker.detect_for_video(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb), ts)
        if not result.pose_landmarks:
            return frame, None
        lm = {i: Point(p.x * w, p.y * h, p.visibility if p.visibility is not None else 1.0)
              for i, p in enumerate(result.pose_landmarks[0])}
        return frame, lm

    def close(self):
        self.cap.release()
        self.landmarker.close()


def draw_overlay(cv2, frame, lm, det: GestureDetector, remaining: Optional[float]):
    if lm:
        for i in (NOSE, L_EAR, R_EAR, L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST, L_HIP, R_HIP):
            p = lm.get(i)
            if p and p.visibility >= det.p["MIN_VISIBILITY"]:
                cv2.circle(frame, (int(p.x), int(p.y)), 5, (0, 255, 0), -1)
    # cv2.putText 不支援中文，畫面上用英文
    lines = [f"detected: {', '.join(det.detected) or '-'}"]
    d = det.debug
    if "yaw" in d:
        lines.append(f"yaw {d['yaw']:+.2f} ({d['yaw_sw']})  pitch {d['pitch']:+.2f} ({d['pitch_sw']})")
    if "raise" in d:
        lines.append(f"hand {d['raise']:+.2f}")
    if remaining is not None:
        lines.append(f"time left {remaining:.1f}s")
    for k, text in enumerate(lines):
        cv2.putText(frame, text, (10, 30 + 28 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)


def observe(source: str, seconds: Optional[float], show: bool = True, early_stop: bool = True,
            accept: Optional[set] = None) -> list[str]:
    """觀察 seconds 秒（None = 一直看到按 q），回傳辨識到的動作清單；accept 指定只算哪些動作"""
    cam = PoseCamera(source)
    cv2 = cam.cv2
    det = GestureDetector()
    start = time.monotonic()

    def accepted() -> list[str]:
        return [g for g in det.detected if accept is None or g in accept]

    try:
        while True:
            now = time.monotonic() - start
            if seconds is not None and now >= seconds:
                break
            frame, lm = cam.read()
            if frame is None:
                print("[C1] 影像結束")
                break
            for g in det.update(lm, now):
                note = "" if accept is None or g in accept else "（這題不算）"
                print(f"[C1] {now:5.1f}s 辨識到：{GESTURE_TEXT.get(g, g)}（{g}）{note}")
            if show:
                draw_overlay(cv2, frame, lm, det, None if seconds is None else seconds - now)
                cv2.imshow("C1 gesture", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            if early_stop and seconds is not None and accepted():
                break
    finally:
        cam.close()
        if show:
            cv2.destroyAllWindows()
    return accepted()


def ask(purpose: str, source: str, seconds: float, show: bool = True,
        event_id: Optional[str] = None) -> list[str]:
    """提出問題 → 觀察長者動作 → （有 event_id 就）把結果送給 C2"""
    q = QUESTIONS[purpose]
    speak(q["prompt"])
    print(f"[C1] 觀察 {seconds:.0f} 秒，等待：{'／'.join(GESTURE_TEXT[g] for g in sorted(q['accept']))}")
    gestures = observe(source, seconds, show=show, early_stop=CONFIG["EARLY_STOP"], accept=q["accept"])
    print(f"[C1] 回應：{'、'.join(GESTURE_TEXT.get(g, g) for g in gestures) or '未做出指定動作'}")
    if event_id:
        send_to_c2(event_id, gestures, purpose)
    return gestures


# ============================================================
# server 模式：等 A 組／Temi 叫我開始觀察
# ============================================================
busy = threading.Lock()


def run_observation(event_id: str, purpose: str, seconds: float, show: bool):
    with busy:
        print(f"\n[C1] 事件 {event_id}：開始肢體確認（{purpose}）")
        try:
            ask(purpose, CONFIG["SOURCE"], seconds, show=show, event_id=event_id)
        except Exception as e:
            print(f"[C1] 觀察失敗：{e}")
            send_to_c2(event_id, [], purpose)


def make_handler(show: bool):
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
                if self.path.rstrip("/") != "/start":
                    return self._reply(404, {"error": "unknown path"})
                event_id = data.get("event_id")
                if not event_id:
                    return self._reply(400, {"error": "缺少 event_id"})
                if busy.locked():
                    return self._reply(409, {"error": "正在觀察其他事件"})
                purpose = data.get("purpose", "call_consent")
                if purpose not in QUESTIONS:
                    return self._reply(400, {"error": f"purpose 只能是 {list(QUESTIONS)}"})
                seconds = float(data.get("seconds", CONFIG["OBSERVE_SECONDS"]))
                threading.Thread(target=run_observation, args=(event_id, purpose, seconds, show),
                                 daemon=True).start()
                self._reply(200, {"event_id": event_id, "message": f"開始肢體確認（{purpose}）"})
            except Exception as e:
                self._reply(400, {"error": str(e)})

        def log_message(self, *args):
            pass

    return Handler


def run_server(show: bool):
    srv = ThreadingHTTPServer((CONFIG["HOST"], CONFIG["PORT"]), make_handler(show))
    print(f"C1 肢體辨識伺服器啟動：http://{CONFIG['HOST']}:{CONFIG['PORT']}/start")
    srv.serve_forever()


# ============================================================
# demo：用假關鍵點測試辨識邏輯（不需攝影機）
# ============================================================
def fake_pose(t: float, yaw: float = 0.0, pitch: float = 0.0, hand_up: bool = False,
              lying: bool = False) -> dict[int, Point]:
    """產生一個假的人：yaw / pitch 是頭部偏移（以兩耳距離為單位），lying=True 代表整個人轉 90 度躺著"""
    ear_d, shoulder_w = 60.0, 160.0
    pts = {  # 站姿、面向鏡頭，以 (0,0) 為肩膀中點；y 往下為正
        L_EAR: (ear_d / 2, -120), R_EAR: (-ear_d / 2, -120),
        NOSE: (yaw * ear_d, -120 + 5 + pitch * ear_d),
        L_SHOULDER: (shoulder_w / 2, 0), R_SHOULDER: (-shoulder_w / 2, 0),
        L_HIP: (60, 250), R_HIP: (-60, 250),
        L_WRIST: (90, 200), R_WRIST: (-90, 200),
    }
    if hand_up:
        pts[R_WRIST] = (-110, -180)
    out = {}
    for k, (x, y) in pts.items():
        if lying:                     # 轉 90 度：頭朝畫面左邊
            x, y = y, -x
        out[k] = Point(320 + x, 300 + y, 0.95)
    return out


def run_demo():
    fps = 15
    dt = 1 / fps

    def simulate(name: str, duration: float, fn) -> list[str]:
        det = GestureDetector()
        t = 0.0
        while t < duration:
            det.update(fn(t), t)
            t += dt
        return det.detected

    cases = [
        ("搖頭（站著）", [], lambda t: fake_pose(t, yaw=0.3 * math.sin(2 * math.pi * 1.5 * t)), ["shake_head"]),
        ("點頭（站著）", [], lambda t: fake_pose(t, pitch=0.2 * math.sin(2 * math.pi * 1.5 * t)), ["nod"]),
        ("舉手（站著）", [], lambda t: fake_pose(t, hand_up=t > 0.5), ["raise_hand"]),
        ("搖頭（躺著）", [], lambda t: fake_pose(t, yaw=0.3 * math.sin(2 * math.pi * 1.5 * t), lying=True), ["shake_head"]),
        ("點頭（躺著）", [], lambda t: fake_pose(t, pitch=0.2 * math.sin(2 * math.pi * 1.5 * t), lying=True), ["nod"]),
        ("舉手（躺著）", [], lambda t: fake_pose(t, hand_up=t > 0.5, lying=True), ["raise_hand"]),
        ("不動", [], lambda t: fake_pose(t), []),
        ("頭微微晃動（雜訊）", [], lambda t: fake_pose(t, yaw=0.03 * math.sin(7 * t), pitch=0.02 * math.cos(5 * t)), []),
        ("舉手一下就放下", [], lambda t: fake_pose(t, hand_up=0.5 < t < 0.8), []),
        ("看不到人", [], lambda t: None, []),
    ]
    passed = 0
    print("【動作辨識】")
    for name, _, fn, expected in cases:
        got = simulate(name, 4.0, fn)
        ok = got == expected
        passed += ok
        print(f"{'✅' if ok else '❌'} {name:12} → 辨識：{got or '無'}（預期：{expected or '無'}）")

    # 依問題只算指定動作：例如問「要不要打電話」時舉手不算
    question_cases = [
        ("問打電話，長者點頭", "call_consent", lambda t: fake_pose(t, pitch=0.2 * math.sin(2 * math.pi * 1.5 * t)), ["nod"]),
        ("問打電話，長者搖頭", "call_consent", lambda t: fake_pose(t, yaw=0.3 * math.sin(2 * math.pi * 1.5 * t)), ["shake_head"]),
        ("問打電話，長者只舉手", "call_consent", lambda t: fake_pose(t, hand_up=t > 0.5), []),
        ("請舉手，長者舉手", "consciousness", lambda t: fake_pose(t, hand_up=t > 0.5, lying=True), ["raise_hand"]),
        ("請舉手，長者沒動", "consciousness", lambda t: fake_pose(t, lying=True), []),
    ]
    print("\n【依問題篩選動作】")
    for name, purpose, fn, expected in question_cases:
        accept = QUESTIONS[purpose]["accept"]
        got = [g for g in simulate(name, 4.0, fn) if g in accept]
        ok = got == expected
        passed += ok
        print(f"{'✅' if ok else '❌'} {name:12} → 回應：{got or '無'}（預期：{expected or '無'}）")

    total = len(cases) + len(question_cases)
    print(f"\n通過 {passed}/{total}")
    return passed == total


# ============================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="C1 肢體確認（提問＋舉手／點頭／搖頭）")
    ap.add_argument("mode", nargs="?", default="demo", choices=["demo", "camera", "ask", "server"])
    ap.add_argument("--purpose", default="call_consent", choices=list(QUESTIONS),
                    help="ask 模式要問的問題：call_consent（要不要打電話）或 consciousness（聽得到請舉手）")
    ap.add_argument("--event-id", help="把結果送給 C2 時使用的事件編號")
    ap.add_argument("--source", default=CONFIG["SOURCE"], help="攝影機編號或影片／串流網址（預設 0）")
    ap.add_argument("--seconds", type=float, default=CONFIG["OBSERVE_SECONDS"], help="觀察秒數")
    ap.add_argument("--no-window", action="store_true", help="不顯示畫面")
    ap.add_argument("--no-voice", action="store_true", help="不用電腦語音唸問題，只顯示文字")
    args = ap.parse_args()
    CONFIG["SOURCE"] = args.source
    if args.no_voice:
        CONFIG["VOICE"] = False

    if args.mode == "demo":
        sys.exit(0 if run_demo() else 1)
    elif args.mode == "server":
        run_server(show=not args.no_window)
    elif args.mode == "ask":
        ask(args.purpose, args.source, args.seconds, show=not args.no_window, event_id=args.event_id)
    elif args.event_id:
        gestures = observe(args.source, args.seconds, show=not args.no_window, early_stop=CONFIG["EARLY_STOP"])
        print(f"[C1] 觀察結果：{gestures or '無肢體回應'}")
        send_to_c2(args.event_id, gestures)
    else:
        print("[C1] 測試模式：對著鏡頭舉手／點頭／搖頭，按 q 結束")
        observe(args.source, None, show=not args.no_window, early_stop=False)
