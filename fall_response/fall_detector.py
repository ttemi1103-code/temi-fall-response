"""跌倒偵測：快速下降 + 倒地姿勢 + 持續一段時間，三者都成立才判定跌倒。

快速下降有兩種判斷方式：瞬間速度夠快，或是剛才還站著、現在已倒地且髖部明顯變低。
後者可以處理摔倒太快、關節點短暫追蹤不到的情況。
蹲下、彎腰會很快恢復；慢慢躺下午睡沒有快速下降，因此都不會觸發。
所有門檻都放在 FallConfig，需要用實際拍攝的資料校正。
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .models import LEFT_HIP, LEFT_SHOULDER, NOSE, RIGHT_HIP, RIGHT_SHOULDER, FallEvent, Pose


@dataclass(frozen=True)
class FallConfig:
    descent_speed_threshold: float = 1.5  # 軀幹長度／秒
    velocity_window_s: float = 0.5
    min_velocity_span_s: float = 0.15
    rapid_drop_window_s: float = 2.0  # 離開站姿後多久內倒地，仍算快速倒下
    rapid_drop_min: float = 0.6  # 髖部至少要比站立時低多少（軀幹長度），可排除彎腰
    down_angle_deg: float = 55.0
    down_aspect_ratio: float = 1.3
    upright_angle_deg: float = 35.0
    posture_timeout_s: float = 2.0  # 快速下降後，等待倒地姿勢出現的時間
    confirm_seconds: float = 3.0  # 維持倒地且幾乎不動多久才判定跌倒
    max_confirm_seconds: float = 8.0  # 跌倒後可能還在掙扎，一直在動也不能無限延後
    still_window_s: float = 1.0
    still_motion_max: float = 0.25  # 這段時間內肩、髖移動不超過多少（軀幹長度）才算不動
    reference_smoothing: float = 0.05


class FallState(Enum):
    MONITORING = "monitoring"
    CANDIDATE = "candidate"  # 偵測到快速下降，等待倒地姿勢
    CONFIRMING = "confirming"  # 倒地中，計時確認
    FALLEN = "fallen"  # 已送出事件，等待 reset()


@dataclass(frozen=True)
class FallUpdate:
    state: FallState
    descent_speed: float
    event: Optional[FallEvent] = None
    moving: bool = False  # 倒地確認中仍在動，判定會延後


class FallDetector:
    def __init__(self, config: FallConfig = FallConfig()):
        self.config = config
        self._reference_torso: Optional[float] = None
        self.reset()

    def reset(self) -> None:
        """事件處理完成後呼叫。站立時的軀幹長度保留，不需要重新學習。"""
        self.state = FallState.MONITORING
        self._history: deque = deque()
        self._motion: deque = deque()
        self._candidate_since = 0.0
        self._fall_started = 0.0
        self._down_since = 0.0
        self._peak_speed = 0.0
        self._occluded = False
        self._last_position: Optional[tuple[float, float]] = None
        self._last_upright: Optional[tuple[float, float]] = None  # (時間, 髖部高度)

    def update(self, pose: Optional[Pose], timestamp: float) -> FallUpdate:
        if self.state is FallState.FALLEN:
            return FallUpdate(self.state, 0.0)

        tracked = pose if pose is not None and pose.torso_reliable else None
        speed = self._observe(pose, tracked, timestamp)
        moving = False

        if self.state is FallState.MONITORING:
            if speed >= self.config.descent_speed_threshold:
                self._enter_candidate(timestamp, speed)
                self._peak_speed = speed
                self._fall_started = timestamp
            elif tracked is not None and self._dropped_from_upright(tracked, timestamp):
                self._enter_candidate(timestamp, speed)
                self._peak_speed = speed
                self._fall_started = self._last_upright[0]
        elif self.state is FallState.CANDIDATE:
            self._peak_speed = max(self._peak_speed, speed)
            if tracked is None or self._is_down(tracked):
                self._enter_confirming(timestamp, occluded=tracked is None)
            elif timestamp - self._candidate_since > self.config.posture_timeout_s:
                self.state = FallState.MONITORING
        elif self.state is FallState.CONFIRMING:
            if tracked is None:
                # 跌倒後被家具遮擋很常見，不能因此取消，交給 Temi 到現場確認
                self._occluded = True
            elif self._is_upright(tracked):
                self.state = FallState.MONITORING
            elif not self._is_down(tracked):
                self._enter_candidate(timestamp, speed)
            else:
                moving = self._is_moving(tracked, timestamp)

        if self.state is FallState.CONFIRMING:
            elapsed = timestamp - self._down_since
            if elapsed >= self.config.confirm_seconds and (
                not moving or elapsed >= self.config.max_confirm_seconds
            ):
                self.state = FallState.FALLEN
                return FallUpdate(self.state, speed, self._build_event(timestamp))
        return FallUpdate(self.state, speed, moving=moving)

    def _observe(self, pose: Optional[Pose], tracked: Optional[Pose], timestamp: float) -> float:
        """更新歷史資料並回傳目前的下降速度；完全追蹤不到時不跨越空窗計算。"""
        points = self._descent_points(pose)
        if not points:
            self._history.clear()
            return 0.0

        if tracked is not None:
            self._last_position = tracked.hip_mid
            if self._is_upright(tracked):
                self._update_reference(tracked.torso_length)
                self._last_upright = (timestamp, tracked.hip_mid[1])

        self._history.append((timestamp, points))
        while timestamp - self._history[0][0] > self.config.velocity_window_s:
            self._history.popleft()

        if self._reference_torso is None:
            return 0.0
        speeds = []
        for name, y in points.items():
            past_time, past_y = next((t, past[name]) for t, past in self._history if name in past)
            span = timestamp - past_time
            if span >= self.config.min_velocity_span_s:
                speeds.append((y - past_y) / span / self._reference_torso)
        return sum(speeds) / len(speeds) if speeds else 0.0

    def _descent_points(self, pose: Optional[Pose]) -> dict[str, float]:
        """頭、肩、髖的高度，看得到幾個就用幾個；彎腰時髖部不動，平均後不易誤判。"""
        if pose is None:
            return {}
        points = {}
        if pose.visible(NOSE):
            points["head"] = pose.landmarks[NOSE].y
        if pose.visible(LEFT_SHOULDER) and pose.visible(RIGHT_SHOULDER):
            points["shoulder"] = pose.shoulder_mid[1]
        if pose.visible(LEFT_HIP) and pose.visible(RIGHT_HIP):
            points["hip"] = pose.hip_mid[1]
        return points

    def _is_moving(self, pose: Pose, timestamp: float) -> bool:
        self._motion.append((timestamp, *pose.shoulder_mid, *pose.hip_mid))
        while timestamp - self._motion[0][0] > self.config.still_window_s:
            self._motion.popleft()
        scale = self._reference_torso or pose.torso_length
        limit = self.config.still_motion_max * scale
        return any(
            max(sample[axis] for sample in self._motion) - min(sample[axis] for sample in self._motion) > limit
            for axis in range(1, 5)
        )

    def _update_reference(self, torso_length: float) -> None:
        # 倒地時軀幹在畫面上會縮短，所以只用直立時的長度當作身體尺度
        if self._reference_torso is None:
            self._reference_torso = torso_length
        else:
            alpha = self.config.reference_smoothing
            self._reference_torso += alpha * (torso_length - self._reference_torso)

    def _dropped_from_upright(self, pose: Pose, timestamp: float) -> bool:
        if self._last_upright is None or self._reference_torso is None or not self._is_down(pose):
            return False
        upright_at, upright_hip_y = self._last_upright
        drop = (pose.hip_mid[1] - upright_hip_y) / self._reference_torso
        return (
            timestamp - upright_at <= self.config.rapid_drop_window_s
            and drop >= self.config.rapid_drop_min
        )

    def _is_wide(self, pose: Pose) -> bool:
        return pose.legs_visible and pose.aspect_ratio >= self.config.down_aspect_ratio

    def _is_down(self, pose: Pose) -> bool:
        return pose.torso_angle_deg >= self.config.down_angle_deg or self._is_wide(pose)

    def _is_upright(self, pose: Pose) -> bool:
        return pose.torso_angle_deg <= self.config.upright_angle_deg and not self._is_wide(pose)

    def _enter_candidate(self, timestamp: float, speed: float) -> None:
        self.state = FallState.CANDIDATE
        self._candidate_since = timestamp
        self._peak_speed = max(self._peak_speed, speed)

    def _enter_confirming(self, timestamp: float, occluded: bool) -> None:
        self.state = FallState.CONFIRMING
        self._down_since = timestamp
        self._motion.clear()
        self._occluded = self._occluded or occluded

    def _build_event(self, timestamp: float) -> FallEvent:
        onset_delay = timestamp - self._fall_started
        return FallEvent(
            timestamp=timestamp,
            wall_time=(datetime.now() - timedelta(seconds=onset_delay)).isoformat(timespec="seconds"),
            position_px=self._last_position,
            peak_descent_speed=self._peak_speed,
            occluded=self._occluded,
            onset_delay_s=onset_delay,
        )
