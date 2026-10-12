@echo off
REM ==================================================================== #
REM  build_all.bat — собирает оба exe и складывает в одну папку
REM ==================================================================== #
REM  Результат:
REM    VHS-deploy\
REM    ├── core\
REM    │   ├── VHS.exe
REM    │   ├── _internal\
REM    │   ├── config.yaml
REM    │   └── recordings\
REM    ├── analytics\
REM    │   ├── VHS-Analytics.exe
REM    │   ├── _internal\
REM    │   ├── config.yaml
REM    │   ├── models\
REM    │   └── snapshots\
REM    ├── START.bat
REM    └── README.txt
REM ==================================================================== #

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   VHS2 — полная сборка
echo ============================================================
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 1. Сборка Core
REM -------------------------------------------------------------------- #
echo [1/4] Сборка VHS Core...
cd core
if not exist "build.bat" (
    echo [ОШИБКА] Не найден core\build.bat
    pause
    exit /b 1
)
call build.bat nopause
if errorlevel 1 (
    echo [ОШИБКА] Сборка Core не удалась
    pause
    exit /b 1
)
cd ..
echo [OK] Core собран.
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 2. Сборка Analytics
REM -------------------------------------------------------------------- #
echo [2/4] Сборка VHS Analytics...
cd analytics
if not exist "build.bat" (
    echo [ОШИБКА] Не найден analytics\build.bat
    pause
    exit /b 1
)
call build.bat nopause
if errorlevel 1 (
    echo [ОШИБКА] Сборка Analytics не удалась
    pause
    exit /b 1
)
cd ..
echo [OK] Analytics собран.
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 3. Складываем в общую папку VHS-deploy
REM -------------------------------------------------------------------- #
echo [3/4] Формирую VHS-deploy\...

if exist "VHS-deploy" rmdir /S /Q "VHS-deploy"
mkdir "VHS-deploy"
mkdir "VHS-deploy\core"
mkdir "VHS-deploy\analytics"

REM --- Core ---
if exist "core\dist\VHS\*" (
    xcopy /E /I /Y /Q "core\dist\VHS\*" "VHS-deploy\core\" >NUL
    echo [OK] Core скопирован в VHS-deploy\core\
) else (
    echo [ОШИБКА] Не найдена папка core\dist\VHS\
    pause
    exit /b 1
)

REM --- Analytics ---
if exist "analytics\dist\VHS-Analytics\*" (
    xcopy /E /I /Y /Q "analytics\dist\VHS-Analytics\*" "VHS-deploy\analytics\" >NUL
    echo [OK] Analytics скопирован в VHS-deploy\analytics\
) else (
    echo [ОШИБКА] Не найдена папка analytics\dist\VHS-Analytics\
    pause
    exit /b 1
)

REM --- Создаём папку для записей ---
if not exist "VHS-deploy\core\recordings" (
    mkdir "VHS-deploy\core\recordings"
)

REM --- Создаём папку для снапшотов ---
if not exist "VHS-deploy\analytics\snapshots" (
    mkdir "VHS-deploy\analytics\snapshots"
)
echo.

REM -------------------------------------------------------------------- #
REM  Шаг 4. START.bat и README
REM -------------------------------------------------------------------- #
echo [4/4] Создаю START.bat и README.txt...

(
echo @echo off
echo REM Запуск VHS Core + VHS Analytics
echo cd /d "%%~dp0core"
echo start "VHS Core" cmd /c "VHS.exe"
echo timeout /T 3 /NOBREAK ^>NUL
echo cd /d "%%~dp0analytics"
echo start "VHS Analytics" cmd /c "VHS-Analytics.exe"
echo echo.
echo echo Запущено.
echo echo Core:      http://localhost:8000
echo echo Analytics: http://localhost:8100
echo echo Логин: admin / admin
echo pause
) > "VHS-deploy\START.bat"

(
echo ============================================================
echo   VHS2 — сервер видеонаблюдения
echo ============================================================
echo.
echo Структура:
echo.
echo   core\VHS.exe               - основной сервер (запись, архив, UI)
echo   core\config.yaml           - настройки Core
echo   core\recordings\           - архив записей
echo.
echo   analytics\VHS-Analytics.exe - модуль аналитики (AI, ALPR)
echo   analytics\config.yaml      - настройки Analytics
echo   analytics\models\          - ONNX-модели
echo   analytics\snapshots\       - снапшоты событий
echo.
echo Запуск:
echo.
echo   1. Двойной клик по START.bat
echo   2. Откройте http://localhost:8000
echo   3. Логин: admin / admin
echo.
echo ВАЖНО: смените пароль admin после первого входа!
echo.
echo Первый запуск Analytics:
echo   Модель детектора номеров скачается автоматически
echo   в %%USERPROFILE%%\.cache\open-image-models\ (~8 МБ).
echo   Нужен интернет один раз.
echo.
echo Управление Analytics:
echo   http://localhost:8000/settings -^> вкладка "Аналитика"
echo.
) > "VHS-deploy\README.txt"

echo [OK] START.bat и README.txt созданы.
echo.

REM -------------------------------------------------------------------- #
REM  Итог
REM -------------------------------------------------------------------- #
echo ============================================================
echo   ГОТОВО
echo ============================================================
echo.
echo   Дистрибутив: %CD%\VHS-deploy\
echo.
echo   Запуск:    VHS-deploy\START.bat
echo   Core:      VHS-deploy\core\VHS.exe
echo   Analytics: VHS-deploy\analytics\VHS-Analytics.exe
echo.
echo   ВАЖНО: в VHS-deploy\core\config.yaml проверьте:
echo     - server.secret_key       (смените!)
echo     - auth.default_password   (смените!)
echo     - analytics.api_token     (должен совпадать с Analytics)
echo     - analytics.exe_path      должен быть:
echo         ..\analytics\VHS-Analytics.exe
echo.
echo   В VHS-deploy\analytics\config.yaml проверьте:
echo     - vhs_core.api_token      (совпадает с Core)
echo     - vhs_core.base_url       = http://localhost:8000
echo.

choice /C YN /M "Открыть папку VHS-deploy в Проводнике"
if errorlevel 2 goto :end
if errorlevel 1 explorer "%CD%\VHS-deploy"

:end
endlocal
pause
exit /b 0