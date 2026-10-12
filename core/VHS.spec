# -*- mode: python ; coding: utf-8 -*-
"""
VHS.spec — конфигурация сборки PyInstaller для VHS Core.

Использование:
    pyinstaller VHS.spec --clean

Результат:
    dist/VHS/VHS.exe                — запускаемый файл
    dist/VHS/_internal/             — зависимости, DLL, ресурсы
    dist/VHS/config.yaml            — копия конфига
    dist/VHS/recordings/            — папка для записей
    dist/VHS/icon.ico               — иконка (если была)

Особенности:
- --onedir (не --onefile): быстрее старт, проще отладка.
- UPX отключён: снижает ложные срабатывания антивирусов.
- console=True: оставляем консоль для логов (потом можно False).
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None


# ---------------------------------------------------------------------- #
#  Тяжёлые библиотеки с динамическими импортами
# ---------------------------------------------------------------------- #
cv2_datas, cv2_binaries, cv2_hiddenimports = collect_all("cv2")
pil_datas, pil_binaries, pil_hiddenimports = collect_all("PIL")


# ---------------------------------------------------------------------- #
#  ONVIF / zeep — опционально
# ---------------------------------------------------------------------- #
try:
    zeep_datas, zeep_binaries, zeep_hiddenimports = collect_all("zeep")
    onvif_datas, onvif_binaries, onvif_hiddenimports = collect_all("onvif")
    lxml_datas, lxml_binaries, lxml_hiddenimports = collect_all("lxml")
except Exception:
    zeep_datas = zeep_binaries = zeep_hiddenimports = []
    onvif_datas = onvif_binaries = onvif_hiddenimports = []
    lxml_datas = lxml_binaries = lxml_hiddenimports = []


# ---------------------------------------------------------------------- #
#  Иконка (если лежит рядом со spec)
# ---------------------------------------------------------------------- #
icon_file = "icon.ico" if Path("icon.ico").exists() else None


# ---------------------------------------------------------------------- #
#  Анализ
# ---------------------------------------------------------------------- #
a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[] + cv2_binaries + pil_binaries + zeep_binaries + onvif_binaries + lxml_binaries,
    datas=[
        # Шаблоны и статика — попадут во временную папку _MEIPASS
        ("templates", "templates"),
        ("static", "static"),
    ] + cv2_datas + pil_datas + zeep_datas + onvif_datas + lxml_datas,
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

        # NumPy — динамические импорты
        "numpy",
        "numpy.core._multiarray_umath",
        "numpy.core._multiarray_tests",

        # YAML
        "yaml",
        "yaml.cyaml",

        # Requests
        "requests",
        "requests_toolbelt",
        "urllib3",
        "charset_normalizer",
        "idna",
        "certifi",

        # Date/time
        "dateutil",
        "dateutil.parser",
        "dateutil.tz",

        # SQLite
        "sqlite3",
        "sqlite3.dbapi2",

        # Иконка в трее
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
    ] + cv2_hiddenimports + pil_hiddenimports + zeep_hiddenimports + onvif_hiddenimports + lxml_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Отсекаем лишнее — уменьшает размер
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
    name="VHS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # True для отладки; потом можно False
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
    name="VHS",
)