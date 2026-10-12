"""
Движок аналитики.

Типы аналитики на камеру задаются независимыми флагами
(приходят из VHS Core):
  detect_objects — YOLOv8 (person, car, truck, bus, ...)
  detect_plates  — ALPR (номера)
  detect_faces   — InsightFace (задел на будущее)
  detect_motion  — MOG2 (детекция движения)

Если флаг `enabled=False` — аналитика на камере отключена.
Если включены несколько флагов — работают все детекторы параллельно.

Совместимость со старым `analytics_mode` (off/person/plate/both)
обеспечена через конвертацию в `analytics_app.py` ещё на этапе
получения конфига от Core.
"""

import json
import time
import logging
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any

import cv2

from capture import CameraStream
from detector import MotionDetector, Zone
from detector_yolo import YoloDetector
from alpr import AlprDetector
from vhs_client import VhsClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Утилиты для правил
# ---------------------------------------------------------------------- #
def _parse_time(s: str) -> Optional[dtime]:
    try:
        hh, mm = s.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _in_time_range(now: dtime, from_t: dtime, to_t: dtime) -> bool:
    if from_t <= to_t:
        return from_t <= now <= to_t
    return now >= from_t or now <= to_t


def _normalize_plate(text: str) -> str:
    import re
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


# ---------------------------------------------------------------------- #
#  Обёртка над камерой
# ---------------------------------------------------------------------- #
class CameraWorker:
    """Связка capture + несколько детекторов + actions."""

    def __init__(
        self,
        camera_id: str,
        camera_name: str,
        stream: CameraStream,
        detectors: List,
        detector_modes: List[str],
        analytics_flags: dict,
        db,
        snapshots_dir: Path,
        events_cfg: dict,
        actions_cfg: dict,
        vhs_client: Optional[VhsClient] = None,
        public_url: str = "",
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.stream = stream
        self.detectors = detectors or []
        self.detector_modes = detector_modes or []
        self.analytics_flags = analytics_flags or {}
        self.db = db
        self.snapshots_dir = snapshots_dir
        self.on_event = on_event
        self.vhs_client = vhs_client
        self.public_url = public_url.rstrip("/") if public_url else ""

        # События
        self.cooldown_sec = float(events_cfg.get("cooldown_sec", 10))
        self.save_snapshots = bool(events_cfg.get("save_snapshots", True))
        self.snapshot_quality = int(events_cfg.get("snapshot_quality", 80))
        self.snapshot_width = int(events_cfg.get("snapshot_width", 640) or 0)
        self.snapshot_height = int(events_cfg.get("snapshot_height", 0) or 0)

        # Действия
        self.actions_enabled = bool(actions_cfg.get("enabled", False))
        self.actions_rules: List[dict] = actions_cfg.get("rules", []) or []
        self.default_record_duration = int(
            actions_cfg.get("default_record_duration_sec", 60)
        )
        self.max_record_duration = int(
            actions_cfg.get("max_record_duration_sec", 600)
        )
        self.access_check_enabled = bool(
            actions_cfg.get("access_check_enabled", True)
        )

        # Состояние
        self._last_event_time: float = 0.0
        self._event_count: int = 0
        self._frame_count: int = 0
        self._action_count: int = 0
        self._access_count: int = 0
        self._lock = threading.Lock()

        self._record_timer: Optional[threading.Timer] = None
        self._record_until: float = 0.0

        self._camera_snap_dir = self.snapshots_dir / camera_id
        self._camera_snap_dir.mkdir(parents=True, exist_ok=True)

        self.stream.set_on_frame(self._process_frame)

    # ------------------------------------------------------------------ #
    #  Обработка кадра
    # ------------------------------------------------------------------ #
    def _process_frame(self, frame):
        self._frame_count += 1

        if not self.detectors:
            return

        all_detections: List[Dict[str, Any]] = []
        used_detector = None

        for i, det in enumerate(self.detectors):
            try:
                dets = det.detect(frame)
            except Exception as e:
                logger.exception(
                    "[%s] Ошибка в детекторе %d (%s): %s",
                    self.camera_id, i, self.detector_modes[i], e,
                )
                continue

            if dets:
                used_detector = det
                all_detections.extend(dets)

        if not all_detections:
            return

        # Фильтр по зонам
        filtered: List[Dict[str, Any]] = []
        for det in all_detections:
            if det.get("class_name") == "plate":
                filtered.append(det)
                continue

            zones = getattr(self.detectors[0], "zones", None) or []
            if zones:
                for zone in zones:
                    if zone.contains_bbox(det["bbox"], min_overlap=0.3):
                        det["zone"] = zone.name
                        filtered.append(det)
                        break
            else:
                filtered.append(det)

        if not filtered:
            return

        has_alpr_event = any(
            d.get("class_name") == "plate" for d in filtered
        )
        if not has_alpr_event:
            now = time.time()
            with self._lock:
                if now - self._last_event_time < self.cooldown_sec:
                    return
                self._last_event_time = now

        main = max(filtered, key=lambda d: d.get("confidence", 0))
        snapshot_path = self._save_snapshot(frame, filtered, used_detector)

        event_type = main.get("class_name") or "motion"
        plate_text = main.get("plate_text")
        extra_data = self._build_extra(main)

        event = {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "zone_name": main.get("zone"),
            "event_type": event_type,
            "confidence": float(main.get("confidence", 1.0)),
            "bbox": main["bbox"],
            "area": main["area"],
            "snapshot_path": snapshot_path,
            "extra": extra_data,
            "plate_text": plate_text,
            "created_at": datetime.now(),
        }

        try:
            event_id = self.db.add_event(
                camera_id=event["camera_id"],
                camera_name=event["camera_name"],
                zone_name=event["zone_name"],
                event_type=event["event_type"],
                confidence=event["confidence"],
                bbox=event["bbox"],
                area=event["area"],
                snapshot_path=snapshot_path,
                extra=extra_data,
                created_at=event["created_at"],
            )
            event["id"] = event_id
        except Exception as e:
            logger.exception("[%s] Ошибка записи события: %s",
                             self.camera_id, e)
            return

        with self._lock:
            self._event_count += 1

        if event_type == "plate" and plate_text:
            logger.info(
                "[%s] Событие #%s: PLATE %s conf=%.2f zone=%s",
                self.camera_id, event_id, plate_text,
                event["confidence"], main.get("zone"),
            )
        else:
            logger.info(
                "[%s] Событие #%s: %s conf=%.2f zone=%s",
                self.camera_id, event_id, event_type,
                event["confidence"], main.get("zone"),
            )

        # АСКУД: проверка номера в Core
        access_result = None
        if (
            plate_text
            and self.access_check_enabled
            and self.vhs_client
            and self.vhs_client.enabled
        ):
            try:
                access_result = self._check_access(
                    plate_text, event_id, snapshot_path,
                )
            except Exception as e:
                logger.exception("[%s] Ошибка проверки АСКУД: %s",
                                 self.camera_id, e)

        # Обратный канал
        if self.actions_enabled and self.vhs_client:
            try:
                self._run_actions(event, main, access_result)
            except Exception as e:
                logger.exception("[%s] Ошибка выполнения действий: %s",
                                 self.camera_id, e)

        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.exception("[%s] Ошибка on_event: %s",
                                 self.camera_id, e)

    # ------------------------------------------------------------------ #
    #  АСКУД
    # ------------------------------------------------------------------ #
    def _check_access(
        self, plate, event_id, snapshot_path,
    ) -> Optional[Dict[str, Any]]:
        snap_url = None
        if self.public_url and event_id:
            snap_url = f"{self.public_url}/snapshot/{event_id}"

        result = self.vhs_client.check_access_plate(
            plate=plate,
            camera_id=self.camera_id,
            event_id=event_id,
            snapshot_url=snap_url,
        )

        if not result:
            logger.warning(
                "[%s] АСКУД: Core не ответил на проверку номера %s",
                self.camera_id, plate,
            )
            return None

        action = result.get("action", "unknown")
        message = result.get("message", "")
        device = result.get("device_name") or ""

        with self._lock:
            self._access_count += 1

        if action == "open":
            logger.info(
                "[%s] АСКУД: %s → ✅ ОТКРЫТО (%s)",
                self.camera_id, plate, device or message,
            )
        elif action == "alarm":
            logger.warning(
                "[%s] АСКУД: %s → 🚨 ТРЕВОГА (%s)",
                self.camera_id, plate, message,
            )
        elif action == "deny":
            logger.info(
                "[%s] АСКУД: %s → ⛔ Отказ (%s)",
                self.camera_id, plate, message,
            )
        elif action == "unknown":
            logger.info(
                "[%s] АСКУД: %s → ❓ Не в базе",
                self.camera_id, plate,
            )
        else:
            logger.warning(
                "[%s] АСКУД: %s → ошибка (%s)",
                self.camera_id, plate, message,
            )

        return result

    # ------------------------------------------------------------------ #
    #  Extra
    # ------------------------------------------------------------------ #
    def _build_extra(self, main: dict) -> Optional[str]:
        data = {}

        if main.get("class_name") == "plate":
            plate = main.get("plate_text")
            if plate:
                data["plate"] = plate
            if main.get("_raw_texts"):
                data["raw_texts"] = main["_raw_texts"][:5]
            if main.get("_votes"):
                data["votes"] = main["_votes"]
        else:
            if main.get("class_name"):
                data["class"] = main["class_name"]
            if main.get("class_id") is not None:
                data["class_id"] = main["class_id"]

        return json.dumps(data, ensure_ascii=False) if data else None

    # ------------------------------------------------------------------ #
    #  Правила
    # ------------------------------------------------------------------ #
    def _run_actions(self, event, main, access_result=None):
        now_dt = event["created_at"]
        now_time = now_dt.time()
        weekday = now_dt.weekday()

        for rule in self.actions_rules:
            if not rule.get("enabled", True):
                continue
            if not self._rule_matches(rule, event, now_time, weekday,
                                      access_result):
                continue

            logger.info(
                "[%s] Правило '%s' сработало на событие #%s (%s)",
                self.camera_id, rule.get("name", "?"),
                event.get("id"), event.get("event_type"),
            )
            for action in rule.get("actions", []) or []:
                try:
                    self._execute_action(action, event, main)
                except Exception as e:
                    logger.exception(
                        "[%s] Ошибка действия %s: %s",
                        self.camera_id, action.get("type"), e,
                    )

    def _rule_matches(self, rule, event, now_time, weekday,
                      access_result=None) -> bool:
        cameras = rule.get("cameras") or []
        if cameras and event["camera_id"] not in cameras:
            return False

        types = rule.get("event_types") or []
        if types and event["event_type"] not in types:
            return False

        zones = rule.get("zones") or []
        if zones and (event.get("zone_name") not in zones):
            return False

        plates = rule.get("plates") or []
        plates_mode = (rule.get("plates_mode") or "any").lower()
        if plates and plates_mode != "any":
            plate = event.get("plate_text")
            if not plate:
                return False
            plate_norm = _normalize_plate(plate)
            plates_norm = {_normalize_plate(p) for p in plates}
            if plates_mode == "whitelist":
                if plate_norm not in plates_norm:
                    return False
            elif plates_mode == "blacklist":
                if plate_norm not in plates_norm:
                    return False

        access_actions = rule.get("access_actions") or []
        if access_actions:
            if not access_result:
                return False
            act = access_result.get("action")
            if act not in access_actions:
                return False

        tr = rule.get("time_range") or {}
        from_t = _parse_time(tr.get("from", ""))
        to_t = _parse_time(tr.get("to", ""))
        if from_t and to_t:
            if not _in_time_range(now_time, from_t, to_t):
                return False

        days = rule.get("days_of_week") or []
        if days and weekday not in days:
            return False

        return True

    def _execute_action(self, action: dict, event: dict, main: dict):
        a_type = action.get("type")
        if a_type == "marker":
            self._action_marker(action, event)
        elif a_type == "record":
            self._action_record(action, event)
        else:
            logger.warning("[%s] Неизвестный тип действия: %s",
                           self.camera_id, a_type)

    def _action_marker(self, action: dict, event: dict):
        marker_type = action.get("marker_type") or event.get("event_type", "event")
        description = action.get("description") or ""
        plate = event.get("plate_text")
        if plate:
            if description:
                description = f"{description}: {plate}"
            else:
                description = f"Номер: {plate}"

        snapshot_url = None
        if self.public_url and event.get("id"):
            snapshot_url = f"{self.public_url}/snapshot/{event['id']}"

        marker_id = self.vhs_client.create_marker(
            camera_id=self.camera_id,
            marker_type=marker_type,
            description=description,
            source="analytics",
            external_id=str(event.get("id")),
            snapshot_url=snapshot_url,
        )
        if marker_id is not None:
            with self._lock:
                self._action_count += 1
            logger.info(
                "[%s] Маркер #%s создан в Core (тип=%s, %s)",
                self.camera_id, marker_id, marker_type,
                f"номер {plate}" if plate else "",
            )

    def _action_record(self, action: dict, event: dict):
        duration = int(action.get("duration_sec", self.default_record_duration))
        duration = max(1, min(duration, self.max_record_duration))
        new_until = time.time() + duration

        with self._lock:
            timer_active = (
                self._record_timer is not None
                and self._record_timer.is_alive()
            )
            if timer_active:
                self._record_timer.cancel()
                self._record_until = new_until
                self._record_timer = threading.Timer(
                    duration, self._stop_recording_timer
                )
                self._record_timer.daemon = True
                self._record_timer.start()
                logger.info("[%s] Запись продлена на %d с",
                            self.camera_id, duration)
                return
            self._record_until = new_until

        ok = self.vhs_client.start_recording(self.camera_id)
        if not ok:
            logger.warning("[%s] Не удалось запустить запись в Core",
                           self.camera_id)
            return

        with self._lock:
            self._record_timer = threading.Timer(
                duration, self._stop_recording_timer
            )
            self._record_timer.daemon = True
            self._record_timer.start()
            self._action_count += 1
        logger.info("[%s] Запись в Core запущена на %d с",
                    self.camera_id, duration)

    def _stop_recording_timer(self):
        with self._lock:
            self._record_timer = None
            self._record_until = 0.0

        try:
            ok = self.vhs_client.stop_recording(self.camera_id)
            if ok:
                logger.info("[%s] Запись в Core остановлена по таймеру",
                            self.camera_id)
            else:
                logger.warning("[%s] Не удалось остановить запись в Core",
                               self.camera_id)
        except Exception as e:
            logger.exception("[%s] Ошибка остановки записи: %s",
                             self.camera_id, e)

    # ------------------------------------------------------------------ #
    #  Снапшот
    # ------------------------------------------------------------------ #
    def _save_snapshot(self, frame, detections, used_detector) -> Optional[str]:
        try:
            out = frame
            if used_detector is not None:
                try:
                    out = used_detector.annotate_frame(out, detections)
                except Exception as e:
                    logger.debug("[%s] Ошибка аннотации: %s",
                                 self.camera_id, e)
            else:
                for det in detections:
                    x, y, w, h = det["bbox"]
                    cv2.rectangle(out, (x, y), (x + w, y + h),
                                  (0, 255, 0), 2)

            if self.snapshot_width > 0:
                h, w = out.shape[:2]
                new_w = self.snapshot_width
                new_h = (self.snapshot_height
                         if self.snapshot_height > 0
                         else int(h * new_w / w))
                out = cv2.resize(out, (new_w, new_h))

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = self._camera_snap_dir / f"{ts}.jpg"

            ok, jpg = cv2.imencode(
                ".jpg", out,
                [cv2.IMWRITE_JPEG_QUALITY, self.snapshot_quality],
            )
            if not ok:
                return None
            path.write_bytes(jpg.tobytes())
            return str(path)
        except Exception as e:
            logger.exception("[%s] Не удалось сохранить снапшот: %s",
                             self.camera_id, e)
            return None

    # ------------------------------------------------------------------ #
    #  Управление
    # ------------------------------------------------------------------ #
    def start(self):
        self.stream.start()

    def stop(self):
        with self._lock:
            if self._record_timer and self._record_timer.is_alive():
                self._record_timer.cancel()
                self._record_timer = None
        self.stream.stop()

    def get_status(self) -> dict:
        status = self.stream.get_status()
        with self._lock:
            status["event_count"] = self._event_count
            status["action_count"] = self._action_count
            status["access_count"] = self._access_count
            status["recording_active"] = (
                self._record_timer is not None
                and self._record_timer.is_alive()
            )
        status["cooldown_sec"] = self.cooldown_sec
        status["detector_modes"] = self.detector_modes
        status["analytics_flags"] = {
            k: v for k, v in self.analytics_flags.items() if k != "zones"
        }
        status["actions_enabled"] = self.actions_enabled
        status["access_check_enabled"] = self.access_check_enabled
        return status


# ---------------------------------------------------------------------- #
#  Движок
# ---------------------------------------------------------------------- #
class AnalyticsEngine:
    """Центральный движок аналитики."""

    def __init__(
        self,
        config: dict,
        db,
        snapshots_dir: Path,
        base_dir: Optional[Path] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.db = db
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.on_event = on_event

        vhs_cfg = config.get("vhs_core", {}) or {}
        self.vhs_client = VhsClient(
            base_url=vhs_cfg.get("base_url", "http://localhost:8000"),
            api_token=vhs_cfg.get("api_token", ""),
            timeout=float(vhs_cfg.get("timeout", 5.0)),
            enabled=bool(vhs_cfg.get("actions_enabled", True)),
        )

        self.public_url = (
            (config.get("server", {}) or {}).get("public_url", "") or ""
        )

        self._workers: Dict[str, CameraWorker] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Создание детекторов по флагам
    # ------------------------------------------------------------------ #
    def _create_detectors(self, cam_cfg: dict) -> tuple:
        """
        Возвращает (detectors, detector_modes, analytics_flags).

        Читает флаги из cam_cfg["analytics"]. Для обратной совместимости
        поддерживает `analytics_mode`.
        """
        detector_cfg = self.config.get("detector", {}) or {}
        cam_id = cam_cfg["id"]

        # --- Нормализация флагов ---
        ana = cam_cfg.get("analytics") or {}

        # Если пришёл старый формат
        if "mode" in ana and "enabled" not in ana:
            from analytics_app import _legacy_mode_to_flags  # локальный импорт
            ana = _legacy_mode_to_flags(ana.get("mode"))
            ana["zones"] = cam_cfg.get("zones") or []

        flags = {
            "enabled":         bool(ana.get("enabled", False)),
            "detect_objects":  bool(ana.get("detect_objects", False)),
            "detect_plates":   bool(ana.get("detect_plates", False)),
            "detect_faces":    bool(ana.get("detect_faces", False)),
            "detect_motion":   bool(ana.get("detect_motion", False)),
            "zones":           ana.get("zones") or [],
        }

        if not flags["enabled"]:
            logger.info("[%s] Аналитика отключена", cam_id)
            return [], [], flags

        detectors = []
        detector_modes = []

        # --- YOLO (objects) ---
        if flags["detect_objects"]:
            yolo_cfg = detector_cfg.get("yolo", {}) or {}
            det = YoloDetector.from_config(
                camera_id=cam_id,
                camera_cfg=cam_cfg,
                yolo_cfg=yolo_cfg,
                base_dir=self.base_dir,
            )
            zones_cfg = flags.get("zones") or []
            if zones_cfg:
                det.zones = []
                for z in zones_cfg:
                    try:
                        det.zones.append(Zone.from_dict(z))
                    except (TypeError, ValueError):
                        pass
            else:
                det.zones = []
            detectors.append(det)
            detector_modes.append("yolo")
            logger.info("[%s] + YOLO-детектор (объекты)", cam_id)

        # --- ALPR (plates) ---
        if flags["detect_plates"]:
            alpr_cfg = detector_cfg.get("alpr", {}) or {}

            zones = []
            zones_cfg = flags.get("zones") or []
            if zones_cfg:
                for z in zones_cfg:
                    try:
                        zones.append(Zone.from_dict(z))
                    except (TypeError, ValueError):
                        pass

            alpr_debug = bool(alpr_cfg.get("debug", False))
            debug_dir = self.snapshots_dir / cam_id / "alpr_debug"

            det = AlprDetector(
                camera_id=cam_id,
                detector_model=alpr_cfg.get(
                    "detector_model",
                    "yolo-v9-t-384-license-plate-end2end",
                ),
                detection_confidence=float(
                    alpr_cfg.get("detection_confidence", 0.4)
                ),
                ocr_confidence=float(alpr_cfg.get("ocr_confidence", 0.5)),
                padding_ratio=float(alpr_cfg.get("padding_ratio", 0.1)),
                process_every_n_frames=int(
                    alpr_cfg.get("process_every_n_frames", 5)
                ),
                voting_window_sec=float(
                    alpr_cfg.get("voting_window_sec", 2.0)
                ),
                min_votes=int(alpr_cfg.get("min_votes", 3)),
                cooldown_same_plate_sec=float(
                    alpr_cfg.get("cooldown_same_plate_sec", 30)
                ),
                cooldown_any_sec=float(
                    alpr_cfg.get("cooldown_any_sec", 5)
                ),
                zones=zones,
                ocr_target_height=int(
                    alpr_cfg.get("ocr_target_height", 96)
                ),
                ocr_use_threshold=bool(
                    alpr_cfg.get("ocr_use_threshold", False)
                ),
                debug=alpr_debug,
                debug_dir=debug_dir,
            )
            detectors.append(det)
            detector_modes.append("alpr")
            logger.info("[%s] + ALPR-детектор (номера)", cam_id)

        # --- FACE (faces) ---
        if flags["detect_faces"]:
            logger.warning(
                "[%s] Флаг detect_faces включён, "
                "но модуль распознавания лиц пока не реализован",
                cam_id,
            )

        # --- MOTION (MOG2) ---
        if flags["detect_motion"]:
            motion_cfg = detector_cfg.get("motion", {}) or {}
            det = MotionDetector.from_config(
                camera_id=cam_id,
                camera_cfg=cam_cfg,
                detector_cfg=motion_cfg,
            )
            detectors.append(det)
            detector_modes.append("motion")
            logger.info("[%s] + MOG2-детектор (движение)", cam_id)

        # Если флаги есть, но ни один детектор не создан — fallback на motion
        if not detectors:
            logger.warning(
                "[%s] Флаги заданы, но детекторы не созданы. "
                "Включаю motion по умолчанию", cam_id,
            )
            motion_cfg = detector_cfg.get("motion", {}) or {}
            det = MotionDetector.from_config(
                camera_id=cam_id,
                camera_cfg=cam_cfg,
                detector_cfg=motion_cfg,
            )
            detectors.append(det)
            detector_modes.append("motion")

        return detectors, detector_modes, flags

    # ------------------------------------------------------------------ #
    #  Жизненный цикл
    # ------------------------------------------------------------------ #
    def start_all(self):
        vhs_cfg = self.config.get("vhs_core", {}) or {}
        base_url = vhs_cfg.get("base_url", "http://localhost:8000")
        api_token = vhs_cfg.get("api_token", "")

        events_cfg = self.config.get("events", {}) or {}
        actions_cfg = self.config.get("actions", {}) or {}

        if actions_cfg.get("enabled") and self.vhs_client.enabled:
            logger.info("Обратный канал включён: %s", self.vhs_client.base_url)
            acc = actions_cfg.get("access_check_enabled", True)
            if acc:
                logger.info("Проверка АСКУД включена")
            else:
                logger.info("Проверка АСКУД отключена")
        else:
            logger.info("Обратный канал отключён")

        cameras = self.config.get("cameras", []) or []
        logger.info("Камер в конфиге: %d", len(cameras))

        for cam_cfg in cameras:
            cam_id = cam_cfg.get("id")
            if not cam_id:
                continue
            if not cam_cfg.get("enabled", True):
                logger.info("[%s] Камера отключена в конфиге", cam_id)
                continue

            # Нормализуем флаги для проверки
            ana = cam_cfg.get("analytics") or {}
            if "mode" in ana and "enabled" not in ana:
                from analytics_app import _legacy_mode_to_flags
                ana = _legacy_mode_to_flags(ana.get("mode"))

            if not ana.get("enabled"):
                logger.info("[%s] Аналитика не запрошена", cam_id)
                continue

            try:
                self._start_worker(
                    cam_cfg, base_url, api_token,
                    events_cfg, actions_cfg,
                )
            except Exception as e:
                logger.exception("[%s] Не удалось запустить: %s", cam_id, e)

        logger.info("Запущено воркеров аналитики: %d", len(self._workers))

    def _start_worker(self, cam_cfg, base_url, api_token,
                      events_cfg, actions_cfg):
        cam_id = cam_cfg["id"]
        cam_name = cam_cfg.get("name", cam_id)

        stream = CameraStream(
            camera_id=cam_id,
            camera_name=cam_name,
            base_url=base_url,
            api_token=api_token,
        )
        detectors, detector_modes, flags = self._create_detectors(cam_cfg)

        if not detectors:
            logger.warning("[%s] Нет детекторов — воркер не создан", cam_id)
            return

        worker = CameraWorker(
            camera_id=cam_id,
            camera_name=cam_name,
            stream=stream,
            detectors=detectors,
            detector_modes=detector_modes,
            analytics_flags=flags,
            db=self.db,
            snapshots_dir=self.snapshots_dir,
            events_cfg=events_cfg,
            actions_cfg=actions_cfg,
            vhs_client=self.vhs_client,
            public_url=self.public_url,
            on_event=self.on_event,
        )
        worker.start()

        with self._lock:
            self._workers[cam_id] = worker

    def stop_all(self):
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            try:
                w.stop()
            except Exception as e:
                logger.error("[%s] Ошибка остановки: %s", w.camera_id, e)
        logger.info("Все воркеры аналитики остановлены")

    def start_camera(self, camera_id: str) -> bool:
        with self._lock:
            if camera_id in self._workers:
                return False

        cam_cfg = None
        for c in self.config.get("cameras", []) or []:
            if c.get("id") == camera_id:
                cam_cfg = c
                break
        if not cam_cfg:
            return False

        ana = cam_cfg.get("analytics") or {}
        if not ana.get("enabled") and "mode" not in ana:
            return False

        vhs_cfg = self.config.get("vhs_core", {}) or {}
        events_cfg = self.config.get("events", {}) or {}
        actions_cfg = self.config.get("actions", {}) or {}
        self._start_worker(
            cam_cfg,
            vhs_cfg.get("base_url", "http://localhost:8000"),
            vhs_cfg.get("api_token", ""),
            events_cfg,
            actions_cfg,
        )
        return True

    def stop_camera(self, camera_id: str) -> bool:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if not worker:
            return False
        worker.stop()
        return True

    # ------------------------------------------------------------------ #
    #  Доступ к состоянию
    # ------------------------------------------------------------------ #
    def get_status_all(self) -> List[dict]:
        with self._lock:
            workers = list(self._workers.values())
        return [w.get_status() for w in workers]

    def get_worker(self, camera_id: str) -> Optional[CameraWorker]:
        with self._lock:
            return self._workers.get(camera_id)

    def get_last_frame(self, camera_id: str):
        worker = self.get_worker(camera_id)
        if not worker:
            return None
        return worker.stream.get_last_frame()

    def reload_config(self):
        logger.info("Перезагрузка конфигурации аналитики...")
        self.stop_all()
        with self._lock:
            self._workers.clear()
        self.start_all()