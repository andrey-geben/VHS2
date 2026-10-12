"""
Одноразовый экспорт YOLOv8 в ONNX.

Зачем: для работы YOLO-детектора в runtime нужен только OpenCV (cv2.dnn),
а модель — файл .onnx. Но чтобы получить .onnx из оригинальной .pt,
нужен пакет ultralytics (тянет за собой torch ~2 ГБ). Этот скрипт
делает экспорт один раз, после чего ultralytics можно удалить.

Использование:
    pip install ultralytics
    python tools/export_yolo.py --model yolov8n
    pip uninstall -y ultralytics torch torchvision

Результат:
    models/yolov8n.onnx (~12 МБ) — модель для runtime.

Аргументы:
    --model   yolov8n | yolov8s | yolov8m | yolov8l | yolov8x
              n — самая лёгкая (10–20 FPS на CPU), x — точная, но тяжёлая.
    --imgsz   размер входного изображения (по умолчанию 640)
    --output  папка для сохранения ONNX (по умолчанию models/)
"""

import argparse
import shutil
import sys
from pathlib import Path


MODEL_CHOICES = ["yolov8n", "yolov8s", "yolov8m", "yolov8l", "yolov8x"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Экспорт YOLOv8 (.pt) в ONNX для использования в VHS-Analytics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        default="yolov8n",
        choices=MODEL_CHOICES,
        help="Модель YOLOv8 (по умолчанию yolov8n — самая лёгкая)",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=640,
        help="Размер входного изображения (по умолчанию 640)",
    )
    parser.add_argument(
        "--output",
        default="models",
        help="Папка для сохранения ONNX (по умолчанию models/)",
    )
    args = parser.parse_args()

    # --- Проверка ultralytics ---
    try:
        from ultralytics import YOLO
    except ImportError:
        print("=" * 64)
        print("  ОШИБКА: пакет ultralytics не установлен.")
        print("=" * 64)
        print()
        print("Установите его (это временная зависимость только для экспорта):")
        print()
        print("    pip install ultralytics")
        print()
        print("Затем запустите этот скрипт снова.")
        print()
        print("После экспорта ultralytics можно удалить:")
        print()
        print("    pip uninstall -y ultralytics torch torchvision")
        print()
        return 1

    # --- Загрузка модели ---
    print(f"[1/3] Загружаю модель {args.model}.pt ...")
    print("      (при первом запуске скачается с GitHub, ~6 МБ)")
    try:
        model = YOLO(f"{args.model}.pt")
    except Exception as e:
        print(f"ОШИБКА загрузки модели: {e}")
        return 2

    # --- Экспорт в ONNX ---
    print(f"[2/3] Экспортирую в ONNX (imgsz={args.imgsz}, opset=12)...")
    print("      Это может занять 30–60 секунд.")
    try:
        onnx_path = model.export(
            format="onnx",
            imgsz=args.imgsz,
            opset=12,
            dynamic=False,      # фиксированные размеры — критично для cv2.dnn
            simplify=True,      # упрощает граф, ускоряет инференс
        )
    except Exception as e:
        print(f"ОШИБКА экспорта: {e}")
        return 3

    # --- Перемещение в models/ ---
    print(f"[3/3] Перемещаю результат в {args.output}/ ...")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{args.model}.onnx"

    src = Path(onnx_path)
    if not src.exists():
        print(f"ОШИБКА: экспортированный файл {onnx_path} не найден.")
        return 4

    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))

    # --- Итог ---
    size_mb = dst.stat().st_size / 1024 / 1024
    print()
    print("=" * 64)
    print(f"  ГОТОВО")
    print("=" * 64)
    print(f"  Модель:  {dst.resolve()}")
    print(f"  Размер:  {size_mb:.1f} МБ")
    print(f"  Классов: 80 (COCO)")
    print()
    print("  Теперь можно удалить временные зависимости:")
    print("      pip uninstall -y ultralytics torch torchvision")
    print()
    print("  Runtime использует только cv2.dnn — ничего лишнего.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())