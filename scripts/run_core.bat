@echo off
REM Запуск VHS Core
cd /d "%~dp0..\core"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python app.py
pause
