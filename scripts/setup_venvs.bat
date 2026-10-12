@echo off
REM Создаёт venv в core и analytics на Python 3.12
cd /d "%~dp0.."
echo [1/2] Создаю venv для core...
cd core
py -3.12 -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install --no-user -r requirements.txt
deactivate
cd ..

echo [2/2] Создаю venv для analytics...
cd analytics
py -3.12 -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install --no-user -r requirements.txt
deactivate
cd ..

echo Готово. Теперь откройте VHS2 в PyCharm.
pause
