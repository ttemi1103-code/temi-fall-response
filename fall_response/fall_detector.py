"""跌倒偵測，有兩條判定路徑：

1. 快速跌倒：快速下降 + 倒地姿勢 + 倒地後大致靜止 3 秒。
   快速下降有兩種判斷方式：瞬間速度夠快，或是剛才還站著、現在已倒地且髖部明顯變低。
   後者可以處理摔倒太快、關節點短暫追蹤不到的情況。
2. 慢慢癱倒：在地面區域（框選的跌倒區域）維持倒地姿勢超過 long_down_seconds。
   長者常是重心不穩後慢慢倒下，且跌倒後起不來；只看地面區域，避免把躺沙發當成跌倒。

蹲下、彎腰會很快恢復；在地面區域以外慢慢躺下沒有快速下降，因此都不會觸發。
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
    long_down_seconds: float = 30.0  # 在地面區域倒地超過多久，即使沒有快速下降也判定
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
    floor_down_s: float = 0.0  # 已在地面區域倒地幾秒（慢慢癱倒的計時）


class FallDetector:
    def __init__(self, config: FallConfig = FallConfig()):
        self.config = config
        self._reference_torso: Optional[float] = None
        # 判定後人通常還躺著，要先看到他站起來，地面停留計時才會重新啟用，避免重複警報
        self._floor_rearm_needed = False
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
        self._floor_since: Optional[float] = None
        self._floor_occluded = False

    def update(self, pose: Optional[Pose], timestamp: float, on_floor: bool = False) -> FallUpdate:
        """on_floor：人的髖部是否在地面區域內，由呼叫端依框選的跌倒區域判斷。"""
        if self.state is FallState.FALLEN:
            return FallUpdate(self.state, 0.0)

        tracked = pose if pose is not None and pose.torso_reliable else None
        speed = self._observe(pose, tracked, timestamp)
        floor_down_s = self._track_floor(tracked, on_floor, timestamp)
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

        if not self._floor_rearm_needed and floor_down_s >= self.config.long_down_seconds:
            # 慢慢癱倒：沒有快速下降，但在地面上倒地太久
            self._fall_started = self._floor_since
            self._occluded = self._occluded or self._floor_occluded
            self.state = FallState.FALLEN
            return FallUpdate(self.state, speed, self._build_event(timestamp), floor_down_s=floor_down_s)
        return FallUpdate(self.state, speed, moving=moving, floor_down_s=floor_down_s)

    def _track_floor(self, tracked: Optional[Pose], on_floor: bool, timestamp: float) -> float:
        """回傳已在地面區域倒地的秒數；站起來或離開地面區域就重新計時，短暫被遮擋不重新計時。"""
        if tracked is not None:
            if self._is_upright(tracked):
                self._floor_rearm_needed = False
                self._floor_since = None
            elif on_floor and self._is_down(tracked):
                if self._floor_since is None:
                    self._floor_since = timestamp
                    self._floor_occluded = False
            else:
                self._floor_since = None
        elif self._floor_since is not None:
            self._floor_occluded = True
        if self._floor_since is None or self._floor_rearm_needed:
            return 0.0
        return timestamp - self._floor_since

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
        self._floor_rearm_needed = True
        onset_delay = timestamp - self._fall_started
        return FallEvent(
            timestamp=timestamp,
            wall_time=(datetime.now() - timedelta(seconds=onset_delay)).isoformat(timespec="seconds"),
            position_px=self._last_position,
            peak_descent_speed=self._peak_speed,
            occluded=self._occluded,
            onset_delay_s=onset_delay,
        )
