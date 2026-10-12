"""
Модуль захвата видеопотока из VHS Core.

Класс CameraStream:
- подключается к /video_feed/<camera_id>?token=XXX;
- парсит multipart/x-mixed-replace (MJPEG);
- декодирует JPEG-кадры в numpy/OpenCV;
- хранит последний кадр для потребителей (engine, веб-превью);
- автоматически переподключается при обрыве;
- работает в отдельном потоке, не блокирует Flask.

Зависимости: requests, opencv-python, numpy.
"""

import time
import threading
import logging
from typing import Optional, Callable

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Парсер MJPEG-потока
# ---------------------------------------------------------------------- #
def _read_mjpeg_frames(response, chunk_size: int = 8192):
    """
    Генератор JPEG-кадров из multipart/x-mixed-replace ответа.

    Yields:
        bytes — байты одного JPEG-кадра (с маркерами SOI и EOI).
    """
    buf = b""
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        buf += chunk

        # JPEG начинается с 0xFFD8, заканчивается 0xFFD9
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                # Мусор до маркера — оставляем хвост, чтобы не потерять начало
                buf = buf[-2:] if len(buf) > 2 else buf
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                # Кадр ещё не пришёл целиком
                if start > 0:
                    buf = buf[start:]
                break
            jpg = buf[start:end + 2]
            buf = buf[end + 2:]
            yield jpg


# ---------------------------------------------------------------------- #
#  Поток камеры
# ---------------------------------------------------------------------- #
class CameraStream:
    """
    Фоновый поток, читающий MJPEG из VHS Core.

    Потребители обращаются через:
    - get_last_frame()  — последний декодированный кадр (numpy BGR) или None;
    - get_status()      — состояние потока (connected, fps, last_error);
    - set_on_frame(cb)  — колбэк, вызывается при каждом новом кадре.
    """

    def __init__(
        self,
        camera_id: str,
        camera_name: str,
        base_url: str,
        api_token: str,
        reconnect_min_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        request_timeout: float = 10.0,
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.reconnect_min_delay = reconnect_min_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.request_timeout = request_timeout

        self._stream_url = (
            f"{self.base_url}/video_feed/{self.camera_id}?token={self.api_token}"
        )

        # --- Состояние ---
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._last_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()
        self._frame_count: int = 0
        self._last_frame_time: float = 0.0
        self._actual_fps: float = 0.0

        self._connected: bool = False
        self._last_error: Optional[str] = None

        # Колбэк обработки кадра (ставится снаружи, например detector'ом)
        self._on_frame: Optional[Callable[[np.ndarray], None]] = None
        self._callback_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Управление
    # ------------------------------------------------------------------ #
    def set_on_frame(self, callback: Optional[Callable[[np.ndarray], None]]):
        """Устанавливает колбэк, вызываемый при каждом новом кадре."""
        with self._callback_lock:
            self._on_frame = callback

    def start(self):
        """Запускает поток."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"capture-{self.camera_id}", daemon=True
        )
        self._thread.start()
        logger.info("[%s] Поток захвата запущен: %s",
                    self.camera_id, self._stream_url)

    def stop(self):
        """Останавливает поток и ждёт его завершения."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[%s] Поток захвата остановлен", self.camera_id)

    # ------------------------------------------------------------------ #
    #  Основной цикл
    # ------------------------------------------------------------------ #
    def _run(self):
        reconnect_delay = self.reconnect_min_delay

        while not self._stop_event.is_set():
            try:
                logger.info("[%s] Подключение к %s",
                            self.camera_id, self._stream_url)
                response = requests.get(
                    self._stream_url,
                    stream=True,
                    timeout=self.request_timeout,
                    headers={"Accept": "multipart/x-mixed-replace"},
                )

                if response.status_code == 401:
                    raise PermissionError(
                        "401 Unauthorized — проверьте vhs_core.api_token"
                    )
                if response.status_code == 404:
                    raise FileNotFoundError(
                        f"404 — камера '{self.camera_id}' не найдена в VHS Core"
                    )
                response.raise_for_status()

                self._connected = True
                self._last_error = None
                reconnect_delay = self.reconnect_min_delay
                logger.info("[%s] Подключено", self.camera_id)

                # --- Читаем поток ---
                for jpg in _read_mjpeg_frames(response):
                    if self._stop_event.is_set():
                        break

                    frame = cv2.imdecode(
                        np.frombuffer(jpg, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if frame is None:
                        continue

                    # Обновляем состояние
                    now = time.time()
                    with self._frame_lock:
                        if self._last_frame_time > 0:
                            dt = now - self._last_frame_time
                            if dt > 0:
                                inst_fps = 1.0 / dt
                                # Экспоненциальное сглаживание
                                self._actual_fps = (
                                    0.9 * self._actual_fps + 0.1 * inst_fps
                                    if self._actual_fps > 0
                                    else inst_fps
                                )
                        self._last_frame = frame
                        self._frame_count += 1
                        self._last_frame_time = now

                    # Вызываем колбэк вне блокировки
                    with self._callback_lock:
                        cb = self._on_frame
                    if cb is not None:
                        try:
                            cb(frame)
                        except Exception as e:
                            logger.exception(
                                "[%s] Ошибка в колбэке обработки кадра: %s",
                                self.camera_id, e,
                            )

                response.close()

            except requests.exceptions.RequestException as e:
                self._connected = False
                self._last_error = str(e)
                logger.warning(
                    "[%s] Ошибка сети: %s. Повтор через %.1f с",
                    self.camera_id, e, reconnect_delay,
                )
            except PermissionError as e:
                self._connected = False
                self._last_error = str(e)
                logger.error(
                    "[%s] %s. Повтор через %.1f с",
                    self.camera_id, e, reconnect_delay,
                )
            except FileNotFoundError as e:
                self._connected = False
                self._last_error = str(e)
                logger.error("[%s] %s", self.camera_id, e)
                # Камеру нет в VHS — бессмысленно долбить часто, ставим макс.
                reconnect_delay = self.reconnect_max_delay
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                logger.exception(
                    "[%s] Неожиданная ошибка: %s", self.camera_id, e,
                )
            finally:
                if not self._stop_event.is_set():
                    if self._stop_event.wait(reconnect_delay):
                        break
                    reconnect_delay = min(
                        reconnect_delay * 2, self.reconnect_max_delay
                    )

        self._connected = False

    # ------------------------------------------------------------------ #
    #  Доступ к кадрам и состоянию
    # ------------------------------------------------------------------ #
    def get_last_frame(self) -> Optional[np.ndarray]:
        """Возвращает последний декодированный кадр (numpy BGR) или None."""
        with self._frame_lock:
            return self._last_frame

    def get_status(self) -> dict:
        with self._frame_lock:
            return {
                "id": self.camera_id,
                "name": self.camera_name,
                "connected": self._connected,
                "fps": round(self._actual_fps, 2),
                "frame_count": self._frame_count,
                "last_error": self._last_error,
                "stream_url": f"{self.base_url}/video_feed/{self.camera_id}",
            }

    @property
    def is_connected(self) -> bool:
        return self._connected