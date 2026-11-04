"""各模組共用的資料結構。座標一律為像素，避免寬高比不同造成角度失真。"""
import math
from dataclasses import dataclass
from typing import Any, Sequence

# MediaPipe Pose 的 33 個關節點編號（只列出本系統用到的）
NOSE = 0
LEFT_SHOULDER, RIGHT_SHOULDER = 11, 12
LEFT_WRIST, RIGHT_WRIST = 15, 16
LEFT_HIP, RIGHT_HIP = 23, 24
LEFT_KNEE, RIGHT_KNEE = 25, 26
TORSO_POINTS = (LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP)

MIN_VISIBILITY = 0.5


@dataclass(frozen=True)
class Frame:
    image: Any  # BGR 影像（numpy array）
    timestamp: float  # 秒；即時影像為 monotonic 時間，影片檔為影片內時間
    index: int


@dataclass(frozen=True)
class Landmark:
    x: float
    y: float
    visibility: float = 1.0


@dataclass(frozen=True)
class Pose:
    landmarks: Sequence[Landmark]

    def visible(self, index: int) -> bool:
        return self.landmarks[index].visibility >= MIN_VISIBILITY

    @property
    def torso_reliable(self) -> bool:
        """肩、髖都看得到且軀幹有長度，才能用來判斷姿勢。"""
        return all(self.visible(i) for i in TORSO_POINTS) and self.torso_length > 1.0

    @property
    def legs_visible(self) -> bool:
        """只拍到上半身時，寬高比會被手臂影響，不能用來判斷姿勢。"""
        return self.visible(LEFT_KNEE) or self.visible(RIGHT_KNEE)

    @property
    def shoulder_mid(self) -> tuple[float, float]:
        return self._mid(LEFT_SHOULDER, RIGHT_SHOULDER)

    @property
    def hip_mid(self) -> tuple[float, float]:
        return self._mid(LEFT_HIP, RIGHT_HIP)

    @property
    def torso_length(self) -> float:
        (sx, sy), (hx, hy) = self.shoulder_mid, self.hip_mid
        return math.hypot(hx - sx, hy - sy)

    @property
    def torso_angle_deg(self) -> float:
        """軀幹與鉛直方向的夾角：0 度為直立、90 度為平躺、180 度為頭下腳上。"""
        (sx, sy), (hx, hy) = self.shoulder_mid, self.hip_mid
        return math.degrees(math.atan2(abs(hx - sx), hy - sy))

    @property
    def aspect_ratio(self) -> float:
        """可見關節點外框的寬高比，平躺時會明顯大於 1。"""
        points = [lm for lm in self.landmarks if lm.visibility >= MIN_VISIBILITY]
        if not points:
            return 0.0
        width = max(p.x for p in points) - min(p.x for p in points)
        height = max(p.y for p in points) - min(p.y for p in points)
        return width / max(height, 1.0)

    def _mid(self, a: int, b: int) -> tuple[float, float]:
        first, second = self.landmarks[a], self.landmarks[b]
        return (first.x + second.x) / 2, (first.y + second.y) / 2


@dataclass(frozen=True)
class FallEvent:
    timestamp: float  # 與 Frame.timestamp 同一時間軸
    wall_time: str  # 開始跌倒的實際時間（ISO 格式），給事件摘要使用
    position_px: tuple[float, float]  # 最後看到的髖部位置，下一步用來判斷跌倒區域
    peak_descent_speed: float  # 單位：軀幹長度／秒
    occluded: bool  # 確認期間人體曾被遮擋或離開畫面
    onset_delay_s: float = 0.0  # 從開始跌倒到系統判定跌倒經過的秒數
