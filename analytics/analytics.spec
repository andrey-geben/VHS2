# -*- mode: python ; coding: utf-8 -*-
"""
analytics.spec — конфигурация сборки PyInstaller для VHS-Analytics.

Использование:
    pyinstaller analytics.spec --clean

Результат:
    dist/VHS-Analytics/VHS-Analytics.exe
    dist/VHS-Analytics/_internal/       (зависимости, DLL, данные)
    dist/VHS-Analytics/config.yaml      (копия конфига)
    dist/VHS-Analytics/models/          (YOLO-модель для mode=yolo)
    dist/VHS-Analytics/snapshots/       (пустая папка для снапшотов)

Особенности ALPR:
- ONNX-модель детектора номеров (open_image_models) скачивается
  в %USERPROFILE%\\.cache\\open-image-models\\ при ПЕРВОМ запуске.
  Она НЕ упаковывается в exe — так exe остаётся компактным.
- ONNX-модели RapidOCR уже включены в сам pip-пакет и упаковываются
  автоматически через collect_all.
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None


# ---------------------------------------------------------------------- #
#  Тяжёлые библиотеки — собираем всё содержимое пакетов
# ---------------------------------------------------------------------- #
print("[spec] Собираю зависимости...")

# OpenCV
cv2_datas, cv2_binaries, cv2_hiddenimports = collect_all("cv2")
print(f"[spec] cv2: {len(cv2_datas)} datas, {len(cv2_binaries)} binaries")

# Pillow (для pystray)
pil_datas, pil_binaries, pil_hiddenimports = collect_all("PIL")

# ONNX Runtime — критично, содержит DLL для инференса
onnx_datas, onnx_binaries, onnx_hiddenimports = collect_all("onnxruntime")
print(f"[spec] onnxruntime: {len(onnx_binaries)} binaries")

# RapidOCR — внутри пакета лежат ONNX-модели
rapidocr_datas, rapidocr_binaries, rapidocr_hiddenimports = collect_all(
    "rapidocr_onnxruntime"
)
print(f"[spec] rapidocr: {len(rapidocr_datas)} datas")

# open_image_models — детектор номеров
oim_datas, oim_binaries, oim_hiddenimports = collect_all("open_image_models")
print(f"[spec] open_image_models: {len(oim_datas)} datas")

# fast_alpr — обёртка (может импортироваться динамически)
try:
    fast_alpr_datas, fast_alpr_binaries, fast_alpr_hiddenimports = collect_all(
        "fast_alpr"
    )
except Exception:
    fast_alpr_datas = fast_alpr_binaries = fast_alpr_hiddenimports = []

# YAML
yaml_datas, yaml_binaries, yaml_hiddenimports = collect_all("yaml")


# ---------------------------------------------------------------------- #
#  Иконка
# ---------------------------------------------------------------------- #
icon_file = "icon.ico" if Path("icon.ico").exists() else None


# ---------------------------------------------------------------------- #
#  Анализ
# ---------------------------------------------------------------------- #
a = Analysis(
    ["analytics_app.py"],
    pathex=[],
    binaries=(
        [] + cv2_binaries + pil_binaries + onnx_binaries
        + rapidocr_binaries + oim_binaries + yaml_binaries
        + fast_alpr_binaries
    ),
    datas=[
        # Шаблоны и статика — попадут в _MEIPASS
        ("templates", "templates"),
        ("static", "static"),
    ] + cv2_datas + pil_datas + onnx_datas + rapidocr_datas + oim_datas + yaml_datas + fast_alpr_datas,
    hiddenimports=[
        # Flask и окружение
        "flask",
        "flask_login",
        "werkzeug",
        "jinja2",
        "jinja2.ext",
        "itsdangerous",
        "click",
        "blinker",

        # NumPy — динамические импорты (важно для NumPy 2.3+)
        "numpy",
        "numpy._core",
        "numpy._core._exceptions",
        "numpy._core._multiarray_umath",
        "numpy._core._multiarray_tests",
        "numpy._core._dtype_ctypes",
        "numpy.core._multiarray_umath",
        "numpy.core._multiarray_tests",
        "numpy.lib.format",

        # YAML
        "yaml",
        "yaml.cyaml",

        # Requests
        "requests",
        "urllib3",
        "charset_normalizer",
        "idna",
        "certifi",

        # SQLite
        "sqlite3",
        "sqlite3.dbapi2",

        # Трей
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",

        # ONNX Runtime
        "onnxruntime",
        "onnxruntime.capi",
        "onnxruntime.capi._pybind_state",

        # RapidOCR — динамические импорты
        "rapidocr_onnxruntime",
        "rapidocr_onnxruntime.main",
        "rapidocr_onnxruntime.ch_ppocr_v3_det",
        "rapidocr_onnxruntime.ch_ppocr_v3_rec",
        "rapidocr_onnxruntime.ch_ppocr_v3_cls",

        # open_image_models — детектор номеров
        "open_image_models",
        "open_image_models.detection",
        "open_image_models.detection.core",
        "open_image_models.detection.core.yolo_v9",
        "open_image_models.detection.core.yolo_v9.inference",

        # Наши модули
        "capture",
        "detector",
        "detector_yolo",
        "alpr",
        "engine",
        "database",
        "auth",
        "tray",
        "vhs_client",
    ] + cv2_hiddenimports + pil_hiddenimports + onnx_hiddenimports + rapidocr_hiddenimports + oim_hiddenimports + yaml_hiddenimports + fast_alpr_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Исключаем всё, что не нужно
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "unittest",
        "pydoc",
        "doctest",
        # ML — не нужно в runtime
        "torch",
        "torchvision",
        "tensorflow",
        "ultralytics",
        "polars",
        "sympy",
        "networkx",
        "httpx",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# ---------------------------------------------------------------------- #
#  EXE
# ---------------------------------------------------------------------- #
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VHS-Analytics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)


# ---------------------------------------------------------------------- #
#  COLLECT
# ---------------------------------------------------------------------- #
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VHS-Analytics",
)