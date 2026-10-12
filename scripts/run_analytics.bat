@echo off
REM Запуск VHS Analytics
cd /d "%~dp0..\analytics"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python analytics_app.py
pause
