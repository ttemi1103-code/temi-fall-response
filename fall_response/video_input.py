"""影像輸入：統一讀取電腦鏡頭、手機串流網址與影片檔。"""
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, Union

from .models import Frame

logger = logging.getLogger(__name__)

Source = Union[int, str]


def parse_source(value: str) -> Source:
    """命令列輸入 "0" 代表鏡頭編號，其餘視為網址或檔案路徑。"""
    return int(value) if value.isdigit() else value


def is_live_source(source: Source) -> bool:
    return isinstance(source, int) or "://" in source


def _open_with_opencv(source: Source):
    import cv2

    return cv2.VideoCapture(source)


class VideoSource:
    """即時來源只保留最新畫面並自動重連；影片檔則逐格讀取，方便重現實驗結果。"""

    def __init__(
        self,
        source: Source,
        capture_factory: Callable[[Source], object] = _open_with_opencv,
        reconnect_delay_s: float = 2.0,
        stale_warning_s: float = 5.0,
    ):
        self.source = source
        self.is_live = is_live_source(source)
        self._capture_factory = capture_factory
        self._reconnect_delay_s = reconnect_delay_s
        self._stale_warning_s = stale_warning_s
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._latest: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None

    def frames(self) -> Iterator[Frame]:
        return self._live_frames() if self.is_live else self._file_frames()

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _file_frames(self) -> Iterator[Frame]:
        if not Path(str(self.source)).exists():
            raise FileNotFoundError(f"找不到影片檔：{self.source}")
        capture = self._capture_factory(self.source)
        if not capture.isOpened():
            raise RuntimeError(f"無法開啟影片檔：{self.source}")
        fps = capture.get(5) or 30.0  # 5 = cv2.CAP_PROP_FPS
        index = 0
        try:
            while not self._stop.is_set():
                ok, image = capture.read()
                if not ok:
                    return
                yield Frame(image, index / fps, index)
                index += 1
        finally:
            capture.release()

    def _live_frames(self) -> Iterator[Frame]:
        self._thread = threading.Thread(target=self._read_live, daemon=True)
        self._thread.start()
        index = 0
        last_frame_at = time.monotonic()
        warned = False
        while not self._stop.is_set():
            with self._condition:
                if self._latest is None:
                    self._condition.wait(timeout=0.5)
                latest, self._latest = self._latest, None
            if latest is None:
                # 鏡頭中斷時系統等於沒有監測，必須讓操作者知道
                if not warned and time.monotonic() - last_frame_at > self._stale_warning_s:
                    logger.error("已超過 %.0f 秒沒有收到影像，目前無法偵測跌倒", self._stale_warning_s)
                    warned = True
                continue
            image, timestamp = latest
            last_frame_at, warned = time.monotonic(), False
            yield Frame(image, timestamp, index)
            index += 1

    def _read_live(self) -> None:
        capture = None
        while not self._stop.is_set():
            if capture is None:
                capture = self._capture_factory(self.source)
                if not capture.isOpened():
                    logger.warning("無法連線影像來源 %s，%.0f 秒後重試", self._safe_name(), self._reconnect_delay_s)
                    capture.release()
                    capture = None
                    self._stop.wait(self._reconnect_delay_s)
                    continue
                logger.info("已連線影像來源 %s", self._safe_name())
            ok, image = capture.read()
            if not ok:
                logger.warning("影像來源中斷，準備重新連線")
                capture.release()
                capture = None
                self._stop.wait(self._reconnect_delay_s)
                continue
            with self._condition:
                self._latest = (image, time.monotonic())
                self._condition.notify_all()
        if capture is not None:
            capture.release()

    def _safe_name(self) -> str:
        # 串流網址可能含帳密，紀錄時只保留協定與主機
        text = str(self.source)
        if "://" in text and "@" in text:
            scheme, rest = text.split("://", 1)
            return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
        return text
