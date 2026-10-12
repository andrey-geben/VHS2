"""
Модуль детекции движения.

MotionDetector:
- использует OpenCV MOG2 (адаптивная фоновая модель);
- поддерживает зоны интереса (ROI) — анализирует только прямоугольники,
  заданные в конфиге;
- фильтрует шум по min_area, max_area и solidity;
- применяет морфологию для устранения «дырок» в маске;
- возвращает список обнаруженных объектов с координатами и площадью.

Один экземпляр MotionDetector на камеру!
"""

import logging
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Зона интереса
# ---------------------------------------------------------------------- #
class Zone:
    """Прямоугольная зона интереса в кадре."""

    def __init__(self, name: str, x: int, y: int, w: int, h: int):
        self.name = name
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    @property
    def area(self) -> int:
        return self.w * self.h

    def contains_bbox(self, bbox: Tuple[int, int, int, int],
                      min_overlap: float = 0.3) -> bool:
        bx, by, bw, bh = bbox
        ix = max(self.x, bx)
        iy = max(self.y, by)
        ix2 = min(self.x + self.w, bx + bw)
        iy2 = min(self.y + self.h, by + bh)
        iw = max(0, ix2 - ix)
        ih = max(0, iy2 - iy)
        inter = iw * ih
        if inter <= 0:
            return False
        return inter >= (bw * bh) * min_overlap

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y,
                "w": self.w, "h": self.h}

    @staticmethod
    def from_dict(data: dict) -> "Zone":
        return Zone(
            name=data.get("name", "zone"),
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            w=int(data.get("w", 0)),
            h=int(data.get("h", 0)),
        )


# ---------------------------------------------------------------------- #
#  Детектор движения
# ---------------------------------------------------------------------- #
class MotionDetector:
    """
    Детектор движения на MOG2.

    Один экземпляр на камеру. detect() вызывается для каждого кадра.

    Фильтры:
    - min_area   — минимальная площадь контура (отсекает мелкий шум);
    - max_area   — максимальная площадь контура. Если контур занимает
                   почти всю зону — это изменение освещения, не объект.
                   None = без ограничения.
    - min_solidity — минимальная «плотность» контура (area / bbox_area).
                   Шум и размытые пятна дают низкую solidity.
                   0.0 = без фильтра.
    """

    def __init__(
        self,
        camera_id: str,
        history: int = 500,
        var_threshold: float = 25.0,
        min_area: int = 800,
        max_area: Optional[int] = None,
        min_solidity: float = 0.35,
        detect_shadows: bool = False,
        morph_kernel: int = 5,
        zones: Optional[List[Zone]] = None,
        warmup_frames: int = 60,
    ):
        self.camera_id = camera_id
        self.min_area = int(min_area)
        self.max_area = int(max_area) if max_area else None
        self.min_solidity = max(0.0, min(1.0, float(min_solidity)))
        self.morph_kernel = max(1, morph_kernel)
        self.zones: List[Zone] = zones or []
        self.warmup_frames = max(0, int(warmup_frames))

        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=detect_shadows,
        )

        self._kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (self.morph_kernel, self.morph_kernel)
        )

        self._frame_count = 0

    # ------------------------------------------------------------------ #
    #  Основной метод
    # ------------------------------------------------------------------ #
    def detect(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """
        Обрабатывает кадр, возвращает список найденных объектов.

        Каждый объект:
        {
            'bbox': (x, y, w, h),
            'area': int,
            'solidity': float,
            'zone': str | None,
            'center': (cx, cy),
        }
        """
        self._frame_count += 1
        warming = self._frame_count <= self.warmup_frames

        # 1. Маска движения
        fg_mask = self._bg.apply(frame)

        # 2. Морфология
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self._kernel)
        fg_mask = cv2.dilate(fg_mask, self._kernel, iterations=2)
        # Закрываем «дырки» внутри объектов (человек в тёмной одежде)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, self._kernel)

        # 3. Поиск контуров
        contours, _ = cv2.findContours(
            fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        results: List[Dict[str, Any]] = []

        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue

            x, y, w, h = cv2.boundingRect(c)
            bbox = (x, y, w, h)
            bbox_area = w * h
            solidity = area / bbox_area if bbox_area > 0 else 0.0

            # Максимальная площадь: если объект «съел» всю зону —
            # это изменение освещения, а не движение
            if self.max_area and area > self.max_area:
                logger.debug(
                    "[%s] Отклонён крупный контур area=%d (>max_area=%d)",
                    self.camera_id, area, self.max_area,
                )
                continue

            # Solidity: слишком «размазанный» контур — шум
            if solidity < self.min_solidity:
                logger.debug(
                    "[%s] Отклонён контур с solidity=%.2f (area=%d)",
                    self.camera_id, solidity, area,
                )
                continue

            # Фильтр по зонам
            matched_zone: Optional[str] = None
            if self.zones:
                for zone in self.zones:
                    if zone.contains_bbox(bbox, min_overlap=0.3):
                        matched_zone = zone.name
                        break
                if matched_zone is None:
                    continue

            if warming:
                continue

            cx, cy = x + w // 2, y + h // 2
            results.append({
                "bbox": bbox,
                "area": int(area),
                "solidity": round(solidity, 3),
                "zone": matched_zone,
                "center": (cx, cy),
            })

        return results

    # ------------------------------------------------------------------ #
    #  Вспомогательные методы
    # ------------------------------------------------------------------ #
    def annotate_frame(self, frame: np.ndarray,
                       detections: List[Dict[str, Any]]) -> np.ndarray:
        """Рисует зоны и найденные объекты поверх кадра."""
        out = frame.copy()

        for zone in self.zones:
            cv2.rectangle(
                out,
                (zone.x, zone.y),
                (zone.x + zone.w, zone.y + zone.h),
                (0, 200, 255), 2,
            )
            cv2.putText(
                out, zone.name,
                (zone.x + 5, zone.y + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (0, 200, 255), 1, cv2.LINE_AA,
            )

        for det in detections:
            x, y, w, h = det["bbox"]
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(
                out, f"{det['area']}px s={det['solidity']}",
                (x, max(15, y - 5)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (0, 0, 255), 1, cv2.LINE_AA,
            )

        return out

    def reset(self):
        """Сбрасывает фоновую модель."""
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=25, detectShadows=False,
        )
        self._frame_count = 0

    # ------------------------------------------------------------------ #
    #  Фабричный метод
    # ------------------------------------------------------------------ #
    @staticmethod
    def from_config(camera_id: str, camera_cfg: dict,
                    detector_cfg: dict) -> "MotionDetector":
        zones = []
        for z in camera_cfg.get("zones") or []:
            try:
                zones.append(Zone.from_dict(z))
            except (TypeError, ValueError) as e:
                logger.warning(
                    "[%s] Пропускаю некорректную зону %s: %s",
                    camera_id, z, e,
                )

        # Если max_area не задан явно — вычислим 40% от площади зон
        # (или от всего кадра, если зон нет)
        max_area = detector_cfg.get("max_area")
        if max_area is None and zones:
            total_zone_area = sum(z.area for z in zones)
            max_area = int(total_zone_area * 0.4)
            logger.info(
                "[%s] max_area не задан, вычислен как 40%% площади зон: %d",
                camera_id, max_area,
            )

        return MotionDetector(
            camera_id=camera_id,
            history=int(detector_cfg.get("history", 500)),
            var_threshold=float(detector_cfg.get("var_threshold", 25)),
            min_area=int(detector_cfg.get("min_area", 800)),
            max_area=max_area,
            min_solidity=float(detector_cfg.get("min_solidity", 0.35)),
            detect_shadows=bool(detector_cfg.get("detect_shadows", False)),
            morph_kernel=int(detector_cfg.get("morph_kernel", 5)),
            zones=zones,
            warmup_frames=int(detector_cfg.get("warmup_frames", 60)),
        )