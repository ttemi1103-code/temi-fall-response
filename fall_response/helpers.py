"""產生合成關節點，讓測試不需要鏡頭或模型。"""
import math
from typing import Optional

from fall_response import models
from fall_response.models import Landmark, Pose

FPS = 15
TORSO = 100.0


def make_pose(
    hip_y: float = 400.0,
    angle_deg: float = 0.0,
    hip_x: float = 300.0,
    torso: float = TORSO,
    nose_offset: tuple[float, float] = (0.0, 0.0),
    left_wrist: Optional[tuple[float, float]] = None,
) -> Pose:
    """angle_deg 為軀幹與鉛直的夾角。left_wrist、nose_offset 以身體座標表示（軀幹長度為單位）。"""
    ux, uy = math.sin(math.radians(angle_deg)), math.cos(math.radians(angle_deg))
    vx, vy = -uy, ux  # 與軀幹垂直的方向
    shoulder_x, shoulder_y = hip_x - ux * torso, hip_y - uy * torso

    def body(u: float, v: float) -> Landmark:
        return Landmark(shoulder_x + (ux * u + vx * v) * torso, shoulder_y + (uy * u + vy * v) * torso)

    points = [body(0.3, 0.0)] * 33
    points = list(points)
    # 肩髖寬度沿 v 軸；身體由髖往腳延伸，讓直立與平躺的外框比例接近真實人體
    points[models.LEFT_SHOULDER] = body(0.0, 0.2)
    points[models.RIGHT_SHOULDER] = body(0.0, -0.2)
    points[models.LEFT_HIP] = body(1.0, 0.15)
    points[models.RIGHT_HIP] = body(1.0, -0.15)
    points[models.NOSE] = body(-0.4 + nose_offset[0], nose_offset[1])
    wrist = left_wrist if left_wrist is not None else (0.9, 0.3)
    points[models.LEFT_WRIST] = body(*wrist)
    points[models.RIGHT_WRIST] = body(0.9, -0.3)
    for knee in (25, 26):
        points[knee] = body(1.5, 0.15 if knee == 25 else -0.15)
    for ankle in (27, 28):
        points[ankle] = body(2.0, 0.15 if ankle == 27 else -0.15)
    return Pose(tuple(points))


def timeline(seconds: float, start: float = 0.0):
    return [start + i / FPS for i in range(int(seconds * FPS))]
