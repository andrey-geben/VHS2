"""
YOLOv8-детектор объектов через OpenCV DNN (без PyTorch в runtime).

YoloDetector:
- загружает ONNX-модель через cv2.dnn.readNetFromONNX;
- выполняет инференс на CPU или GPU (CUDA, если доступно);
- возвращает список объектов с классом, confidence и bbox;
- поддерживает фильтрацию по классам (только person, только car и т.п.);
- совместим по интерфейсу с MotionDetector.detect(frame).

Модель: models/yolov8n.onnx (экспортируется tools/export_yolo.py).

Один экземпляр YoloDetector на камеру!
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Классы COCO (80 штук)
# ---------------------------------------------------------------------- #
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

# Группы для удобной фильтрации в конфиге
CLASS_GROUPS = {
    "people":    ["person"],
    "vehicles":  ["bicycle", "car", "motorcycle", "bus", "truck"],
    "transport": ["bicycle", "car", "motorcycle", "bus", "truck", "train",
                  "boat", "airplane"],
    "animals":   ["bird", "cat", "dog", "horse", "sheep", "cow",
                  "elephant", "bear", "zebra", "giraffe"],
}


# ---------------------------------------------------------------------- #
#  Цвета для отрисовки по классам
# ---------------------------------------------------------------------- #
CLASS_COLORS = {
    "person":     (0, 200, 255),    # голубой
    "car":        (0, 255, 0),      # зелёный
    "truck":      (0, 165, 255),    # оранжевый
    "bus":        (0, 100, 255),    # тёмно-оранжевый
    "motorcycle": (255, 100, 0),    # синий
    "bicycle":    (255, 200, 0),    # голубовато-жёлтый
}
DEFAULT_COLOR = (0, 0, 255)         # красный для остальных


# ---------------------------------------------------------------------- #
#  Non-Maximum Suppression
# ---------------------------------------------------------------------- #
def _nms(boxes_cxcywh: np.ndarray, scores: np.ndarray,
         nms_threshold: float) -> List[int]:
    """
    Non-Maximum Suppression для боксов в формате [cx, cy, w, h].

    YOLO отдаёт боксы именно в этом формате (центр + размеры),
    поэтому NMS сразу работает с ними — без промежуточной конвертации.

    boxes_cxcywh: (N, 4) — cx, cy, w, h
    scores: (N,)
    Возвращает список индексов оставшихся боксов.
    """
    if len(boxes_cxcywh) == 0:
        return []

    # cx, cy, w, h → x1, y1, x2, y2
    x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
    y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
    x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
    y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2

    areas = (x2 - x1) * (y2 - y1)

    order = scores.argsort()[::-1]
    keep: List[int] = []

    while order.size > 0:
        i = order[0]
        keep.append(int(i))

        if order.size == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h

        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        inds = np.where(iou <= nms_threshold)[0]
        order = order[inds + 1]

    return keep


# ---------------------------------------------------------------------- #
#  Детектор
# ---------------------------------------------------------------------- #
class YoloDetector:
    """
    YOLOv8-детектор через cv2.dnn.

    Один экземпляр на камеру.
    """

    def __init__(
        self,
        camera_id: str,
        model_path: str,
        confidence_threshold: float = 0.4,
        nms_threshold: float = 0.45,
        input_size: Tuple[int, int] = (640, 640),
        allowed_classes: Optional[List[str]] = None,
        min_area: int = 0,
        use_gpu: bool = False,
        warmup: bool = True,
    ):
        self.camera_id = camera_id
        self.model_path = Path(model_path)
        self.conf_threshold = float(confidence_threshold)
        self.nms_threshold = float(nms_threshold)
        self.input_size = input_size   # (width, height)

        # Пустой список = все классы
        self.allowed_classes = set(allowed_classes) if allowed_classes else None

        self.min_area = int(min_area)

        # --- Загрузка модели ---
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Модель не найдена: {self.model_path}. "
                f"Запустите tools/export_yolo.py для экспорта."
            )

        logger.info("[%s] Загружаю YOLO-модель: %s",
                    camera_id, self.model_path.name)
        self._net = cv2.dnn.readNetFromONNX(str(self.model_path))

        # --- GPU/CPU ---
        self._using_gpu = False
        if use_gpu:
            try:
                self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
                test_blob = np.zeros((1, 3, *self.input_size[::-1]),
                                     dtype=np.float32)
                self._net.setInput(test_blob)
                self._net.forward()
                self._using_gpu = True
                logger.info("[%s] Используется GPU (CUDA)", camera_id)
            except cv2.error as e:
                logger.warning(
                    "[%s] GPU недоступен (%s), переключаюсь на CPU",
                    camera_id, e,
                )
                self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        else:
            self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

        logger.info(
            "[%s] YoloDetector готов: conf=%.2f, nms=%.2f, imgsz=%dx%d, classes=%s",
            camera_id, self.conf_threshold, self.nms_threshold,
            self.input_size[0], self.input_size[1],
            sorted(self.allowed_classes) if self.allowed_classes else "все",
        )

        # Прогрев (первый инференс медленный — JIT-компиляция слоёв)
        if warmup:
            try:
                test = np.zeros((480, 640, 3), dtype=np.uint8)
                self.detect(test)
                logger.info("[%s] Прогрев модели выполнен", camera_id)
            except Exception as e:
                logger.warning("[%s] Прогрев не удался: %s", camera_id, e)

    # ------------------------------------------------------------------ #
    #  Основной метод
    # ------------------------------------------------------------------ #
    def detect(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """
        Выполняет детекцию на кадре.

        Возвращает список объектов:
        {
            'bbox': (x, y, w, h),
            'area': int,
            'class_id': int,
            'class_name': str,
            'confidence': float,
            'solidity': float,
            'zone': str | None,
            'center': (cx, cy),
        }
        """
        h, w = frame.shape[:2]

        # --- Препроцессинг ---
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1.0 / 255.0,
            size=self.input_size,
            mean=(0, 0, 0),
            swapRB=True,
            crop=False,
        )
        self._net.setInput(blob)

        # --- Инференс ---
        try:
            outputs = self._net.forward()
        except cv2.error as e:
            logger.error("[%s] Ошибка инференса: %s", self.camera_id, e)
            return []

        # --- Парсинг выхода ---
        # YOLOv8n@640: (1, 84, 8400), где 84 = 4 (bbox) + 80 (классы)
        predictions = np.squeeze(outputs)
        if predictions.ndim != 2:
            logger.error("[%s] Неожиданный формат выхода: %s",
                         self.camera_id, outputs.shape)
            return []

        # Ориентация: (84, 8400) → транспонируем, (8400, 84) → оставляем
        if predictions.shape[0] == 84:
            predictions = predictions.T  # (8400, 84)

        # Координаты и классы
        boxes_cxcywh = predictions[:, :4]              # (8400, 4) cx, cy, w, h
        class_scores = predictions[:, 4:]              # (8400, 80)
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(len(class_ids)), class_ids]

        # Фильтр по confidence
        mask = confidences >= self.conf_threshold
        if not np.any(mask):
            return []

        boxes_cxcywh = boxes_cxcywh[mask]
        class_ids = class_ids[mask]
        confidences = confidences[mask]

        # --- NMS в формате cx,cy,w,h (как отдаёт YOLO) ---
        keep = _nms(boxes_cxcywh, confidences, self.nms_threshold)
        if not keep:
            return []

        boxes_cxcywh = boxes_cxcywh[keep]
        class_ids = class_ids[keep]
        confidences = confidences[keep]

        # --- Масштабирование координат к исходному кадру ---
        scale_x = w / self.input_size[0]
        scale_y = h / self.input_size[1]

        results: List[Dict[str, Any]] = []
        for i in range(len(boxes_cxcywh)):
            cx, cy, bw, bh = boxes_cxcywh[i]

            # cx,cy,w,h → x,y,w,h (левый верхний угол)
            x = (cx - bw / 2) * scale_x
            y = (cy - bh / 2) * scale_y
            bw_s = bw * scale_x
            bh_s = bh * scale_y

            # Клиппинг к границам кадра
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            bw_s = max(1, min(w - x, bw_s))
            bh_s = max(1, min(h - y, bh_s))

            bx, by, bw_i, bh_i = int(x), int(y), int(bw_s), int(bh_s)
            area = bw_i * bh_i

            cls_id = int(class_ids[i])
            cls_name = (COCO_CLASSES[cls_id]
                        if cls_id < len(COCO_CLASSES) else f"cls_{cls_id}")

            # Фильтр по разрешённым классам
            if self.allowed_classes and cls_name not in self.allowed_classes:
                continue

            # Фильтр по минимальной площади
            if self.min_area > 0 and area < self.min_area:
                continue

            results.append({
                "bbox": (bx, by, bw_i, bh_i),
                "area": area,
                "class_id": cls_id,
                "class_name": cls_name,
                "confidence": float(confidences[i]),
                "solidity": 1.0,
                "zone": None,
                "center": (bx + bw_i // 2, by + bh_i // 2),
            })

        return results

    # ------------------------------------------------------------------ #
    #  Отрисовка
    # ------------------------------------------------------------------ #
    def annotate_frame(self, frame: np.ndarray,
                       detections: List[Dict[str, Any]]) -> np.ndarray:
        """Рисует найденные объекты поверх кадра."""
        out = frame.copy()

        for det in detections:
            x, y, w, h = det["bbox"]
            cls = det.get("class_name", "?")
            conf = det.get("confidence", 0)
            color = CLASS_COLORS.get(cls, DEFAULT_COLOR)

            cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)

            label = f"{cls} {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
            )
            cv2.rectangle(
                out, (x, max(0, y - th - 6)), (x + tw + 4, y), color, -1
            )
            cv2.putText(
                out, label,
                (x + 2, max(12, y - 4)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (0, 0, 0), 1, cv2.LINE_AA,
            )

        return out

    def reset(self):
        """У YOLO нет фоновой модели — метод для совместимости."""
        pass

    # ------------------------------------------------------------------ #
    #  Фабричный метод
    # ------------------------------------------------------------------ #
    @staticmethod
    def from_config(camera_id: str, camera_cfg: dict,
                    yolo_cfg: dict, base_dir: Path) -> "YoloDetector":
        """
        Создаёт YoloDetector из конфигов.

        yolo_cfg — секция detector.yolo из config.yaml.
        base_dir — папка, где лежит models/ (рядом с exe или с app.py).
        """
        model_rel = yolo_cfg.get("model_path", "models/yolov8n.onnx")
        model_path = Path(model_rel)
        if not model_path.is_absolute():
            model_path = base_dir / model_path

        # Разрешённые классы
        allowed = camera_cfg.get("detect_classes")
        if allowed is None:
            allowed = yolo_cfg.get("classes")
        if allowed is not None and len(allowed) == 0:
            allowed = None  # пустой список = все классы

        # Разворачиваем группы (people → person)
        if allowed:
            expanded: List[str] = []
            for item in allowed:
                if item in CLASS_GROUPS:
                    expanded.extend(CLASS_GROUPS[item])
                else:
                    expanded.append(item)
            allowed = list(set(expanded))

        imgsz = int(yolo_cfg.get("imgsz", 640))

        return YoloDetector(
            camera_id=camera_id,
            model_path=str(model_path),
            confidence_threshold=float(yolo_cfg.get("confidence", 0.4)),
            nms_threshold=float(yolo_cfg.get("nms", 0.45)),
            input_size=(imgsz, imgsz),
            allowed_classes=allowed,
            min_area=int(yolo_cfg.get("min_area", 0)),
            use_gpu=bool(yolo_cfg.get("use_gpu", False)),
            warmup=bool(yolo_cfg.get("warmup", True)),
        )