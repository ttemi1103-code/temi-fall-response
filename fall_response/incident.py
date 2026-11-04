"""跌倒事件流程，依簡報第 14 頁的流程樹狀圖：

Temi 前往 → 語音確認 ─ 有回應 → 肢體確認 → 傳送摘要 ─ 已查看 → 由聯絡人決定後續
                     │                              └ 未查看 → Temi 撥打聯絡人
                     └ 無回應（或 Temi 無法到場）→ 直接撥打聯絡人

Temi、語音、肢體與通知都用介面隔開，目前預設接 Simulated 版本，不會有實際動作。
流程由這裡的固定規則決定，LLM 只整理內容，不判斷傷勢，也不決定後續處置。
"""
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol

from .models import FallEvent

logger = logging.getLogger(__name__)


class Outcome(Enum):
    SUMMARY_SENT = "summary_sent"  # 有語音回應，已傳送事件摘要
    DIRECT_CALL = "direct_call"  # 無語音回應或 Temi 無法到場，直接撥打


class BodyStatus(Enum):
    COMPLETED = "已完成肢體指令"
    NOT_COMPLETED = "未能完成肢體指令"
    UNAVAILABLE = "無法進行肢體確認"  # 鏡頭或程式異常，不代表長者做不到


@dataclass(frozen=True)
class LayerResult:
    responded: bool
    details: dict = field(default_factory=dict)


@dataclass
class IncidentRecord:
    event: FallEvent
    observation_point: Optional[str] = None
    temi_arrived: bool = False
    voice_responded: bool = False
    voice_details: dict = field(default_factory=dict)
    body_status: Optional[BodyStatus] = None
    body_details: dict = field(default_factory=dict)
    outcome: Optional[Outcome] = None
    contact_acknowledged: Optional[bool] = None
    contact_called: bool = False
    errors: list = field(default_factory=list)
    timeline: dict = field(default_factory=dict)  # 各階段耗時（秒）


def build_summary(record: IncidentRecord) -> dict:
    """整理給緊急聯絡人的事件摘要；只列可觀察到的資訊，沒有資料就顯示未取得。"""

    def yes_no(value: Optional[bool], yes: str, no: str) -> str:
        return "未取得" if value is None else (yes if value else no)

    details = record.voice_details
    return {
        "跌倒時間": record.event.wall_time,
        "觀察位置": record.observation_point or "未取得",
        "Temi 到場": "是" if record.temi_arrived else "否",
        "回應方式": "語音" if record.voice_responded else "無語音回應",
        "長者描述": details.get("transcript") or "未取得",
        "疼痛部位": details.get("pain_location") or "未取得",
        "自行起身": yes_no(details.get("can_stand_up"), "可以", "無法"),
        "需要協助": yes_no(details.get("needs_help"), "是", "否"),
        "肢體反應": record.body_status.value if record.body_status else "未進行",
    }


class ObservationPlanner(Protocol):
    def choose(self, event: FallEvent) -> str: ...


class RobotController(Protocol):
    def go_to(self, location: str, timeout_s: float) -> bool: ...


class ResponseLayer(Protocol):
    name: str

    def confirm(self, record: IncidentRecord, timeout_s: float) -> LayerResult: ...


class Notifier(Protocol):
    def send_summary(self, record: IncidentRecord, summary: dict) -> None: ...

    def wait_for_ack(self, record: IncidentRecord, timeout_s: float) -> bool: ...

    def call_contact(self, record: IncidentRecord, summary: dict) -> None: ...


class SimulatedPlanner:
    def choose(self, event: FallEvent) -> str:
        return "home base"


class SimulatedRobot:
    def go_to(self, location: str, timeout_s: float) -> bool:
        logger.info("[模擬] Temi 前往 %s", location)
        return True


class SimulatedLayer:
    def __init__(self, name: str):
        self.name = name

    def confirm(self, record: IncidentRecord, timeout_s: float) -> LayerResult:
        logger.info("[模擬] %s確認：尚未實作，視為無回應", self.name)
        return LayerResult(responded=False)


class SimulatedNotifier:
    def send_summary(self, record: IncidentRecord, summary: dict) -> None:
        logger.warning("[模擬] 傳送事件摘要：%s", summary)

    def wait_for_ack(self, record: IncidentRecord, timeout_s: float) -> bool:
        logger.info("[模擬] 緊急聯絡人已查看摘要")
        return True

    def call_contact(self, record: IncidentRecord, summary: dict) -> None:
        logger.warning("[模擬] 撥打緊急聯絡人（未實際撥打）：%s", summary)


@dataclass(frozen=True)
class IncidentTimeouts:
    navigation_s: float = 60.0
    per_layer_s: float = 20.0
    overall_s: float = 120.0  # Temi 前往加上兩層確認的總上限
    notify_s: float = 10.0
    ack_s: float = 60.0
    grace_s: float = 2.0  # 各階段會自行依時限結束，這裡多留一點時間才判定卡住


class IncidentManager:
    def __init__(
        self,
        planner: ObservationPlanner,
        robot: RobotController,
        voice_layer: ResponseLayer,
        body_layer: ResponseLayer,
        notifier: Notifier,
        timeouts: IncidentTimeouts = IncidentTimeouts(),
        log_path: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._planner = planner
        self._robot = robot
        self._voice_layer = voice_layer
        self._body_layer = body_layer
        self._notifier = notifier
        self._timeouts = timeouts
        self._log_path = log_path
        self._clock = clock

    def handle(self, event: FallEvent) -> IncidentRecord:
        """任何環節失敗或逾時都不可中斷通報：拿不到語音回應就直接撥打。"""
        record = IncidentRecord(event=event)
        started = self._clock()
        deadline = started + self._timeouts.overall_s

        self._navigate(record)
        if record.temi_arrived:
            self._confirm_voice(record, deadline)
        if record.voice_responded:
            self._confirm_body(record, deadline)
        record.timeline["confirmation_done_s"] = round(self._clock() - started, 3)

        if record.voice_responded:
            record.outcome = Outcome.SUMMARY_SENT
            self._send_summary(record)
        else:
            record.outcome = Outcome.DIRECT_CALL
            self._call_contact(record)

        # 總應變時間從長者倒地起算，因此要加上判定跌倒前經過的時間
        record.timeline["total_s"] = round(self._clock() - started + event.onset_delay_s, 3)
        self._write_log(record)
        return record

    def _navigate(self, record: IncidentRecord) -> None:
        def go() -> bool:
            record.observation_point = self._planner.choose(record.event)
            return self._robot.go_to(record.observation_point, self._timeouts.navigation_s)

        record.temi_arrived = self._run_limited(record, "navigation", go, self._timeouts.navigation_s) is True

    def _confirm_voice(self, record: IncidentRecord, deadline: float) -> None:
        result = self._run_layer(record, self._voice_layer, deadline)
        if result is not None and result.responded:
            record.voice_responded = True
            record.voice_details = result.details

    def _confirm_body(self, record: IncidentRecord, deadline: float) -> None:
        result = self._run_layer(record, self._body_layer, deadline)
        if result is None:
            record.body_status = BodyStatus.UNAVAILABLE
        else:
            record.body_status = BodyStatus.COMPLETED if result.responded else BodyStatus.NOT_COMPLETED
            record.body_details = result.details

    def _run_layer(self, record: IncidentRecord, layer: ResponseLayer, deadline: float) -> Optional[LayerResult]:
        remaining = deadline - self._clock()
        if remaining <= 0:
            record.errors.append("overall_timeout")
            return None
        timeout = min(self._timeouts.per_layer_s, remaining)
        return self._run_limited(record, layer.name, lambda: layer.confirm(record, timeout), timeout)

    def _send_summary(self, record: IncidentRecord) -> None:
        summary = build_summary(record)
        limit = self._timeouts.notify_s
        self._run_limited(record, "send_summary", lambda: self._notifier.send_summary(record, summary), limit)
        acknowledged = self._run_limited(
            record,
            "wait_for_ack",
            lambda: self._notifier.wait_for_ack(record, self._timeouts.ack_s),
            self._timeouts.ack_s,
        )
        record.contact_acknowledged = acknowledged is True
        if not record.contact_acknowledged:
            self._call_contact(record)

    def _call_contact(self, record: IncidentRecord) -> None:
        summary = build_summary(record)

        def call() -> bool:
            self._notifier.call_contact(record, summary)
            return True

        record.contact_called = self._run_limited(record, "call_contact", call, self._timeouts.notify_s) is True

    def _run_limited(self, record: IncidentRecord, stage: str, action: Callable, timeout_s: float):
        """在限定時間內執行；出錯或卡住都只記錄並回傳 None，讓流程繼續往下走。"""
        outcome = {}

        def target() -> None:
            try:
                outcome["value"] = action()
            except Exception as error:
                logger.exception("階段 %s 失敗", stage)
                outcome["error"] = error

        worker = threading.Thread(target=target, daemon=True, name=f"incident-{stage}")
        worker.start()
        worker.join(timeout_s + self._timeouts.grace_s)
        if worker.is_alive():
            logger.error("階段 %s 超過 %.0f 秒仍未結束", stage, timeout_s)
            record.errors.append(f"{stage}: timeout")
            return None
        if "error" in outcome:
            record.errors.append(f"{stage}: {outcome['error']}")
            return None
        return outcome.get("value")

    def _write_log(self, record: IncidentRecord) -> None:
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(asdict(record), default=lambda v: v.value, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("無法寫入事件紀錄 %s", self._log_path)
