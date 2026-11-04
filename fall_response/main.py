"""命令列入口。

監測跌倒：python -m fall_response.main --source 0
測試動作：python -m fall_response.main --mode gesture --source 0
"""
import argparse
import logging
import time
from collections import deque
from pathlib import Path

from .app import FallMonitorApp, run_gesture_demo
from .fall_detector import FallDetector, FallState
from .gesture_recognizer import GestureRecognizer
from .incident import IncidentManager, SimulatedLayer, SimulatedNotifier, SimulatedPlanner, SimulatedRobot
from .pose_estimator import PoseEstimator
from .response_layers import LANGUAGES, MANDARIN, BodyLayer, SimulatedSpeaker, SimulatedVoiceClient, VoiceLayer
from .video_input import VideoSource, parse_source

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = BASE_DIR / "models" / "pose_landmarker_full.task"
DEFAULT_LOG = BASE_DIR / "logs" / "incidents.jsonl"
WINDOW_TITLE = "Temi Fall Response (q to quit)"
CHINESE_FONT = Path(r"C:\Windows\Fonts\msjhbd.ttc")
# 顏色為 BGR；最後一項是找不到中文字型時改顯示的英文
FALL_BANNER = ("偵測到摔倒", (0, 0, 220), "FALL DETECTED")
CONFIRM_BANNER = ("疑似摔倒，確認中", (0, 140, 230), "CONFIRMING")

SKELETON = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24),
            (23, 24), (23, 25), (25, 27), (24, 26), (26, 28)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Temi 跌倒應變系統：影像、肢體辨識與跌倒偵測")
    parser.add_argument("--source", default="0", help="鏡頭編號、手機串流網址或影片檔路徑")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="Pose Landmarker 模型路徑")
    parser.add_argument("--mode", choices=["monitor", "gesture"], default="monitor")
    parser.add_argument("--no-display", action="store_true", help="不開啟預覽視窗")
    parser.add_argument("--language", choices=LANGUAGES, default=MANDARIN, help="互動語言：zh-TW 國語、nan-TW 台語")
    parser.add_argument("--temi-camera", help="肢體確認使用的影像來源；未設定時肢體層為模擬")
    parser.add_argument("--simulate-voice-response", action="store_true", help="模擬長者有語音回應")
    return parser.parse_args()


def build_incidents(args: argparse.Namespace) -> tuple[IncidentManager, object]:
    """組裝事件流程；回傳的第二個值是肢體層專用的姿勢模型，沒有使用時為 None。"""
    voice = VoiceLayer(SimulatedVoiceClient(args.simulate_voice_response), args.language)
    body, body_estimator = SimulatedLayer("肢體"), None
    if args.temi_camera:
        # MediaPipe 的時間戳必須遞增，不能和固定鏡頭共用同一個模型
        body_estimator = PoseEstimator(args.model)
        body = BodyLayer(
            SimulatedSpeaker(),
            body_estimator,
            lambda: VideoSource(parse_source(args.temi_camera)),
            args.language,
        )
    manager = IncidentManager(
        SimulatedPlanner(), SimulatedRobot(), voice, body, SimulatedNotifier(), log_path=DEFAULT_LOG
    )
    return manager, body_estimator


def monitor_lines(pose, update) -> list[str]:
    lines = [f"state: {update.state.value}", f"descent: {update.descent_speed:.2f} (fall >= 1.50)"]
    if pose is None:
        return lines + ["body: NOT DETECTED"]
    if not pose.torso_reliable:
        return lines + ["body: shoulders/hips NOT VISIBLE"]
    return lines + [
        f"angle: {pose.torso_angle_deg:.0f} (down >= 55)",
        f"ratio: {pose.aspect_ratio:.2f} (down >= 1.30)",
    ]


def make_monitor_callback(show=None, alert_seconds: float = 5.0, peak_seconds: float = 5.0):
    """模擬流程會瞬間跑完，fallen 狀態只有一格畫面，所以另外讓提示停留幾秒。"""
    show = show or show_frame
    alert_until = float("-inf")
    speeds: deque = deque()
    frame_times: deque = deque(maxlen=30)

    def callback(frame, pose, update) -> bool:
        nonlocal alert_until
        if update.event is not None:
            alert_until = frame.timestamp + alert_seconds

        frame_times.append(time.monotonic())
        speeds.append((frame.timestamp, update.descent_speed))
        while frame.timestamp - speeds[0][0] > peak_seconds:
            speeds.popleft()
        elapsed = frame_times[-1] - frame_times[0]
        fps = (len(frame_times) - 1) / elapsed if elapsed > 0 else 0.0

        lines = monitor_lines(pose, update)
        lines.append(f"peak descent ({peak_seconds:.0f}s): {max(s for _, s in speeds):.2f}")
        lines.append(f"FPS: {fps:.0f}")
        if update.moving:
            lines.append("moving: waiting until still")

        banner = None
        if frame.timestamp < alert_until:
            banner = FALL_BANNER
        elif update.state is FallState.CONFIRMING:
            banner = CONFIRM_BANNER
        return show(frame, pose, lines, banner)

    return callback


def draw_banner(image, text: str, color: tuple[int, int, int], fallback: str) -> None:
    """在畫面下方畫提示條。OpenCV 無法顯示中文，所以用 Pillow 繪製。"""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    bar_height = max(60, height // 7)
    top = height - bar_height
    cv2.rectangle(image, (0, top), (width, height), color, -1)
    if not CHINESE_FONT.exists():
        cv2.putText(image, fallback, (20, top + bar_height // 2 + 12), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        return

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(CHINESE_FONT), int(bar_height * 0.6))
    bar = Image.fromarray(image[top:height, 0:width][:, :, ::-1])
    draw = ImageDraw.Draw(bar)
    left, upper, right, lower = draw.textbbox((0, 0), text, font=font)
    position = ((width - (right - left)) // 2 - left, (bar_height - (lower - upper)) // 2 - upper)
    draw.text(position, text, font=font, fill=(255, 255, 255))
    image[top:height, 0:width] = np.array(bar)[:, :, ::-1]


def show_frame(frame, pose, lines: list[str], banner=None) -> bool:
    import cv2

    image = frame.image.copy()
    if pose is not None:
        for a, b in SKELETON:
            if pose.visible(a) and pose.visible(b):
                start = (int(pose.landmarks[a].x), int(pose.landmarks[a].y))
                end = (int(pose.landmarks[b].x), int(pose.landmarks[b].y))
                cv2.line(image, start, end, (80, 220, 80), 2)
    for row, line in enumerate(lines):
        cv2.putText(image, line, (12, 30 + row * 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
    if banner is not None:
        draw_banner(image, *banner)
    cv2.imshow(WINDOW_TITLE, image)
    return cv2.waitKey(1) & 0xFF != ord("q")


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    estimator = PoseEstimator(args.model)
    source = VideoSource(parse_source(args.source))
    body_estimator = None
    try:
        if args.mode == "gesture":
            callback = None
            if not args.no_display:
                callback = lambda frame, pose, gestures: show_frame(
                    frame, pose, ["gestures: " + (", ".join(sorted(g.value for g in gestures)) or "-")]
                )
            run_gesture_demo(estimator, GestureRecognizer(), source.frames(), callback)
        else:
            callback = None
            if not args.no_display:
                callback = make_monitor_callback()
            incidents, body_estimator = build_incidents(args)
            app = FallMonitorApp(estimator, FallDetector(), incidents)
            app.run(source.frames(), callback)
    except KeyboardInterrupt:
        logging.info("使用者中止")
    finally:
        source.close()
        estimator.close()
        if body_estimator is not None:
            body_estimator.close()
        if not args.no_display:
            import cv2

            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
