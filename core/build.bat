@echo off
REM ==================================================================== #
REM  build.bat — сборка VHS Core в exe через PyInstaller
REM ==================================================================== #
REM  Что делает:
REM    1. Проверяет, что мы в корне проекта (есть app.py и VHS.spec).
REM    2. Активирует .venv (если есть).
REM    3. Проверяет Python, ключевые модули и PyInstaller.
REM    4. Завершает запущенный VHS.exe (если работает).
REM    5. Чистит build/ и dist/.
REM    6. Собирает через pyinstaller VHS.spec --clean.
REM    7. Копирует config.yaml, icon.ico, создаёт recordings/.
REM    8. Печатает итоговый путь.
REM ==================================================================== #

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   VHS Core — сборка exe через PyInstaller
echo ============================================================
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 1. Проверка корня проекта
REM -------------------------------------------------------------------- #
if not exist "app.py" (
    echo [ОШИБКА] Не найден app.py в текущей папке:
    echo         %CD%
    pause
    exit /b 1
)
if not exist "VHS.spec" (
    echo [ОШИБКА] Не найден VHS.spec в текущей папке.
    pause
    exit /b 1
)
echo [OK] Работаем в: %CD%
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 2. Активация venv
REM -------------------------------------------------------------------- #
if exist ".venv\Scripts\activate.bat" (
    echo [i] Активируем .venv\Scripts\activate.bat
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo [i] Активируем venv\Scripts\activate.bat
    call "venv\Scripts\activate.bat"
) else (
    echo [!] Виртуальное окружение не найдено.
    choice /C YN /M "Продолжить с системным Python"
    if errorlevel 2 (
        echo Отменено пользователем.
        pause
        exit /b 1
    )
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 3. Проверка Python, модулей и PyInstaller
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
python -c "import yaml, cv2, flask, numpy, requests, PIL, pystray" >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Не все ключевые модули установлены:
    echo         yaml, cv2, flask, numpy, requests, PIL, pystray
    echo.
    echo Установите их командой:
    echo     python -m pip install --no-user -r requirements.txt
    pause
    exit /b 1
)
python -c "import yaml, cv2, flask, numpy, requests, PIL, pystray; print('OK: все ключевые модули доступны')"
echo.

echo [i] Проверка PyInstaller...
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [i] PyInstaller не установлен — устанавливаем...
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
REM  Шаг 4. Завершение запущенного VHS.exe
REM -------------------------------------------------------------------- #
echo [i] Проверка запущенного VHS.exe...
tasklist /FI "IMAGENAME eq VHS.exe" 2>NUL | find /I "VHS.exe" >NUL
if not errorlevel 1 (
    echo [!] Найден запущенный VHS.exe — завершаю...
    taskkill /F /IM VHS.exe >NUL 2>&1
    timeout /T 2 /NOBREAK >NUL
    echo [OK] Процесс завершён.
) else (
    echo [i] Запущенных процессов не найдено.
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 5. Чистка
REM -------------------------------------------------------------------- #
echo [i] Удаляем старые папки build\ и dist\...
if exist "build" rmdir /S /Q "build"
if exist "dist"  rmdir /S /Q "dist"

if exist "dist" (
    echo [!] Не удалось удалить dist — переименовываю в dist_old
    if exist "dist_old" rmdir /S /Q "dist_old" >NUL 2>&1
    ren "dist" "dist_old"
    if exist "dist" (
        echo [ОШИБКА] Не удалось ни удалить, ни переименовать dist.
        echo         Закройте VHS.exe вручную и повторите.
        pause
        exit /b 1
    )
)
echo [OK] Очищено.
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 6. Сборка
REM -------------------------------------------------------------------- #
echo [i] Запуск сборки (может занять 1–3 минуты)...
echo.
python -m PyInstaller VHS.spec --clean --noconfirm
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
REM  Шаг 7. Копирование данных
REM -------------------------------------------------------------------- #
if not exist "dist\VHS" (
    echo [ОШИБКА] Не найдена папка dist\VHS после сборки.
    pause
    exit /b 1
)

REM --- config.yaml ---
if exist "config.yaml" (
    if not exist "dist\VHS\config.yaml" (
        copy /Y "config.yaml" "dist\VHS\config.yaml" >nul
        echo [OK] Скопирован config.yaml
    ) else (
        echo [i] dist\VHS\config.yaml уже существует — не перезаписываем.
    )
)

REM --- icon.ico ---
if exist "icon.ico" (
    copy /Y "icon.ico" "dist\VHS\icon.ico" >nul
    echo [OK] Скопирована иконка icon.ico
)

REM --- recordings\ ---
if not exist "dist\VHS\recordings" (
    mkdir "dist\VHS\recordings"
    echo [OK] Создана папка dist\VHS\recordings\
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 8. Итог
REM -------------------------------------------------------------------- #
echo ============================================================
echo   ГОТОВО
echo ============================================================
echo.
echo   Папка сборки: %CD%\dist\VHS\
echo   Запуск:       dist\VHS\VHS.exe
echo.
echo   Перед первым запуском проверьте dist\VHS\config.yaml:
echo     - server.secret_key           (смените!)
echo     - auth.default_password       (смените!)
echo     - analytics.api_token         (должен совпадать с Analytics)
echo     - cameras                     (задайте свои)
echo.
echo   Если Windows Defender ругается — добавьте dist\VHS\
echo   в исключения (это ложное срабатывание PyInstaller).
echo.

choice /C YN /M "Открыть папку dist\VHS в Проводнике"
if errorlevel 2 goto :end
if errorlevel 1 explorer "%CD%\dist\VHS"

:end
endlocal
pause
exit /b 0