"""
Генератор тестовых изображений номеров для проверки OCR.
Рисует белый прямоугольник с номером — имитация фото.
"""
from pathlib import Path
import cv2
import numpy as np


def make_plate_image(text: str, out_path: Path):
    """Рисует синтетический номер."""
    w, h = 600, 200
    img = np.full((h, w, 3), 240, dtype=np.uint8)  # серый фон

    # Белый прямоугольник номера
    x1, y1, x2, y2 = 20, 40, w - 20, h - 40
    cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), -1)
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), 3)

    # Текст номера
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 2.0
    thickness = 4
    (tw, th), _ = cv2.getTextSize(text, font, font_scale, thickness)
    tx = x1 + (x2 - x1 - tw) // 2
    ty = y1 + (y2 - y1 + th) // 2
    cv2.putText(img, text, (tx, ty), font, font_scale, (0, 0, 0), thickness,
                cv2.LINE_AA)

    cv2.imwrite(str(out_path), img)
    print(f"Создан: {out_path}")


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "test_data"
    out_dir.mkdir(exist_ok=True)

    # Реальные российские номера
    make_plate_image("А123ВС77", out_dir / "plate1.jpg")
    make_plate_image("Е777КХ99", out_dir / "plate2.jpg")
    make_plate_image("М456ОР197", out_dir / "plate3.jpg")