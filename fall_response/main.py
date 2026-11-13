"""命令列入口。

監測跌倒：python -m fall_response.main --source 0
"""
import argparse
import logging
import time
from collections import deque
from pathlib import Path

from .app import FallMonitorApp
from .fall_detector import FallDetector, FallState
from .incident import IncidentManager, SimulatedLayer, SimulatedNotifier, SimulatedPlanner, SimulatedRobot
from .models import LEFT_HIP, LEFT_SHOULDER, RIGHT_HIP, RIGHT_SHOULDER
from .overlay import CHINESE_FONT, draw_zones
from .pose_estimator import PoseEstimator
from .response_layers import LANGUAGES, MANDARIN, SimulatedVoiceClient, VoiceLayer
from .temi_app import TemiAppController, TemiAppError
from .temi_rim import DEFAULT_CONFIG, ConfiguredPlanner, RimClient, RimError, RimRobotController, load_config
from .video_input import VideoSource, parse_source
from .zones import DEFAULT_ZONES, ZonePlanner, load_zones, locate

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = BASE_DIR / "models" / "pose_landmarker_full.task"
DEFAULT_LOG = BASE_DIR / "logs" / "incidents.jsonl"
WINDOW_TITLE = "Temi Fall Response (q to quit)"
# 顏色為 BGR；最後一項是找不到中文字型時改顯示的英文
FALL_BANNER = ("偵測到摔倒", (0, 0, 220), "FALL DETECTED")
CONFIRM_BANNER = ("疑似摔倒，確認中", (0, 140, 230), "CONFIRMING")

SKELETON = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24),
            (23, 24), (23, 25), (25, 27), (24, 26), (26, 28)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Temi 跌倒應變系統：跌倒偵測與派遣 Temi")
    parser.add_argument("--source", default="0", help="鏡頭編號、手機串流網址或影片檔路徑")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="Pose Landmarker 模型路徑")
    parser.add_argument("--no-display", action="store_true", help="不開啟預覽視窗")
    parser.add_argument("--language", choices=LANGUAGES, default=MANDARIN, help="互動語言：zh-TW 國語、nan-TW 台語")
    parser.add_argument("--simulate-voice-response", action="store_true", help="模擬長者有語音回應")
    parser.add_argument(
        "--robot",
        choices=["simulated", "dry-run", "rim", "app"],
        default="simulated",
        help="dry-run：向 RIM2 查好任務但不送出，Temi 不會移動；rim：透過 RIM2 派遣；app：透過 Temi 上的 Java App 派遣",
    )
    parser.add_argument("--temi-config", default=str(DEFAULT_CONFIG), help="Temi 設定檔路徑（dry-run、rim 使用）")
    parser.add_argument("--temi-app-url", help="Temi Java App 的網址，例如 http://192.168.50.20:8080（app 使用）")
    parser.add_argument("--zones", default=str(DEFAULT_ZONES), help="跌倒區域設定檔，建立方式見 README")
    return parser.parse_args()


def build_robot(args: argparse.Namespace, config) -> RimRobotController:
    return RimRobotController(
        RimClient(config), config, dry_run=args.robot == "dry-run", require_robot_name=args.robot == "rim"
    )


def build_incidents(args: argparse.Namespace, zones=(), robot=None) -> IncidentManager:
    """組裝事件流程。語音、肢體、通知完成後，在這裡替換掉模擬版本。

    robot 可傳入已在啟動時檢查過的 RimRobotController 或 TemiAppController。
    """
    voice = VoiceLayer(SimulatedVoiceClient(args.simulate_voice_response), args.language)
    body = SimulatedLayer("肢體")  # 肢體確認由其他組員負責
    planner = SimulatedPlanner()
    fallback = "home base"
    if args.robot in ("dry-run", "rim"):
        config = load_config(args.temi_config)
        fallback = config.default_location
        planner = ConfiguredPlanner(fallback)
        robot = robot or build_robot(args, config)
    elif args.robot == "app":
        robot = robot or TemiAppController(args.temi_app_url)
        if zones:
            fallback = zones[0].location  # 位置不明時前往第一個框選區域的觀察點
    robot = robot or SimulatedRobot()
    if zones:
        planner = ZonePlanner(zones, fallback)
    return IncidentManager(planner, robot, voice, body, SimulatedNotifier(), log_path=DEFAULT_LOG)


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


def current_zone(zones, pose, frame):
    """目前人所在的跌倒區域，用來在畫面上標示；不在任何區域內時回傳 None。"""
    shape = getattr(frame.image, "shape", None)
    if not zones or pose is None or shape is None:
        return None
    if pose.visible(LEFT_HIP) and pose.visible(RIGHT_HIP):
        x, y = pose.hip_mid
    elif pose.visible(LEFT_SHOULDER) and pose.visible(RIGHT_SHOULDER):
        x, y = pose.shoulder_mid
    else:
        return None
    zone, inside = locate(zones, x / shape[1], y / shape[0])
    return zone if inside else None


def make_monitor_callback(show=None, alert_seconds: float = 5.0, peak_seconds: float = 5.0, zones=()):
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
        if zones:
            return show(frame, pose, lines, banner, zones=zones, active=current_zone(zones, pose, frame))
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


def show_frame(frame, pose, lines: list[str], banner=None, zones=(), active=None) -> bool:
    import cv2

    image = frame.image.copy()
    if zones:
        draw_zones(image, zones, active)
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
    robot = None
    try:
        zones = load_zones(args.zones)
        if args.robot in ("dry-run", "rim"):
            # 先登入並查好所有觀察點的任務，名稱有誤時立刻停下，不要等到跌倒才發現無法派遣
            config = load_config(args.temi_config)
            robot = build_robot(args, config)
            robot.prepare(sorted({zone.location for zone in zones} | {config.default_location}))
        elif args.robot == "app":
            if not args.temi_app_url:
                raise ValueError("使用 --robot app 時要加上 --temi-app-url，例如 --temi-app-url http://TemiIP:8080")
            # 先確認 App 連得上，所有觀察點也都存在 Temi 上
            robot = TemiAppController(args.temi_app_url)
            robot.prepare(sorted({zone.location for zone in zones}))
    except (RimError, TemiAppError, ValueError) as error:
        logging.error("%s", error)
        raise SystemExit(1)
    if zones:
        logging.info("已載入 %d 個跌倒區域", len(zones))
    else:
        logging.info("尚未設定跌倒區域，跌倒時前往預設觀察點")

    estimator = PoseEstimator(args.model)
    source = VideoSource(parse_source(args.source))
    try:
        callback = None if args.no_display else make_monitor_callback(zones=zones)
        app = FallMonitorApp(estimator, FallDetector(), build_incidents(args, zones, robot))
        app.run(source.frames(), callback)
    except KeyboardInterrupt:
        logging.info("使用者中止")
    finally:
        source.close()
        estimator.close()
        if not args.no_display:
            import cv2

            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
