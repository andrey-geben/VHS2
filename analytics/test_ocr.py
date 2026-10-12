"""
Тест RapidOCR на российских номерах с постобработкой v2.
"""

import sys
import time
import re
from pathlib import Path

from rapidocr_onnxruntime import RapidOCR


PLATE_REGEX = re.compile(
    # Основная часть: буква, 3 цифры, 2 буквы — обязательно.
    # Регион (2-3 цифры) — опционально: часто затёрт, обрезан,
    # не влез в кадр или на фото его просто нет.
    r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}(\d{2,3})?$"
)

LAT_TO_CYR = {
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М",
    "H": "Н", "O": "О", "P": "Р", "C": "С", "T": "Т",
    "X": "Х", "Y": "У",
}

GARBAGE_PREFIXES = ("RUS", "РУС")


# ---------------------------------------------------------------------- #
#  Нормализация и валидация
# ---------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """Всё в кириллицу, только буквы и цифры, верхний регистр."""
    text = re.sub(r"[^А-ЯA-Z0-9]", "", text.upper())
    for lat, cyr in LAT_TO_CYR.items():
        text = text.replace(lat, cyr)
    return text


def fix_letter_digit_confusion(text: str) -> str:
    """
    Позиционная эвристика для российского номера:
      позиция 0     — буква (0 → О)
      позиции 1-3   — цифры (О → 0)
      позиции 4-5   — буквы (0 → О)
      позиции 6+    — цифры (О → 0, для региона)
    Работает только если длина хотя бы 6 символов.
    """
    if len(text) < 6:
        return text

    chars = list(text)

    def to_letter(ch: str) -> str:
        return "О" if ch == "0" else ch

    def to_digit(ch: str) -> str:
        return "0" if ch == "О" else ch

    chars[0] = to_letter(chars[0])
    for i in (1, 2, 3):
        chars[i] = to_digit(chars[i])
    chars[4] = to_letter(chars[4])
    chars[5] = to_letter(chars[5])
    for i in range(6, len(chars)):
        chars[i] = to_digit(chars[i])

    return "".join(chars)


def is_valid_plate(text: str) -> bool:
    return bool(PLATE_REGEX.match(text))


# ---------------------------------------------------------------------- #
#  Работа с блоками
# ---------------------------------------------------------------------- #
def bbox_to_rect(bbox) -> tuple[float, float, float, float]:
    """bbox (4 точки) → (x_min, y_min, x_max, y_max)."""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return min(xs), min(ys), max(xs), max(ys)


def blocks_are_neighbors(b1, b2, max_gap_ratio: float = 3.0) -> bool:
    """
    Проверяет, что блок b2 находится сразу справа от b1:
    - b2 начинается правее конца b1 (или слегка перекрывает);
    - вертикальные центры не сильно различаются;
    - высоты сопоставимы.
    """
    x1, y1, x2, y2 = bbox_to_rect(b1["bbox"])
    X1, Y1, X2, Y2 = bbox_to_rect(b2["bbox"])

    h1 = y2 - y1
    h2 = Y2 - Y1
    if h1 <= 0 or h2 <= 0:
        return False

    # Разница высот не больше 50%
    if abs(h1 - h2) / max(h1, h2) > 0.5:
        return False

    # b2 слева от b1 — не подходит
    if X1 < x1:
        return False

    # Горизонтальный зазор не больше 3 высот
    gap = X1 - x2
    if gap > max_gap_ratio * max(h1, h2):
        return False

    # Вертикальный сдвиг — не больше половины высоты
    cy1 = (y1 + y2) / 2
    cy2 = (Y1 + Y2) / 2
    if abs(cy1 - cy2) > 0.5 * max(h1, h2):
        return False

    return True


def try_combine_geometric(blocks: list[dict]) -> list[str]:
    """
    Склеивает блоки с учётом геометрии.

    Стратегия:
      1. Одиночный блок — попытка прочитать как полный номер.
      2. Пара соседних блоков: [номер][регион].
      3. Тройка соседних: [часть1][часть2][часть3].
    """
    candidates = []

    # Одиночные
    for b in blocks:
        n = fix_letter_digit_confusion(normalize(b["text"]))
        if is_valid_plate(n):
            candidates.append(n)

    # Пары
    for i, b1 in enumerate(blocks):
        for j, b2 in enumerate(blocks):
            if i == j:
                continue
            if not blocks_are_neighbors(b1, b2):
                continue
            combined = normalize(b1["text"]) + normalize(b2["text"])
            combined = fix_letter_digit_confusion(combined)
            if is_valid_plate(combined):
                candidates.append(combined)

    # Тройки (редко, но бывает: "А12", "3ВС", "77")
    for i, b1 in enumerate(blocks):
        for j, b2 in enumerate(blocks):
            if j == i:
                continue
            if not blocks_are_neighbors(b1, b2):
                continue
            for k, b3 in enumerate(blocks):
                if k in (i, j):
                    continue
                if not blocks_are_neighbors(b2, b3):
                    continue
                combined = normalize(b1["text"]) + normalize(b2["text"]) + normalize(b3["text"])
                combined = fix_letter_digit_confusion(combined)
                if is_valid_plate(combined):
                    candidates.append(combined)

    # Уникальные с сохранением порядка
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def main() -> int:
    test_dir = Path(__file__).parent / "test_data"
    if not test_dir.exists():
        print(f"Папка {test_dir} не найдена.")
        return 1

    images = sorted(
        list(test_dir.glob("*.jpg"))
        + list(test_dir.glob("*.jpeg"))
        + list(test_dir.glob("*.png"))
    )
    if not images:
        print(f"В папке {test_dir} нет картинок.")
        return 1

    print(f"Найдено картинок: {len(images)}")
    print("=" * 70)
    print("Инициализация RapidOCR...")
    t0 = time.time()
    ocr = RapidOCR()
    print(f"OK за {time.time() - t0:.1f} сек")
    print("=" * 70)

    total_plates = 0
    for img_path in images:
        print(f"\nФайл: {img_path.name}")
        try:
            t0 = time.time()
            result, _ = ocr(str(img_path))
            dt = time.time() - t0
        except Exception as e:
            print(f"  ОШИБКА: {e}")
            continue

        if not result:
            print("  Ничего не распознано")
            continue

        print(f"  Блоков: {len(result)}  (за {dt*1000:.0f} мс)")

        blocks = []
        for i, item in enumerate(result):
            bbox, text, conf = item
            norm = normalize(text)
            is_garbage = (
                any(norm.startswith(p) for p in GARBAGE_PREFIXES)
                or conf < 0.65
            )
            marker = "  × мусор" if is_garbage else ""
            print(f"    [{i}] raw='{text}'  conf={conf:.3f}{marker}")
            if not is_garbage:
                blocks.append({"bbox": bbox, "text": text, "conf": conf})

        candidates = try_combine_geometric(blocks)
        if candidates:
            print(f"  🎯 НОМЕРА: {', '.join(candidates)}")
            total_plates += len(candidates)
        else:
            remaining = [b["text"] for b in blocks]
            print(f"  ❌ Номер не собран из: {remaining}")

    print()
    print("=" * 70)
    print(f"ИТОГ: распознано номеров — {total_plates}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())