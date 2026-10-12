@echo off
REM ==================================================================== #
REM  build.bat — сборка VHS Analytics в exe через PyInstaller
REM ==================================================================== #
REM  Что делает:
REM    1. Проверяет корень проекта (analytics_app.py + analytics.spec).
REM    2. Активирует .venv.
REM    3. Проверяет Python, ключевые модули, PyInstaller.
REM    4. Проверяет наличие YOLO-модели (для mode=yolo).
REM    5. Завершает запущенный VHS-Analytics.exe.
REM    6. Чистит build/ и dist/.
REM    7. Собирает через pyinstaller analytics.spec --clean.
REM    8. Копирует config.yaml, icon.ico, models/, создаёт snapshots/.
REM    9. Печатает итог.
REM ==================================================================== #

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   VHS Analytics — сборка exe через PyInstaller
echo ============================================================
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 1. Проверка корня проекта
REM -------------------------------------------------------------------- #
if not exist "analytics_app.py" (
    echo [ОШИБКА] Не найден analytics_app.py в:
    echo         %CD%
    pause
    exit /b 1
)
if not exist "analytics.spec" (
    echo [ОШИБКА] Не найден analytics.spec.
    pause
    exit /b 1
)
echo [OK] Работаем в: %CD%
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 2. Активация venv
REM -------------------------------------------------------------------- #
if exist ".venv\Scripts\activate.bat" (
    echo [i] Активируем .venv
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo [i] Активируем venv
    call "venv\Scripts\activate.bat"
) else (
    echo [!] Виртуальное окружение не найдено.
    choice /C YN /M "Продолжить с системным Python"
    if errorlevel 2 (
        echo Отменено.
        pause
        exit /b 1
    )
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 3. Проверка Python и ключевых модулей
REM -------------------------------------------------------------------- #
echo [i] Проверка Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH.
    pause
    exit /b 1
)
python --version
echo.

echo [i] Проверка ключевых модулей в venv...
python -c "import yaml, cv2, flask, numpy, requests, PIL" >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Не все базовые модули установлены:
    echo         yaml, cv2, flask, numpy, requests, PIL
    echo.
    echo Установите их:
    echo     python -m pip install --no-user -r requirements.txt
    pause
    exit /b 1
)
echo [OK] Базовые модули на месте.

echo [i] Проверка модулей ALPR (onnxruntime, rapidocr, open_image_models)...
python -c "import onnxruntime, rapidocr_onnxruntime; from open_image_models import LicensePlateDetector" >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Модули ALPR не установлены:
    echo         onnxruntime, rapidocr_onnxruntime, open_image_models
    echo.
    echo Установите их:
    echo     python -m pip install --no-user "fast-alpr[onnx]" rapidocr-onnxruntime
    pause
    exit /b 1
)
echo [OK] Модули ALPR на месте.
echo.

echo [i] Проверка PyInstaller...
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [i] PyInstaller не установлен — устанавливаю...
    python -m pip install --upgrade pip
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить PyInstaller.
        pause
        exit /b 1
    )
)
python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 4. Проверка YOLO-модели (только для mode=yolo)
REM -------------------------------------------------------------------- #
if not exist "models\yolov8n.onnx" (
    echo [!] ВНИМАНИЕ: models\yolov8n.onnx не найдена.
    echo     Это нормально, если используется mode: alpr или mode: motion.
    echo     Для режима yolo модель обязательна:
    echo         python tools\export_yolo.py --model yolov8n
    echo.
) else (
    echo [OK] YOLO-модель: models\yolov8n.onnx
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 5. Завершение запущенного exe
REM -------------------------------------------------------------------- #
echo [i] Проверка запущенного VHS-Analytics.exe...
tasklist /FI "IMAGENAME eq VHS-Analytics.exe" 2>NUL | find /I "VHS-Analytics.exe" >NUL
if not errorlevel 1 (
    echo [!] Найден запущенный VHS-Analytics.exe — завершаю...
    taskkill /F /IM VHS-Analytics.exe >NUL 2>&1
    timeout /T 2 /NOBREAK >NUL
    echo [OK] Процесс завершён.
) else (
    echo [i] Запущенных процессов не найдено.
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 6. Чистка
REM -------------------------------------------------------------------- #
echo [i] Удаляю старые папки build\ и dist\...
if exist "build" rmdir /S /Q "build"
if exist "dist"  rmdir /S /Q "dist"

if exist "dist" (
    echo [!] Не удалось удалить dist — переименовываю в dist_old
    if exist "dist_old" rmdir /S /Q "dist_old" >NUL 2>&1
    ren "dist" "dist_old"
    if exist "dist" (
        echo [ОШИБКА] Не удалось ни удалить, ни переименовать dist.
        echo         Закройте VHS-Analytics.exe вручную и повторите.
        pause
        exit /b 1
    )
)
echo [OK] Очищено.
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 7. Сборка
REM -------------------------------------------------------------------- #
echo [i] Запуск сборки (может занять 2-4 минуты из-за больших зависимостей)...
echo.
python -m PyInstaller analytics.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Сборка не удалась. Смотрите вывод PyInstaller выше.
    pause
    exit /b 1
)
echo.
echo [OK] Сборка завершена.
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 8. Копирование данных
REM -------------------------------------------------------------------- #
if not exist "dist\VHS-Analytics" (
    echo [ОШИБКА] Не найдена папка dist\VHS-Analytics после сборки.
    pause
    exit /b 1
)

REM --- config.yaml ---
if exist "config.yaml" (
    if not exist "dist\VHS-Analytics\config.yaml" (
        copy /Y "config.yaml" "dist\VHS-Analytics\config.yaml" >nul
        echo [OK] Скопирован config.yaml
    ) else (
        echo [i] dist\VHS-Analytics\config.yaml уже существует — не перезаписываю.
    )
)

REM --- icon.ico ---
if exist "icon.ico" (
    copy /Y "icon.ico" "dist\VHS-Analytics\icon.ico" >nul
    echo [OK] Скопирована иконка icon.ico
)

REM --- models\ (для YOLO, опционально) ---
if exist "models" (
    if not exist "dist\VHS-Analytics\models" (
        mkdir "dist\VHS-Analytics\models"
    )
    xcopy /Y /Q "models\*.onnx" "dist\VHS-Analytics\models\" >nul 2>&1
    if errorlevel 1 (
        echo [!] Не удалось скопировать ONNX-модели из models\
    ) else (
        echo [OK] Скопированы ONNX-модели из models\
    )
)

REM --- snapshots\ ---
if not exist "dist\VHS-Analytics\snapshots" (
    mkdir "dist\VHS-Analytics\snapshots"
    echo [OK] Создана папка dist\VHS-Analytics\snapshots\
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 9. Итог
REM -------------------------------------------------------------------- #
echo ============================================================
echo   ГОТОВО
echo ============================================================
echo.
echo   Папка сборки: %CD%\dist\VHS-Analytics\
echo   Запуск:       dist\VHS-Analytics\VHS-Analytics.exe
echo.
echo   ПЕРВЫЙ ЗАПУСК ALPR:
echo     При первом запуске модели ONNX детектора номеров
echo     скачаются автоматически в:
echo       %%USERPROFILE%%\.cache\open-image-models\
echo     Нужен доступ в интернет (одноразово, ~8 МБ).
echo     Дальше — работает без сети.
echo.
echo   VHS Core должен быть запущен, иначе Analytics не увидит поток.
echo.
echo   Если Windows Defender ругается — добавьте dist\VHS-Analytics\
echo   в исключения (ложное срабатывание PyInstaller).
echo.

choice /C YN /M "Открыть папку dist\VHS-Analytics в Проводнике"
if errorlevel 2 goto :end
if errorlevel 1 explorer "%CD%\dist\VHS-Analytics"

:end
endlocal
pause
exit /b 0