@echo off
REM ==================================================================== #
REM  run.bat — быстрый запуск VHS Core из исходников (для разработки)
REM ==================================================================== #
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

python app.py
pause