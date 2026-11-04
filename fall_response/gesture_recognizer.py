"""肢體回應辨識：舉手、揮手、點頭、搖頭（對應報告的第二層確認）。

長者可能躺在地上，因此以「身體座標」判斷，不用畫面的上下左右：
u 軸沿軀幹由肩指向髖，v 軸與軀幹垂直，長度則以軀幹長度為單位。
"""
import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from .models import LEFT_SHOULDER, LEFT_WRIST, NOSE, RIGHT_SHOULDER, RIGHT_WRIST, Pose


class Gesture(Enum):
    RAISE_HAND = "raise_hand"
    WAVE = "wave"
    NOD = "nod"
    SHAKE_HEAD = "shake_head"


@dataclass(frozen=True)
class GestureConfig:
    window_s: float = 4.0
    raise_hold_s: float = 0.3
    raise_min_displacement: float = 0.35  # 必須真的抬手，原本就舉著不算回應
    raise_above_shoulder: float = 0.0
    raise_lateral: float = 0.6  # 躺著時手臂往身體外側抬起
    wave_amplitude: float = 0.15
    wave_reversals: int = 2
    head_amplitude: float = 0.05
    head_reversals: int = 1  # 頭往下再回來就算一次，門檻越低越敏銳，也越容易誤判
    head_axis_dominance: float = 1.3
    shoulder_to_torso: float = 1.25  # 看不到髖部時，用肩寬估計軀幹長度


def count_reversals(values: Iterable[float], min_amplitude: float) -> int:
    """計算來回次數；小於 min_amplitude 的抖動不計。"""
    iterator = iter(values)
    pivot = next(iterator, None)
    if pivot is None:
        return 0
    direction = reversals = 0
    for value in iterator:
        if direction == 0:
            if abs(value - pivot) >= min_amplitude:
                direction = 1 if value > pivot else -1
                pivot = value
        elif (value - pivot) * direction > 0:
            pivot = value
        elif abs(value - pivot) >= min_amplitude:
            reversals += 1
            direction = -direction
            pivot = value
    return reversals


class GestureRecognizer:
    def __init__(self, config: GestureConfig = GestureConfig()):
        self.config = config
        self.reset()

    def reset(self) -> None:
        """每次 Temi 下達動作指令時呼叫，只計算指令之後的動作。"""
        self._samples: deque = deque()
        self._scale = 0.0
        self._baselines: dict[int, tuple[float, float]] = {}
        self._raised_since: dict[int, float] = {}
        self._detected: set[Gesture] = set()

    def update(self, pose: Optional[Pose], timestamp: float) -> frozenset[Gesture]:
        """回傳本輪至今偵測到的所有動作；追蹤中斷時保留已偵測結果。"""
        axes = self._body_axes(pose) if pose is not None else None
        if axes is None:
            return frozenset(self._detected)

        self._scale = max(self._scale, axes[2])
        sample = {index: self._to_body_frame(pose, index, axes) for index in (NOSE, LEFT_WRIST, RIGHT_WRIST)}
        self._samples.append((timestamp, sample))
        while timestamp - self._samples[0][0] > self.config.window_s:
            self._samples.popleft()

        self._check_raise(sample, timestamp)
        self._check_wave()
        self._check_head()
        return frozenset(self._detected)

    def _body_axes(self, pose: Pose) -> Optional[tuple[float, float, float]]:
        """回傳由肩指向髖的單位向量與軀幹長度；看不到髖部時改由肩線推算。"""
        if pose.torso_reliable:
            (sx, sy), (hx, hy) = pose.shoulder_mid, pose.hip_mid
            length = pose.torso_length
            return (hx - sx) / length, (hy - sy) / length, length

        if not (pose.visible(LEFT_SHOULDER) and pose.visible(RIGHT_SHOULDER)):
            return None
        left, right = pose.landmarks[LEFT_SHOULDER], pose.landmarks[RIGHT_SHOULDER]
        width = math.hypot(right.x - left.x, right.y - left.y)
        if width <= 1.0:
            return None
        ux, uy = -(right.y - left.y) / width, (right.x - left.x) / width
        # 與肩線垂直的方向有兩個，要選遠離頭部的那一個
        sx, sy = pose.shoulder_mid
        if pose.visible(NOSE):
            toward_nose = (pose.landmarks[NOSE].x - sx) * ux + (pose.landmarks[NOSE].y - sy) * uy
            flip = toward_nose > 0
        else:
            flip = uy < 0
        if flip:
            ux, uy = -ux, -uy
        return ux, uy, width * self.config.shoulder_to_torso

    def _to_body_frame(
        self, pose: Pose, index: int, axes: tuple[float, float, float]
    ) -> Optional[tuple[float, float]]:
        if not pose.visible(index):
            return None
        ux, uy, _ = axes
        sx, sy = pose.shoulder_mid
        dx, dy = pose.landmarks[index].x - sx, pose.landmarks[index].y - sy
        return dx * ux + dy * uy, dx * -uy + dy * ux

    def _check_raise(self, sample: dict, timestamp: float) -> None:
        for wrist in (LEFT_WRIST, RIGHT_WRIST):
            point = sample[wrist]
            if point is None:
                self._raised_since.pop(wrist, None)
                continue
            baseline = self._baselines.setdefault(wrist, point)
            moved = math.hypot(point[0] - baseline[0], point[1] - baseline[1]) / self._scale
            above_shoulder = point[0] / self._scale <= -self.config.raise_above_shoulder
            lifted_out = abs(point[1]) / self._scale >= self.config.raise_lateral
            if moved >= self.config.raise_min_displacement and (above_shoulder or lifted_out):
                since = self._raised_since.setdefault(wrist, timestamp)
                if timestamp - since >= self.config.raise_hold_s:
                    self._detected.add(Gesture.RAISE_HAND)
            else:
                self._raised_since.pop(wrist, None)

    def _check_wave(self) -> None:
        amplitude = self.config.wave_amplitude * self._scale
        for wrist in (LEFT_WRIST, RIGHT_WRIST):
            points = [sample[wrist] for _, sample in self._samples if sample[wrist] is not None]
            for axis in (0, 1):
                series = [point[axis] for point in points]
                if count_reversals(series, amplitude) >= self.config.wave_reversals:
                    self._detected.add(Gesture.WAVE)

    def _check_head(self) -> None:
        points = [sample[NOSE] for _, sample in self._samples if sample[NOSE] is not None]
        if len(points) < 2:
            return
        along = [point[0] for point in points]
        across = [point[1] for point in points]
        range_along, range_across = max(along) - min(along), max(across) - min(across)
        amplitude = self.config.head_amplitude * self._scale
        dominance = self.config.head_axis_dominance
        if (
            range_along >= dominance * range_across
            and count_reversals(along, amplitude) >= self.config.head_reversals
        ):
            self._detected.add(Gesture.NOD)
        if (
            range_across >= dominance * range_along
            and count_reversals(across, amplitude) >= self.config.head_reversals
        ):
            self._detected.add(Gesture.SHAKE_HEAD)
