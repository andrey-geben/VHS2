"""
Тест полного ALPR-пайплайна: детектор + OCR.
"""

import sys
import time
from pathlib import Path

import cv2

from alpr import AlprPipeline


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

    print("Инициализация ALPR...")
    t0 = time.time()
    pipeline = AlprPipeline(
        detector_model="yolo-v9-t-384-license-plate-end2end",
        detection_confidence=0.4,
        ocr_confidence=0.5,
    )
    pipeline._ensure_loaded()
    print(f"OK за {time.time() - t0:.1f} сек")
    print("=" * 70)

    total = 0
    for img_path in images:
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"{img_path.name}: не читается")
            continue

        t0 = time.time()
        plates = pipeline.detect(frame)
        dt = (time.time() - t0) * 1000

        print(f"\n{img_path.name}: распознано {len(plates)}  ({dt:.0f} мс)")
        for p in plates:
            print(f"    🎯 {p['text']}   "
                  f"(raw='{p['raw_text']}'  conf={p['confidence']:.3f}  "
                  f"bbox={p['bbox']})")

        out = pipeline.annotate_frame(frame, plates)
        out_path = base / f"alpr_{img_path.stem}.jpg"
        cv2.imwrite(str(out_path), out)
        total += len(plates)

    print()
    print("=" * 70)
    print(f"ИТОГО распознано номеров: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())