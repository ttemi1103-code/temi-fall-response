"""主流程：影像 → 肢體辨識 → 跌倒偵測 → 事件處理。"""
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, Optional

from .fall_detector import FallDetector, FallUpdate
from .gesture_recognizer import Gesture, GestureRecognizer
from .incident import IncidentManager, IncidentRecord
from .models import Frame, Pose

logger = logging.getLogger(__name__)

MonitorCallback = Callable[[Frame, Optional[Pose], FallUpdate], bool]
GestureCallback = Callable[[Frame, Optional[Pose], frozenset], bool]


class FallMonitorApp:
    """事件處理放在背景執行緒，Temi 移動與對話期間影像仍持續處理。"""

    def __init__(self, estimator, detector: FallDetector, incidents: IncidentManager):
        self._estimator = estimator
        self._detector = detector
        self._incidents = incidents
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="incident")
        self._active: Optional[Future] = None
        self.records: list[IncidentRecord] = []

    def run(self, frames: Iterable[Frame], on_frame: Optional[MonitorCallback] = None) -> None:
        """on_frame 回傳 False 時結束。"""
        try:
            for frame in frames:
                pose = self._estimator.estimate(frame)
                update = self._detector.update(pose, frame.timestamp)
                if update.event is not None:
                    logger.warning(
                        "偵測到跌倒：位置=%s 遮擋=%s",
                        tuple(round(v) for v in update.event.position_px),
                        update.event.occluded,
                    )
                    self._active = self._executor.submit(self._incidents.handle, update.event)
                self._collect_finished()
                if on_frame is not None and on_frame(frame, pose, update) is False:
                    break
        finally:
            self._executor.shutdown(wait=True)
            self._collect_finished()

    def _collect_finished(self) -> None:
        if self._active is None or not self._active.done():
            return
        try:
            self.records.append(self._active.result())
        except Exception:
            logger.exception("事件處理發生未預期錯誤")
        self._active = None
        self._detector.reset()


def run_gesture_demo(
    estimator,
    recognizer: GestureRecognizer,
    frames: Iterable[Frame],
    on_frame: Optional[GestureCallback] = None,
    hold_s: float = 2.0,
) -> frozenset:
    """單獨測試肢體回應辨識，回傳偵測到的所有動作。

    偵測結果顯示 hold_s 秒後清除，方便一次接一次測試。
    """
    seen: frozenset[Gesture] = frozenset()
    shown: frozenset[Gesture] = frozenset()
    shown_at = 0.0
    for frame in frames:
        pose = estimator.estimate(frame)
        current = recognizer.update(pose, frame.timestamp)
        new = current - shown
        for gesture in new:
            logger.info("偵測到動作：%s", gesture.value)
        if new:
            shown_at = frame.timestamp
        elif current and frame.timestamp - shown_at >= hold_s:
            recognizer.reset()
            current = frozenset()
        shown = current
        seen |= current
        if on_frame is not None and on_frame(frame, pose, shown) is False:
            break
    return seen
