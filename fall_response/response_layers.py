"""語音確認層：轉接語音組的模組。肢體確認由其他組員負責，透過 incident.ResponseLayer 介面接入。"""
import logging
from typing import Protocol

from .incident import IncidentRecord, LayerResult

logger = logging.getLogger(__name__)

MANDARIN = "zh-TW"
TAIWANESE = "nan-TW"
LANGUAGES = (MANDARIN, TAIWANESE)


class VoiceClient(Protocol):
    """語音組要提供的介面，回傳格式請見 README。"""

    def confirm_voice(self, incident_time: str, location: str, language: str, timeout_s: float) -> dict: ...


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
