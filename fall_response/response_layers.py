"""兩層回應確認：語音層轉接另一組的模組，肢體層用 Temi 鏡頭畫面辨識動作。"""
import logging
import threading
import time
from typing import Callable, Protocol

from .gesture_recognizer import Gesture, GestureRecognizer
from .incident import IncidentRecord, LayerResult

logger = logging.getLogger(__name__)

MANDARIN = "zh-TW"
TAIWANESE = "nan-TW"
LANGUAGES = (MANDARIN, TAIWANESE)

# 台語文字請由會說台語的組員確認，並依實際使用的語音服務調整寫法
BODY_INSTRUCTIONS = {
    MANDARIN: "如果你聽得到我，請舉手或點頭。",
    TAIWANESE: "若有聽著我講話，請共手攑起來，抑是頕頭。",
}
ACCEPTED_GESTURES = frozenset({Gesture.RAISE_HAND, Gesture.WAVE, Gesture.NOD})


class VoiceClient(Protocol):
    """語音組要提供的介面，回傳格式請見 README。"""

    def confirm_voice(self, incident_time: str, location: str, language: str, timeout_s: float) -> dict: ...


class Speaker(Protocol):
    def speak(self, text: str, language: str) -> None: ...


class VoiceLayer:
    name = "語音"

    def __init__(self, client: VoiceClient, language: str = MANDARIN):
        self._client = client
        self._language = language

    def confirm(self, record: IncidentRecord, timeout_s: float) -> LayerResult:
        result = self._client.confirm_voice(
            incident_time=record.event.wall_time,
            location=record.observation_point or "",
            language=self._language,
            timeout_s=timeout_s,
        )
        return LayerResult(result.get("responded") is True, result)


class BodyLayer:
    name = "肢體"

    def __init__(
        self,
        speaker: Speaker,
        estimator,
        open_source: Callable[[], object],
        language: str = MANDARIN,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._speaker = speaker
        self._estimator = estimator
        self._open_source = open_source
        self._language = language
        self._clock = clock

    def confirm(self, record: IncidentRecord, timeout_s: float) -> LayerResult:
        self._speaker.speak(BODY_INSTRUCTIONS[self._language], self._language)
        recognizer = GestureRecognizer()  # 只計算指令之後的動作
        deadline = self._clock() + timeout_s
        source = self._open_source()
        # 收不到畫面時迴圈不會繼續，所以時間到要主動關閉影像來源
        watchdog = threading.Timer(timeout_s, source.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            for frame in source.frames():
                pose = self._estimator.estimate(frame)
                accepted = recognizer.update(pose, frame.timestamp) & ACCEPTED_GESTURES
                if accepted:
                    return LayerResult(True, {"gestures": sorted(g.value for g in accepted)})
                if self._clock() >= deadline:
                    break
        finally:
            watchdog.cancel()
            source.close()
        return LayerResult(False, {"gestures": []})


class SimulatedVoiceClient:
    def __init__(self, responded: bool = False):
        self._responded = responded

    def confirm_voice(self, incident_time: str, location: str, language: str, timeout_s: float) -> dict:
        logger.info("[模擬] 語音確認（%s）：%s", language, "有回應" if self._responded else "無回應")
        if not self._responded:
            return {"responded": False}
        return {
            "responded": True,
            "transcript": "我右膝很痛，站不起來",
            "pain_location": "右膝",
            "can_stand_up": False,
            "needs_help": True,
            "language": language,
        }


class SimulatedSpeaker:
    def speak(self, text: str, language: str) -> None:
        logger.info("[模擬] Temi 說（%s）：%s", language, text)
