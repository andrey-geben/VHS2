"""
Модуль камеры.

Класс Camera инкапсулирует:
- захват видеопотока (аналог/DirectShow на Windows, V4L2 на Linux;
  IP-камеры — RTSP/FFmpeg);
- буфер последнего кадра для MJPEG-трансляции в браузер;
- запись в файлы с разбиением по времени;
- автоматическое переподключение при обрыве связи.

Кроссплатформенность:
- Windows: cv2.CAP_DSHOW для аналоговых устройств;
- Linux:   cv2.CAP_V4L2 для аналоговых устройств (/dev/videoN);
- RTSP:    cv2.CAP_FFMPEG на обеих платформах.
"""

import platform
import cv2
import time
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Определяем платформу один раз при импорте модуля
IS_WINDOWS = platform.system() == "Windows"


class Camera:
    """
    Одна камера = один поток захвата.

    Поток непрерывно читает кадры, кладёт их в self._last_frame
    (для веб-трансляции) и, если идёт запись, пишет в VideoWriter.
    """

    def __init__(self, config: dict, db, recordings_dir: Path):
        self.id: str = config["id"]
        self.name: str = config["name"]
        self.source = config["source"]
        self.type: str = config.get("type", "ip")
        self.fps: int = int(config.get("fps", 25))
        self.width: int = int(config.get("width", 640))
        self.height: int = int(config.get("height", 480))
        self.auto_record: bool = bool(config.get("auto_record", False))

        self.db = db
        self.recordings_dir = Path(recordings_dir) / self.id
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        # --- Состояние ---
        self._cap: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._last_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()

        self._recording: bool = False
        self._recording_lock = threading.Lock()

        self._connected: bool = False
        self._last_error: Optional[str] = None
        self._actual_fps: float = 0.0

        # --- Запись ---
        self._writer: Optional[cv2.VideoWriter] = None
        self._current_recording_id: Optional[int] = None
        self._segment_start: Optional[datetime] = None
        self._segment_duration: int = int(config.get("segment_duration", 3600))

        # Для расчёта FPS
        self._last_frame_time: float = 0.0

    # ------------------------------------------------------------------ #
    #  Открытие/закрытие источника
    # ------------------------------------------------------------------ #
    def _open_capture(self) -> cv2.VideoCapture:
        """Открывает источник с нужным бэкендом (зависит от ОС)."""
        if self.type == "analog":
            # Windows → DirectShow; Linux → V4L2
            backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_V4L2
            try:
                source = int(self.source)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Для аналоговой камеры источник должен быть числом "
                    f"(индекс устройства), получено: {self.source!r}"
                )
            cap = cv2.VideoCapture(source, backend)
        else:
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)

        # Параметры — по возможности
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # Уменьшаем внутренний буфер, чтобы получать свежие кадры
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    # ------------------------------------------------------------------ #
    #  Управление жизненным циклом
    # ------------------------------------------------------------------ #
    def start(self):
        """Запускает поток захвата."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-{self.id}", daemon=True
        )
        self._thread.start()
        logger.info(
            "[%s] Поток камеры запущен (backend=%s)",
            self.id, "DSHOW" if IS_WINDOWS else "V4L2/FFMPEG",
        )

    def stop(self):
        """Останавливает поток и освобождает ресурсы."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._release_capture()
        self._stop_recording_internal(finalize=True)
        logger.info("[%s] Поток камеры остановлен", self.id)

    def _release_capture(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._connected = False

    # ------------------------------------------------------------------ #
    #  Основной цикл
    # ------------------------------------------------------------------ #
    def _run(self):
        """Цикл: читаем кадры, обновляем буфер, пишем при необходимости."""
        reconnect_delay = 1
        while not self._stop_event.is_set():
            # --- Подключение ---
            if self._cap is None or not self._cap.isOpened():
                try:
                    self._cap = self._open_capture()
                    if not self._cap.isOpened():
                        raise IOError("не удалось открыть поток")
                    self._connected = True
                    self._last_error = None
                    reconnect_delay = 1
                    logger.info("[%s] Подключено к источнику", self.id)

                    if self.auto_record and not self._recording:
                        self.start_recording()
                except Exception as e:
                    self._connected = False
                    self._last_error = str(e)
                    logger.warning(
                        "[%s] Ошибка подключения: %s. Повтор через %s с",
                        self.id, e, reconnect_delay,
                    )
                    self._release_capture()
                    if self._stop_event.wait(reconnect_delay):
                        break
                    reconnect_delay = min(reconnect_delay * 2, 30)
                    continue

            # --- Чтение кадра ---
            ret, frame = self._cap.read()
            if not ret or frame is None:
                logger.warning("[%s] Потеря кадра, переподключение", self.id)
                self._connected = False
                self._last_error = "потеря сигнала"
                self._release_capture()
                if self._stop_event.wait(1):
                    break
                continue

            # --- Буфер для трансляции ---
            try:
                ok, jpg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
                )
                if ok:
                    with self._frame_lock:
                        self._last_frame = jpg.tobytes()
            except Exception as e:
                logger.debug("[%s] Ошибка кодирования кадра: %s", self.id, e)

            # --- FPS ---
            now = time.time()
            if self._last_frame_time > 0:
                dt = now - self._last_frame_time
                if dt > 0:
                    inst_fps = 1.0 / dt
                    self._actual_fps = (
                        0.9 * self._actual_fps + 0.1 * inst_fps
                        if self._actual_fps > 0 else inst_fps
                    )
            self._last_frame_time = now

            # --- Запись ---
            with self._recording_lock:
                if self._recording and self._writer is not None:
                    self._write_frame(frame)

            # --- Контроль FPS ---
            time.sleep(max(0.0, 1.0 / self.fps))

        self._release_capture()

    # ------------------------------------------------------------------ #
    #  Трансляция (MJPEG)
    # ------------------------------------------------------------------ #
    def get_last_frame(self) -> Optional[bytes]:
        """Возвращает JPEG-байты последнего кадра или None."""
        with self._frame_lock:
            return self._last_frame

    def mjpeg_generator(self):
        """Генератор для Response в Flask — multipart/x-mixed-replace."""
        boundary = b"--frame\r\n"
        last_sent = None
        while not self._stop_event.is_set():
            frame = self.get_last_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            if frame is last_sent:
                time.sleep(1.0 / self.fps)
                continue
            last_sent = frame
            yield (
                boundary
                + b"Content-Type: image/jpeg\r\n"
                + b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )
            time.sleep(1.0 / self.fps)

    # ------------------------------------------------------------------ #
    #  Запись
    # ------------------------------------------------------------------ #
    def start_recording(self) -> bool:
        """Включает запись. Возвращает True при успехе."""
        with self._recording_lock:
            if self._recording:
                return False
            try:
                self._open_new_segment()
                self._recording = True
                logger.info("[%s] Запись запущена", self.id)
                return True
            except Exception as e:
                logger.error("[%s] Не удалось начать запись: %s", self.id, e)
                return False

    def stop_recording(self) -> bool:
        """Останавливает запись."""
        with self._recording_lock:
            if not self._recording:
                return False
            self._recording = False
        self._stop_recording_internal(finalize=True)
        logger.info("[%s] Запись остановлена", self.id)
        return True

    def _stop_recording_internal(self, finalize: bool = True):
        """Закрывает VideoWriter и обновляет запись в БД."""
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception:
                pass
            self._writer = None

        if finalize and self._current_recording_id is not None:
            self._finalize_recording()

        self._segment_start = None
        self._current_recording_id = None

    def _open_new_segment(self):
        """Создаёт новый файл и запись в БД."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}.avi"
        filepath = self.recordings_dir / filename

        # XVID доступен в стандартной сборке OpenCV на обеих ОС
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        writer = cv2.VideoWriter(
            str(filepath), fourcc, self.fps, (self.width, self.height)
        )
        if not writer.isOpened():
            raise IOError("VideoWriter не открылся (проверьте кодек XVID)")

        self._writer = writer
        self._segment_start = datetime.now()
        self._current_recording_id = self.db.add_recording(
            camera_id=self.id,
            filepath=str(filepath),
            start_time=self._segment_start,
        )

    def _write_frame(self, frame):
        """Пишет кадр и при необходимости ротирует файл."""
        # Ротация по времени
        if (
            self._segment_start is not None
            and (datetime.now() - self._segment_start).total_seconds()
            >= self._segment_duration
        ):
            self._stop_recording_internal(finalize=True)
            try:
                self._open_new_segment()
            except Exception as e:
                logger.error("[%s] Ошибка ротации сегмента: %s", self.id, e)
                self._recording = False
                return

        # Приводим кадр к нужному размеру
        h, w = frame.shape[:2]
        if (w, h) != (self.width, self.height):
            frame = cv2.resize(frame, (self.width, self.height))

        try:
            self._writer.write(frame)
        except Exception as e:
            logger.error("[%s] Ошибка записи кадра: %s", self.id, e)

    def _finalize_recording(self):
        """Обновляет метаданные записи в БД после завершения файла."""
        rec_id = self._current_recording_id
        if rec_id is None:
            return
        rec = self.db.get_recording(rec_id)
        if not rec:
            return
        try:
            filepath = Path(rec["filepath"])
            filesize = filepath.stat().st_size if filepath.exists() else 0
            end_time = datetime.now()
            start_time = datetime.fromisoformat(rec["start_time"])
            duration = int((end_time - start_time).total_seconds())
            self.db.update_recording_end(rec_id, end_time, duration, filesize)
        except Exception as e:
            logger.error("[%s] Ошибка финализации записи: %s", self.id, e)

    # ------------------------------------------------------------------ #
    #  Состояние (для API и UI)
    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "source": str(self.source),
            "connected": self._connected,
            "recording": self._recording,
            "last_error": self._last_error,
            "fps": round(self._actual_fps, 1) if self._actual_fps else self.fps,
            "resolution": f"{self.width}x{self.height}",
        }

    @property
    def is_recording(self) -> bool:
        return self._recording