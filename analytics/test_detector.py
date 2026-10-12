"""
Тест детектора движения.
"""

import logging
import time
import cv2

from capture import CameraStream
from detector import MotionDetector, Zone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CAMERA_ID = "analog_1"
BASE_URL = "http://localhost:8000"
API_TOKEN = "change-this-analytics-token"

detector = MotionDetector(
    camera_id=CAMERA_ID,
    history=500,
    var_threshold=25,
    min_area=800,
    max_area=60000,         # новый фильтр
    min_solidity=0.35,      # новый фильтр
    detect_shadows=False,
    morph_kernel=5,
    zones=[
        Zone(name="Зона 1", x=320, y=0, w=320, h=480),
    ],
    warmup_frames=60,
)

stream = CameraStream(
    camera_id=CAMERA_ID,
    camera_name="Тест",
    base_url=BASE_URL,
    api_token=API_TOKEN,
)

stats = {"frames": 0, "detections": 0, "start": time.time()}
last_log = [time.time()]


def on_frame(frame):
    stats["frames"] += 1
    detections = detector.detect(frame)

    if detections:
        stats["detections"] += len(detections)
        now = time.time()
        if now - last_log[0] >= 0.5:      # логируем не чаще 2 раз в секунду
            last_log[0] = now
            for d in detections:
                print(f"  Detected: area={d['area']:>6}  "
                      f"solidity={d['solidity']:.2f}  "
                      f"zone={d['zone']}  center={d['center']}")

    # Раз в 30 кадров — сохраняем отладочный кадр
    if stats["frames"] % 30 == 0:
        annotated = detector.annotate_frame(frame, detections)
        cv2.imwrite("debug_frame.jpg", annotated)


stream.set_on_frame(on_frame)
stream.start()

try:
    time.sleep(30)
finally:
    stream.stop()

elapsed = time.time() - stats["start"]
print()
print("=" * 60)
print(f"Обработано кадров: {stats['frames']}")
print(f"Событий детекции:  {stats['detections']}")
print(f"Время:             {elapsed:.1f} сек")
print(f"Средний FPS:       {stats['frames'] / elapsed:.1f}")
print(f"Статус камеры:     {stream.get_status()}")
print("Отладочный кадр:   debug_frame.jpg")