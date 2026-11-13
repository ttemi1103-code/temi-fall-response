"""跌倒區域與觀察點：依鏡頭看到的跌倒位置，選出 Temi 要前往的觀察點。

區域以畫面比例（0～1）儲存，換解析度也能使用；每個區域對應一個 Temi 上已儲存的地點。
設定區域：python -m fall_response.zones --source 0
"""
import argparse
import itertools
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union

logger = logging.getLogger(__name__)

DEFAULT_ZONES = Path(__file__).resolve().parent.parent / "zones.json"
SETUP_WINDOW = "Fall Zones (drag: add, z: undo, s: save, q: quit)"


@dataclass(frozen=True)
class Zone:
    name: str  # 跌倒區域，例如「沙發前」
    location: str  # Temi 上儲存的觀察點，需和 temi_config.json 的 routes 一致
    rect: tuple[float, float, float, float]  # 左、上、右、下，畫面比例 0～1

    def distance(self, x: float, y: float) -> float:
        left, top, right, bottom = self.rect
        dx = max(left - x, 0.0, x - right)
        dy = max(top - y, 0.0, y - bottom)
        return math.hypot(dx, dy)

    @property
    def area(self) -> float:
        left, top, right, bottom = self.rect
        return (right - left) * (bottom - top)


def load_zones(path: Union[str, Path] = DEFAULT_ZONES) -> list[Zone]:
    """檔案不存在時回傳空清單，代表尚未設定區域。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        zones = []
        for item in data.get("zones", []):
            rect = tuple(float(value) for value in item["rect"])
            left, top, right, bottom = rect
            if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
                raise ValueError(f"區域「{item['name']}」的範圍不正確")
            zones.append(Zone(item["name"], item.get("location") or item["name"], rect))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"區域設定檔格式錯誤：{path}（{error}）") from None
    return zones


def save_zones(path: Union[str, Path], zones: Sequence[Zone]) -> None:
    data = {
        "zones": [
            {"name": zone.name, "location": zone.location, "rect": [round(value, 4) for value in zone.rect]}
            for zone in zones
        ]
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def locate(zones: Sequence[Zone], x: float, y: float) -> Optional[tuple[Zone, bool]]:
    """回傳位置所在的區域；不在任何區域內時回傳最近的區域。第二個值表示是否在區域內。"""
    if not zones:
        return None
    inside = [zone for zone in zones if zone.distance(x, y) == 0.0]
    if inside:
        return min(inside, key=lambda zone: zone.area), True  # 區域重疊時選範圍較小、較精確的
    return min(zones, key=lambda zone: zone.distance(x, y)), False


class ZonePlanner:
    """實作 incident.ObservationPlanner。"""

    def __init__(self, zones: Sequence[Zone], fallback: str):
        self._zones = list(zones)
        self._fallback = fallback

    def choose(self, event) -> str:
        if not self._zones or event.frame_size is None or event.position_px is None:
            logger.warning("沒有可用的跌倒區域或跌倒位置，前往預設觀察點「%s」", self._fallback)
            return self._fallback
        width, height = event.frame_size
        x, y = event.position_px
        zone, inside = locate(self._zones, x / width, y / height)
        if inside:
            logger.warning("跌倒位置在「%s」，派往觀察點「%s」", zone.name, zone.location)
        else:
            logger.warning("跌倒位置不在任何區域內，改用最近的「%s」，派往觀察點「%s」", zone.name, zone.location)
        return zone.location


def setup_frames(video) -> Iterator:
    """即時來源持續更新畫面；照片或影片只取第一張，讓使用者有時間框選。"""
    frames = video.frames()
    if video.is_live:
        return frames
    first = next(frames, None)
    if first is None:
        raise ValueError(f"無法從 {video.source} 讀到畫面")
    return itertools.repeat(first)


def setup(source: str, path: Path) -> None:
    import cv2

    from .overlay import draw_zones
    from .video_input import VideoSource, parse_source

    zones = load_zones(path)
    drag = {"start": None, "end": None, "done": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            drag["start"] = drag["end"] = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and drag["start"]:
            drag["end"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and drag["start"]:
            drag["done"], drag["start"] = (drag["start"], (x, y)), None

    print("在畫面上拖曳滑鼠框出區域，放開後回到這個視窗輸入名稱，每新增一個就會自動儲存。")
    print("按 z 移除上一個；全部框完後關閉影像視窗，或在這裡按 Ctrl+C 結束。")
    print("等待影像中……若一直沒有畫面，請在 PowerShell 按 Ctrl+C 結束。")
    video = VideoSource(parse_source(source))
    window_ready = False
    try:
        for frame in setup_frames(video):
            if not window_ready:
                # 收到第一張畫面才開視窗，否則等待連線時視窗會顯示「沒有回應」
                cv2.namedWindow(SETUP_WINDOW)
                cv2.setMouseCallback(SETUP_WINDOW, on_mouse)
                window_ready = True
            height, width = frame.image.shape[:2]
            image = frame.image.copy()
            draw_zones(image, zones)
            if drag["start"]:
                cv2.rectangle(image, drag["start"], drag["end"], (0, 200, 255), 2)
            cv2.imshow(SETUP_WINDOW, image)
            key = cv2.waitKey(1 if video.is_live else 30) & 0xFF

            if drag["done"]:
                (x1, y1), (x2, y2) = drag["done"]
                drag["done"] = None
                if abs(x2 - x1) > 10 and abs(y2 - y1) > 10:
                    name = input("跌倒區域名稱（例如 沙發前）：").strip()
                    location = input("Temi 觀察點名稱（按 Enter 表示與區域同名）：").strip() or name
                    if name:
                        rect = (min(x1, x2) / width, min(y1, y2) / height, max(x1, x2) / width, max(y1, y2) / height)
                        rect = tuple(min(max(value, 0.0), 1.0) for value in rect)
                        zones.append(Zone(name, location, rect))
                        # 立刻存檔：輸入法或視窗焦點常讓按鍵收不到，不能只靠按 s 儲存
                        save_zones(path, zones)
                        print(f"已新增並儲存：{name} → {location}（共 {len(zones)} 個區域）")
            if key == ord("z") and zones:
                removed = zones.pop()
                save_zones(path, zones)
                print(f"已移除並儲存：{removed.name}（共 {len(zones)} 個區域）")
            elif key in (ord("s"), ord("q")):
                break
            elif cv2.getWindowProperty(SETUP_WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break  # 使用者按 × 關閉視窗
    except KeyboardInterrupt:
        pass
    finally:
        print(f"目前共有 {len(zones)} 個區域，存在 {path}")
        video.close()
        cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description="在鏡頭畫面上框出跌倒區域，並設定對應的 Temi 觀察點")
    parser.add_argument("--source", default="0", help="鏡頭編號、手機串流網址，或現場固定鏡頭拍的照片／影片")
    parser.add_argument("--zones", default=str(DEFAULT_ZONES), help="區域設定檔路徑")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    setup(args.source, Path(args.zones))


if __name__ == "__main__":
    main()
