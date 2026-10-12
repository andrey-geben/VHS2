@echo off
REM Сборка обоих exe
cd /d "%~dp0.."
echo ================================================
echo   Сборка VHS Core
echo ================================================
cd core
call build.bat
cd ..
echo.
echo ================================================
echo   Сборка VHS Analytics
echo ================================================
cd analytics
call build.bat
cd ..
echo.
echo Готово. Результаты в core\dist и analytics\dist.
pause
