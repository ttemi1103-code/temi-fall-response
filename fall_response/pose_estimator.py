"""肢體辨識：用 MediaPipe Pose Landmarker 取得 33 個人體關節點。"""
from pathlib import Path
from typing import Optional

from .models import Frame, Landmark, Pose


class PoseEstimator:
    def __init__(
        self,
        model_path: str,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ):
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到姿勢模型：{path}（下載方式請見 README）")

        self._cv2 = cv2
        self._mp = mp
        # 以位元組載入模型，避免 Windows 上路徑含中文時無法開檔
        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=path.read_bytes()),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)
        self._last_ms = -1

    def estimate(self, frame: Frame) -> Optional[Pose]:
        height, width = frame.image.shape[:2]
        rgb = self._cv2.cvtColor(frame.image, self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)

        # VIDEO 模式要求時間戳嚴格遞增
        timestamp_ms = max(int(frame.timestamp * 1000), self._last_ms + 1)
        self._last_ms = timestamp_ms
        result = self._landmarker.detect_for_video(image, timestamp_ms)
        if not result.pose_landmarks:
            return None

        landmarks = tuple(
            Landmark(point.x * width, point.y * height, point.visibility or 0.0)
            for point in result.pose_landmarks[0]
        )
        return Pose(landmarks)

    def close(self) -> None:
        self._landmarker.close()
