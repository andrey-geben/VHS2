# test_capture.py
import logging
import cv2
from capture import CameraStream

logging.basicConfig(level=logging.INFO)

stream = CameraStream(
    camera_id="analog_1",
    camera_name="Аналоговая камера 1",
    base_url="http://localhost:8000",
    api_token="change-this-analytics-token",
)

def on_frame(frame):
    # Проверим, что кадр приходит — покажем размер
    print(f"Кадр: {frame.shape}")

stream.set_on_frame(on_frame)
stream.start()

import time
time.sleep(15)

print("Статус:", stream.get_status())
stream.stop()