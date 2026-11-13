"""主流程：影像 → 人體關節點 → 跌倒偵測 → 事件處理。"""
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Callable, Iterable, Optional

from .fall_detector import FallDetector, FallUpdate
from .incident import IncidentManager, IncidentRecord
from .models import Frame, Pose

logger = logging.getLogger(__name__)

MonitorCallback = Callable[[Frame, Optional[Pose], FallUpdate], bool]


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
                    event = update.event
                    shape = getattr(frame.image, "shape", None)
                    if shape is not None:
                        # 偵測器只看得到關節點，畫面大小由這裡補上，才能換算跌倒區域
                        event = replace(event, frame_size=(shape[1], shape[0]))
                    position = tuple(round(v) for v in event.position_px) if event.position_px else "未知"
                    logger.warning("偵測到跌倒：位置=%s 遮擋=%s", position, event.occluded)
                    self._active = self._executor.submit(self._incidents.handle, event)
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
