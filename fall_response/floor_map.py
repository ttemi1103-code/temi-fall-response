"""把鏡頭畫面上的跌倒位置換成 Temi 地圖座標，派 Temi 停到傷患旁邊觀察。

原理：地板是一個平面，只要量好地板上至少 4 個點「在畫面上的位置」和「在 Temi 地圖上的座標」，
就能用透視轉換（homography）把畫面上任何一點換成地圖座標。

量測：python -m fall_response.floor_map --source 0 --temi-app-url http://TemiIP:8080
使用：python -m fall_response.main --source 0 --robot app --temi-app-url http://TemiIP:8080 --near-person

只有 Temi SDK（Java App）能依座標移動，RIM2 只能執行事先建立的任務。
"""
import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

logger = logging.getLogger(__name__)

DEFAULT_FLOOR_MAP = Path(__file__).resolve().parent.parent / "floor_map.json"
SETUP_WINDOW = "Floor Map (left: add point, right: check, z: undo, q: quit)"
MIN_POINTS = 4


@dataclass(frozen=True)
class CalibrationPoint:
    image: tuple[float, float]  # 畫面比例 0～1
    map: Optional[tuple[float, float]] = None  # Temi 地圖座標（公尺）；還沒量時為 None


@dataclass(frozen=True)
class MapTarget:
    x: float
    y: float
    yaw: float  # 弧度，Temi 停下後面向的方向

    def label(self) -> str:
        return f"傷患旁 (x={self.x:.2f}, y={self.y:.2f}, yaw={self.yaw:.2f})"


class Homography:
    """畫面比例座標 → 地圖座標的透視轉換。"""

    def __init__(self, h: Sequence[float]):
        self._h = tuple(h)

    @classmethod
    def fit(cls, pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> "Homography":
        """用最小平方法求解；剛好 4 個點時就是精確解。點太少、太接近或排成一直線時丟出 ValueError。"""
        if len(pairs) < MIN_POINTS:
            raise ValueError(f"至少需要 {MIN_POINTS} 個已量測的點，目前只有 {len(pairs)} 個")
        rows, values = [], []
        for (u, v), (x, y) in pairs:
            rows.append([u, v, 1.0, 0.0, 0.0, 0.0, -u * x, -v * x])
            values.append(x)
            rows.append([0.0, 0.0, 0.0, u, v, 1.0, -u * y, -v * y])
            values.append(y)
        normal = [[sum(r[i] * r[j] for r in rows) for j in range(8)] for i in range(8)]
        rhs = [sum(r[i] * b for r, b in zip(rows, values)) for i in range(8)]
        return cls(_solve(normal, rhs) + [1.0])

    def to_map(self, u: float, v: float) -> tuple[float, float]:
        h = self._h
        w = h[6] * u + h[7] * v + h[8]
        if abs(w) < 1e-12:
            raise ValueError("這個位置無法換算成地圖座標")
        return (h[0] * u + h[1] * v + h[2]) / w, (h[3] * u + h[4] * v + h[5]) / w


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """高斯消去法（部分選主元）。"""
    n = len(rhs)
    a = [row[:] + [b] for row, b in zip(matrix, rhs)]
    scale = max(abs(value) for row in matrix for value in row) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-10 * scale:
            raise ValueError("量測點太接近或排成一直線，請把點分散在地板的四個角落")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= factor * a[col][c]
    result = [0.0] * n
    for r in range(n - 1, -1, -1):
        result[r] = (a[r][n] - sum(a[r][c] * result[c] for c in range(r + 1, n))) / a[r][r]
    return result


@dataclass
class FloorMap:
    points: list[CalibrationPoint] = field(default_factory=list)
    standoff_m: float = 1.0  # Temi 停在離傷患多遠的地方
    approach_from: Optional[tuple[float, float]] = None  # 查不到 Temi 目前位置時，假設 Temi 從這裡過來（例如充電座）

    @property
    def measured(self) -> list[CalibrationPoint]:
        return [point for point in self.points if point.map is not None]

    @property
    def complete(self) -> bool:
        return len(self.measured) >= MIN_POINTS

    def homography(self) -> Homography:
        return Homography.fit([(point.image, point.map) for point in self.measured])

    def errors(self) -> list[float]:
        """每個量測點換算後和實際座標差幾公尺；超過 4 個點時可以用來檢查量測是否準確。"""
        homography = self.homography()
        result = []
        for point in self.measured:
            x, y = homography.to_map(*point.image)
            result.append(math.hypot(x - point.map[0], y - point.map[1]))
        return result

    def covers(self, u: float, v: float) -> bool:
        """是否在量測點圍出的範圍內；範圍外仍可換算，但誤差較大。"""
        us = [point.image[0] for point in self.measured]
        vs = [point.image[1] for point in self.measured]
        return bool(us) and min(us) <= u <= max(us) and min(vs) <= v <= max(vs)


def load_floor_map(path: Union[str, Path] = DEFAULT_FLOOR_MAP) -> Optional[FloorMap]:
    """檔案不存在時回傳 None。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        points = []
        for item in data.get("points", []):
            u, v = (float(value) for value in item["image"])
            mapped = item.get("map")
            points.append(CalibrationPoint((u, v), tuple(float(value) for value in mapped) if mapped else None))
        approach = data.get("approach_from")
        return FloorMap(
            points,
            float(data.get("standoff_m", 1.0)),
            tuple(float(value) for value in approach) if approach else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"地板座標設定檔格式錯誤：{path}（{error}）") from None


def save_floor_map(path: Union[str, Path], floor_map: FloorMap) -> None:
    data = {
        "standoff_m": floor_map.standoff_m,
        "approach_from": list(floor_map.approach_from) if floor_map.approach_from else None,
        "points": [
            {
                "image": [round(value, 4) for value in point.image],
                "map": [round(value, 3) for value in point.map] if point.map else None,
            }
            for point in floor_map.points
        ],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def standoff_target(person: tuple[float, float], start: tuple[float, float], distance: float) -> MapTarget:
    """Temi 從 start 靠近傷患，停在距離傷患 distance 公尺、面向傷患的地方。"""
    dx, dy = start[0] - person[0], start[1] - person[1]
    gap = math.hypot(dx, dy)
    if gap <= distance:
        x, y = start  # 已經夠近，不用再往前，只轉向傷患
    else:
        x, y = person[0] + dx / gap * distance, person[1] + dy / gap * distance
    if math.hypot(person[0] - x, person[1] - y) < 1e-6:
        x, y = person[0] + distance, person[1]  # 起點剛好在傷患位置，方向不明時從 x 方向靠近
    return MapTarget(x, y, math.atan2(person[1] - y, person[0] - x))


class NearPersonDispatch:
    """同時實作 incident.ObservationPlanner 和 incident.RobotController：派 Temi 停到傷患旁。

    算不出傷患位置時，改用 fallback_planner 選的觀察點；派往傷患旁失敗（例如路被擋住）時，
    在剩下的時間內改去該觀察點。app 是 TemiAppController。
    """

    confirms_arrival = True

    def __init__(self, floor_map: FloorMap, app, fallback_planner, clock: Callable[[], float] = time.monotonic):
        self._floor_map = floor_map
        self._homography = floor_map.homography()
        self._app = app
        self._fallback_planner = fallback_planner
        self._clock = clock
        self._pending: Optional[tuple[str, MapTarget, str]] = None

    def choose(self, event) -> str:
        fallback = self._fallback_planner.choose(event)
        target = self._target_for(event)
        if target is None:
            self._pending = None
            return fallback
        label = target.label()
        self._pending = (label, target, fallback)
        logger.warning("派 Temi 到%s，到不了時改去觀察點「%s」", label, fallback)
        return label

    def go_to(self, location: str, timeout_s: float) -> bool:
        pending = self._pending
        if pending is None or pending[0] != location:
            return self._app.go_to(location, timeout_s)
        _, target, fallback = pending
        deadline = self._clock() + timeout_s
        try:
            return self._app.go_to_position(target.x, target.y, target.yaw, timeout_s)
        except Exception as error:
            remaining = deadline - self._clock()
            logger.warning("無法到傷患旁（%s），改去觀察點「%s」", error, fallback)
            if remaining <= 0:
                return False
            return self._app.go_to(fallback, remaining)

    def _target_for(self, event) -> Optional[MapTarget]:
        if event.position_px is None or event.frame_size is None:
            logger.warning("不知道跌倒位置，無法派到傷患旁")
            return None
        width, height = event.frame_size
        u, v = event.position_px[0] / width, event.position_px[1] / height
        try:
            person = self._homography.to_map(u, v)
        except ValueError as error:
            logger.warning("%s", error)
            return None
        if not self._floor_map.covers(u, v):
            logger.warning("跌倒位置在地板量測範圍外，換算的座標誤差可能較大")
        start = self._robot_position() or self._floor_map.approach_from
        if start is None:
            logger.warning("查不到 Temi 目前位置，floor_map.json 也沒有設定 approach_from")
            return None
        logger.info("傷患的地圖座標約為 (%.2f, %.2f)", *person)
        return standoff_target(person, start, self._floor_map.standoff_m)

    def _robot_position(self) -> Optional[tuple[float, float]]:
        try:
            return self._app.position()
        except Exception as error:
            logger.warning("查不到 Temi 目前位置：%s", error)
            return None


def setup(source: str, path: Path, app=None) -> None:
    """在畫面上點地板標記點；有連 Temi App 時，同時記下 Temi 目前的地圖座標。"""
    import cv2

    from .overlay import draw_texts
    from .video_input import VideoSource, parse_source
    from .zones import setup_frames

    floor_map = load_floor_map(path) or FloorMap()
    clicks = {"add": None, "check": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks["add"] = (x, y)
        elif event == cv2.EVENT_RBUTTONDOWN:
            clicks["check"] = (x, y)

    print("把 Temi 停在地板上的標記點，再用滑鼠左鍵點畫面上 Temi 底部的中心，就會記下一組座標並自動儲存。")
    print("至少要 4 個點，分散在地板的四個角落，不要排成一直線。")
    print("滑鼠右鍵：顯示該點換算後的地圖座標，用來檢查。z：移除上一個點。關閉視窗或按 q 結束。")
    if app is None:
        print("沒有加 --temi-app-url，只會記下畫面位置，地圖座標要自己填進設定檔的 map 欄位。")
    video = VideoSource(parse_source(source))
    window_ready = False
    try:
        for frame in setup_frames(video):
            if not window_ready:
                cv2.namedWindow(SETUP_WINDOW)
                cv2.setMouseCallback(SETUP_WINDOW, on_mouse)
                window_ready = True
            height, width = frame.image.shape[:2]

            if clicks["add"]:
                x, y = clicks["add"]
                clicks["add"] = None
                mapped = None
                if app is not None:
                    try:
                        mapped = app.position()
                    except Exception as error:
                        print(f"查不到 Temi 位置，這個點只記下畫面位置：{error}")
                floor_map.points.append(CalibrationPoint((x / width, y / height), mapped))
                save_floor_map(path, floor_map)
                where = f"地圖 ({mapped[0]:.2f}, {mapped[1]:.2f})" if mapped else "地圖座標未填"
                print(f"已新增第 {len(floor_map.points)} 個點：畫面 ({x}, {y}) → {where}")
                report_accuracy(floor_map)
            if clicks["check"]:
                x, y = clicks["check"]
                clicks["check"] = None
                if floor_map.complete:
                    mx, my = floor_map.homography().to_map(x / width, y / height)
                    print(f"畫面 ({x}, {y}) → 地圖約 ({mx:.2f}, {my:.2f})")
                else:
                    print(f"至少要有 {MIN_POINTS} 個已量測的點才能換算")

            image = frame.image.copy()
            labels = []
            for index, point in enumerate(floor_map.points, start=1):
                px, py = int(point.image[0] * width), int(point.image[1] * height)
                color = (0, 200, 0) if point.map else (0, 140, 255)
                cv2.circle(image, (px, py), 6, color, -1)
                text = f"{index}" + (f" ({point.map[0]:.1f}, {point.map[1]:.1f})" if point.map else " 未填")
                labels.append((text, (px + 8, py - 12), color))
            draw_texts(image, labels)
            cv2.imshow(SETUP_WINDOW, image)
            key = cv2.waitKey(1 if video.is_live else 30) & 0xFF
            if key == ord("z") and floor_map.points:
                floor_map.points.pop()
                save_floor_map(path, floor_map)
                print(f"已移除上一個點（共 {len(floor_map.points)} 個）")
            elif key == ord("q"):
                break
            elif cv2.getWindowProperty(SETUP_WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
    except KeyboardInterrupt:
        pass
    finally:
        print(f"目前共有 {len(floor_map.points)} 個點（已量測 {len(floor_map.measured)} 個），存在 {path}")
        video.close()
        cv2.destroyAllWindows()


def report_accuracy(floor_map: FloorMap) -> None:
    if not floor_map.complete:
        print(f"還需要 {MIN_POINTS - len(floor_map.measured)} 個已量測的點")
        return
    try:
        errors = floor_map.errors()
    except ValueError as error:
        print(error)
        return
    if len(errors) > MIN_POINTS:
        print(f"換算誤差：平均 {sum(errors) / len(errors):.2f} 公尺，最大 {max(errors):.2f} 公尺")
    else:
        print("已可換算；再多量 1 個以上的點，就能檢查換算誤差")


def main() -> None:
    parser = argparse.ArgumentParser(description="量測地板座標，讓 Temi 能依跌倒位置停到傷患旁")
    parser.add_argument("--source", default="0", help="鏡頭編號、手機串流網址，或現場固定鏡頭拍的照片／影片")
    parser.add_argument("--floor-map", default=str(DEFAULT_FLOOR_MAP), help="地板座標設定檔路徑")
    parser.add_argument("--temi-app-url", help="Temi Java App 的網址；加上後點畫面時會自動讀取 Temi 目前座標")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = None
    if args.temi_app_url:
        from .temi_app import TemiAppController

        app = TemiAppController(args.temi_app_url)
    setup(args.source, Path(args.floor_map), app)


if __name__ == "__main__":
    main()
