"""預覽畫面上的中文文字與區域框。OpenCV 無法顯示中文，所以用 Pillow 繪製。"""
from pathlib import Path
from typing import Optional, Sequence

CHINESE_FONT = Path(r"C:\Windows\Fonts\msjhbd.ttc")
ZONE_COLOR = (80, 200, 80)  # BGR
ACTIVE_ZONE_COLOR = (0, 0, 230)


def draw_texts(image, items: Sequence[tuple[str, tuple[int, int], tuple[int, int, int]]], size: int = 22) -> None:
    """一次畫多段文字，每段為（文字, 左上角座標, BGR 顏色）。"""
    import cv2
    import numpy as np

    if not items:
        return
    if not CHINESE_FONT.exists():
        for text, (x, y), color in items:
            ascii_text = text.encode("ascii", "replace").decode()
            cv2.putText(image, ascii_text, (x, y + size), cv2.FONT_HERSHEY_SIMPLEX, size / 32, color, 2)
        return

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(CHINESE_FONT), size)
    canvas = Image.fromarray(image[:, :, ::-1])
    draw = ImageDraw.Draw(canvas)
    for text, origin, color in items:
        draw.text(origin, text, font=font, fill=tuple(color[::-1]))
    image[:] = np.array(canvas)[:, :, ::-1]


def draw_zones(image, zones: Sequence, active: Optional[object] = None) -> None:
    """畫出跌倒區域；active 為目前人所在的區域，以紅色標示。"""
    import cv2

    height, width = image.shape[:2]
    labels = []
    for zone in zones:
        left, top, right, bottom = zone.rect
        start = (int(left * width), int(top * height))
        end = (int(right * width), int(bottom * height))
        color = ACTIVE_ZONE_COLOR if zone == active else ZONE_COLOR
        cv2.rectangle(image, start, end, color, 3 if zone == active else 2)
        labels.append((f"{zone.name} → {zone.location}", (start[0] + 6, start[1] + 4), color))
    draw_texts(image, labels)
