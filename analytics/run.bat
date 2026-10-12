@echo off
REM ==================================================================== #
REM  run.bat — быстрый запуск VHS Analytics из исходников
REM ==================================================================== #
REM  Активирует .venv (если есть) и запускает analytics_app.py.
REM  Работает и при двойном клике из Проводника, и из терминала.
REM ==================================================================== #

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   VHS Analytics — запуск
echo ============================================================
echo.

REM -------------------------------------------------------------------- #
REM  Проверка файла
REM -------------------------------------------------------------------- #
if not exist "analytics_app.py" (
    echo [ОШИБКА] Не найден analytics_app.py в:
    echo         %CD%
    echo         Запустите run.bat из папки analytics.
    pause
    exit /b 1
)

REM -------------------------------------------------------------------- #
REM  Активация venv
REM -------------------------------------------------------------------- #
if exist ".venv\Scripts\activate.bat" (
    echo [i] Активируем .venv
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo [i] Активируем venv
    call "venv\Scripts\activate.bat"
) else (
    echo [!] Виртуальное окружение не найдено.
    echo     Запуск пойдёт из системного Python.
    choice /C YN /M "Продолжить"
    if errorlevel 2 (
        echo Отменено.
        pause
        exit /b 1
    )
)
echo.

REM -------------------------------------------------------------------- #
REM  Проверка, что VHS Core доступен
REM -------------------------------------------------------------------- #
echo [i] Проверка VHS Core на http://localhost:8000 ...
curl -s -m 3 -o NUL -w "%%{http_code}" http://localhost:8000/api/ping >NUL 2>&1
if errorlevel 1 (
    echo [!] VHS Core, похоже, не запущен или недоступен.
    echo     Запустите его в отдельном окне: scripts\run_core.bat
    echo.
    choice /C YN /M "Продолжить запуск Analytics"
    if errorlevel 2 (
        echo Отменено.
        pause
        exit /b 1
    )
) else (
    echo [OK] VHS Core отвечает.
)
echo.

REM -------------------------------------------------------------------- #
REM  Запуск
REM -------------------------------------------------------------------- #
echo [i] Запускаю VHS Analytics...
echo     Веб-интерфейс: http://localhost:8100
echo     Логи: analytics.log
echo     Для остановки нажмите Ctrl+C.
echo.

python analytics_app.py

echo.
echo [i] Analytics завершён.
pause