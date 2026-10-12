"""
Проверка YOLO-детектора на реальном потоке.
Считает количество объектов в КАЖДОМ кадре.
"""

import logging
import time
from pathlib import Path
from collections import Counter

import cv2

from capture import CameraStream
from detector_yolo import YoloDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).parent

CAMERA_ID = "analog_1"
BASE_URL = "http://localhost:8000"
API_TOKEN = "change-this-analytics-token"

detector = YoloDetector(
    camera_id=CAMERA_ID,
    model_path=str(BASE_DIR / "models" / "yolov8n.onnx"),
    confidence_threshold=0.4,
    nms_threshold=0.45,
    input_size=(640, 640),
    allowed_classes=None,
    min_area=0,
    use_gpu=False,
    warmup=True,
)

stream = CameraStream(
    camera_id=CAMERA_ID,
    camera_name="Тест YOLO",
    base_url=BASE_URL,
    api_token=API_TOKEN,
)

# Статистика
stats = {
    "frames": 0,
    "start": time.time(),
    "infer_time": 0.0,
    # Гистограмма: сколько раз в кадре было N объектов
    "hist": Counter(),
    # Классы, которые видели, и счётчик появлений
    "classes": Counter(),
    # Пример одной детекции для лога
    "sample": None,
}
log_interval = [time.time()]


def on_frame(frame):
    stats["frames"] += 1

    t0 = time.time()
    detections = detector.detect(frame)
    stats["infer_time"] += time.time() - t0

    # Сколько объектов в ЭТОМ кадре
    n = len(detections)
    stats["hist"][n] += 1

    for d in detections:
        stats["classes"][d["class_name"]] += 1

    # Запоминаем пример — первая детекция из кадра
    if detections and stats["sample"] is None:
        stats["sample"] = detections[0]

    # Раз в секунду — короткий отчёт
    now = time.time()
    if now - log_interval[0] >= 1.0:
        log_interval[0] = now
        last_n = n
        cls = detections[0]["class_name"] if detections else "—"
        conf = detections[0]["confidence"] if detections else 0
        print(f"  Кадр: объектов={last_n}  класс={cls}  conf={conf:.2f}")

    # Каждые 30 кадров — сохраняем отладочный кадр
    if stats["frames"] % 30 == 0:
        annotated = detector.annotate_frame(frame, detections)
        cv2.imwrite("debug_yolo.jpg", annotated)


stream.set_on_frame(on_frame)
stream.start()

try:
    time.sleep(20)
finally:
    stream.stop()

elapsed = time.time() - stats["start"]

print()
print("=" * 60)
print(f"Обработано кадров:   {stats['frames']}")
print(f"Средний FPS:         {stats['frames'] / elapsed:.1f}")
if stats["frames"] > 0:
    avg_infer = stats["infer_time"] / stats["frames"] * 1000
    print(f"Среднее время инференса: {avg_infer:.1f} мс")
print()
print("Гистограмма «объектов в кадре»:")
for n in sorted(stats["hist"].keys()):
    cnt = stats["hist"][n]
    pct = cnt / stats["frames"] * 100
    print(f"  {n} объектов: {cnt:>5} кадров ({pct:5.1f}%)")
print()
print("Классы, которые видел детектор:")
for cls, cnt in stats["classes"].most_common():
    print(f"  {cls:<15} {cnt}")
print()
if stats["sample"]:
    s = stats["sample"]
    print("Пример детекции:")
    print(f"  Класс:      {s['class_name']} (id={s['class_id']})")
    print(f"  Confidence: {s['confidence']:.3f}")
    print(f"  BBox:       {s['bbox']}")
    print(f"  Площадь:    {s['area']}")
    print(f"  Центр:      {s['center']}")
print("=" * 60)
print("Отладочный кадр: debug_yolo.jpg")