@echo off
REM ==================================================================== #
REM  make_deploy.bat — собирает чистый дистрибутив для Linux в VHS2-deploy/
REM ==================================================================== #
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set DEPLOY=VHS2-deploy

echo.
echo ============================================================
echo   Сборка дистрибутива для Linux
echo ============================================================
echo.

if exist "%DEPLOY%" (
    echo [!] Папка %DEPLOY% уже существует.
    choice /C YN /M "Удалить её и создать заново"
    if errorlevel 2 (
        echo Отменено.
        pause
        exit /b 1
    )
    rmdir /S /Q "%DEPLOY%"
)

mkdir "%DEPLOY%"
mkdir "%DEPLOY%\core"
mkdir "%DEPLOY%\analytics"

REM --- core ---
echo [1/2] Копирую core...
robocopy "core" "%DEPLOY%\core" /E /NFL /NDL /NJH /NJS /NP ^
    /XD ".venv" "venv" "build" "dist" "dist_old" "__pycache__" ^
        ".idea" "recordings" ".mypy_cache" ".pytest_cache" ^
    /XF "*.pyc" "*.pyo" "*.db" "*.log" "VHS.spec" "build.bat" "run.bat" ^
        "icon.ico" >NUL

REM --- analytics ---
echo [2/2] Копирую analytics...
robocopy "analytics" "%DEPLOY%\analytics" /E /NFL /NDL /NJH /NJS /NP ^
    /XD ".venv" "venv" "build" "dist" "dist_old" "__pycache__" ^
        ".idea" "snapshots" "test_data" ".mypy_cache" ".pytest_cache" ^
        "tools" ^
    /XF "*.pyc" "*.pyo" "*.db" "*.log" "analytics.spec" "build.bat" "run.bat" ^
        "icon.ico" "test_*.py" "debug_*.jpg" "alpr_*.jpg" "fastalpr_*.jpg" ^
        "yolov8n.pt" >NUL

REM --- install.sh / uninstall.sh ---
if exist "install.sh" (
    copy /Y "install.sh" "%DEPLOY%\install.sh" >nul
) else (
    echo [!] ВНИМАНИЕ: install.sh не найден в корне проекта!
)

if exist "uninstall.sh" (
    copy /Y "uninstall.sh" "%DEPLOY%\uninstall.sh" >nul
) else (
    echo [!] ВНИМАНИЕ: uninstall.sh не найден в корне проекта!
)

echo.
echo ============================================================
echo   ГОТОВО: %CD%\%DEPLOY%\
echo ============================================================
echo.
echo   Размер:
dir "%DEPLOY%" | find "File(s)"
echo.
echo   Следующий шаг — скопировать на сервер:
echo     scp -r VHS2-deploy user@server:/tmp/vhs/
echo.

choice /C YN /M "Открыть папку в Проводнике"
if errorlevel 2 goto :end
if errorlevel 1 explorer "%DEPLOY%"

:end
endlocal
pause
exit /b 0