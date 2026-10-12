"""
Тест детектора номеров (YOLOv8 ONNX) на тестовых картинках.

Запуск:
    python test_plate_detector.py
"""

import sys
import time
from pathlib import Path

import cv2
import numpy as np


# ---------------------------------------------------------------------- #
#  Рисование bbox
# ---------------------------------------------------------------------- #
def draw_boxes(frame, detections, color=(0, 255, 0)):
    out = frame.copy()
    for det in detections:
        x, y, w, h = det["bbox"]
        conf = det["confidence"]
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        label = f"plate {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(out, (x, max(0, y - th - 6)), (x + tw + 4, y), color, -1)
        cv2.putText(out, label, (x + 2, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return out


# ---------------------------------------------------------------------- #
#  Простой YOLO-детектор через cv2.dnn
# ---------------------------------------------------------------------- #
class PlateDetector:
    def __init__(self, model_path, conf_threshold=0.3, nms_threshold=0.45,
                 input_size=(640, 640)):
        self.model_path = Path(model_path)
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.input_size = input_size

        if not self.model_path.exists():
            raise FileNotFoundError(f"Модель не найдена: {self.model_path}")

        print(f"Загружаю модель: {self.model_path.name}")
        self._net = cv2.dnn.readNetFromONNX(str(self.model_path))
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

        # Прогрев
        t0 = time.time()
        test = np.zeros((480, 640, 3), dtype=np.uint8)
        self.detect(test)
        print(f"Прогрев: {time.time() - t0:.2f} сек")

    def detect(self, frame):
        h, w = frame.shape[:2]

        blob = cv2.dnn.blobFromImage(
            frame, scalefactor=1.0 / 255.0, size=self.input_size,
            mean=(0, 0, 0), swapRB=True, crop=False,
        )
        self._net.setInput(blob)
        outputs = self._net.forward()

        # outputs: (1, 5, 8400) для модели с 1 классом (номер)
        # 5 = 4 (cx,cy,w,h) + 1 (confidence)
        preds = np.squeeze(outputs)
        if preds.ndim != 2:
            return []

        # Транспонируем, если нужно
        if preds.shape[0] < preds.shape[1]:
            preds = preds.T  # (8400, 5)

        # Координаты и confidence
        boxes_cxcywh = preds[:, :4]
        scores = preds[:, 4]

        mask = scores >= self.conf_threshold
        if not np.any(mask):
            return []

        boxes_cxcywh = boxes_cxcywh[mask]
        scores = scores[mask]

        # Простой NMS
        keep = self._nms(boxes_cxcywh, scores, self.nms_threshold)
        if not keep:
            return []

        boxes_cxcywh = boxes_cxcywh[keep]
        scores = scores[keep]

        # Масштабируем к исходному кадру
        sx = w / self.input_size[0]
        sy = h / self.input_size[1]

        detections = []
        for i in range(len(boxes_cxcywh)):
            cx, cy, bw, bh = boxes_cxcywh[i]
            x = (cx - bw / 2) * sx
            y = (cy - bh / 2) * sy
            bw_s = bw * sx
            bh_s = bh * sy
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            bw_s = max(1, min(w - x, bw_s))
            bh_s = max(1, min(h - y, bh_s))
            detections.append({
                "bbox": (int(x), int(y), int(bw_s), int(bh_s)),
                "confidence": float(scores[i]),
            })
        return detections

    @staticmethod
    def _nms(boxes_cxcywh, scores, nms_threshold):
        if len(boxes_cxcywh) == 0:
            return []
        x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
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
#  Main
# ---------------------------------------------------------------------- #
def main() -> int:
    base = Path(__file__).parent
    model_path = base / "models" / "plate_detector.onnx"

    if not model_path.exists():
        print(f"Модель не найдена: {model_path}")
        print()
        print("Скачайте YOLO-модель номеров с HuggingFace и положите её сюда.")
        print("Например: keremberke/yolov8n-license-plate")
        return 1

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

    detector = PlateDetector(model_path, conf_threshold=0.3)

    print()
    print("=" * 70)
    total = 0
    for img_path in images:
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"{img_path.name}: не удалось прочитать")
            continue

        t0 = time.time()
        detections = detector.detect(frame)
        dt = (time.time() - t0) * 1000

        print(f"{img_path.name}: найдено {len(detections)} номеров  ({dt:.0f} мс)")
        for d in detections:
            print(f"    bbox={d['bbox']}  conf={d['confidence']:.3f}")

        total += len(detections)

        # Сохраняем визуализацию
        out = draw_boxes(frame, detections)
        out_path = base / f"plate_detect_{img_path.stem}.jpg"
        cv2.imwrite(str(out_path), out)
        print(f"    визуализация: {out_path.name}")

    print()
    print("=" * 70)
    print(f"Всего найдено номеров: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())