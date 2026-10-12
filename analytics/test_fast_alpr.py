"""
Проверка детектора номеров из open_image_models (без OCR).
"""

import sys
import time
from pathlib import Path

import cv2

from open_image_models import LicensePlateDetector


def main():
    base = Path(__file__).parent
    test_dir = base / "test_data"
    if not test_dir.exists():
        print(f"Папка {test_dir} не найдена.")
        return 1

    images = sorted(
        list(test_dir.glob("*.jpg")) + list(test_dir.glob("*.png"))
    )
    if not images:
        print(f"В {test_dir} нет картинок.")
        return 1

    print("Инициализация детектора...")
    t0 = time.time()
    detector = LicensePlateDetector(
        detection_model="yolo-v9-t-384-license-plate-end2end"
    )
    print(f"OK за {time.time() - t0:.1f} сек")
    print("=" * 70)

    total = 0
    for img_path in images:
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"{img_path.name}: не читается")
            continue

        t0 = time.time()
        detections = detector.predict(frame)
        dt = (time.time() - t0) * 1000

        print(f"\n{img_path.name}: найдено {len(detections)}  ({dt:.0f} мс)")
        out = frame.copy()
        for d in detections:
            # open_image_models → LicensePlateDetection
            bbox = d.bounding_box       # (x1, y1, x2, y2)
            conf = d.confidence
            x1, y1, x2, y2 = map(int, bbox)
            print(f"    bbox=({x1},{y1},{x2},{y2})  conf={conf:.3f}")
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(out, f"{conf:.2f}", (x1, max(15, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1,
                        cv2.LINE_AA)

        out_path = base / f"fastalpr_{img_path.stem}.jpg"
        cv2.imwrite(str(out_path), out)
        print(f"    → {out_path.name}")
        total += len(detections)

    print()
    print("=" * 70)
    print(f"ИТОГО найдено: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())