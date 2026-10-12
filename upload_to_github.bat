@echo off
REM ==================================================================== #
REM  upload_to_github.bat — загрузка VHS2 на GitHub
REM ==================================================================== #
REM  Первый запуск: полная настройка и загрузка.
REM  Повторные запуски: коммит изменений и push.
REM ==================================================================== #

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   VHS2 — загрузка на GitHub
echo ============================================================
echo.

REM -------------------------------------------------------------------- #
REM  Проверка Git
REM -------------------------------------------------------------------- #
git --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Git не установлен.
    echo.
    echo Скачайте и установите: https://git-scm.com/download/win
    echo При установке оставьте все настройки по умолчанию.
    pause
    exit /b 1
)
echo [OK] Git установлен.
git --version
echo.

REM -------------------------------------------------------------------- #
REM  Первичная настройка (если ещё не настроено)
REM -------------------------------------------------------------------- #
echo [i] Проверка настроек Git...
git config --global user.name >nul 2>&1
if errorlevel 1 (
    echo.
    echo === Первичная настройка Git ===
    echo.
    set /p GIT_NAME="Ваше имя для коммитов (например, Andrew Geben): "
    set /p GIT_EMAIL="Ваш email (тот же, что на GitHub): "
    git config --global user.name "!GIT_NAME!"
    git config --global user.email "!GIT_EMAIL!"
    git config --global init.defaultBranch main
    git config --global core.autocrlf true
    echo [OK] Настройки сохранены.
) else (
    echo [OK] Git уже настроен:
    git config --global user.name
    git config --global user.email
)
echo.

REM -------------------------------------------------------------------- #
REM  Инициализация репозитория
REM -------------------------------------------------------------------- #
if not exist ".git" (
    echo [i] Инициализация git-репозитория...
    git init
    git branch -M main
    echo [OK] Репозиторий инициализирован.
) else (
    echo [OK] Репозиторий уже инициализирован.
)
echo.

REM -------------------------------------------------------------------- #
REM  URL удалённого репозитория
REM -------------------------------------------------------------------- #
set REMOTE_URL=
for /f "delims=" %%i in ('git remote get-url origin 2^>nul') do set REMOTE_URL=%%i

if "!REMOTE_URL!"=="" (
    echo.
    echo === Настройка удалённого репозитория ===
    echo.
    echo Перед этим шагом вы должны создать пустой репозиторий на GitHub:
    echo   1. Откройте https://github.com/new
    echo   2. Введите имя: VHS2
    echo   3. НЕ ставьте галочки "Add README", "Add .gitignore"
    echo   4. Нажмите "Create repository"
    echo   5. Скопируйте URL — он будет вида:
    echo        https://github.com/ВАШ_ЛОГИН/VHS2.git
    echo.
    set /p REMOTE_URL="Вставьте URL репозитория: "

    if "!REMOTE_URL!"=="" (
        echo [ОШИБКА] URL не указан.
        pause
        exit /b 1
    )

    git remote add origin !REMOTE_URL!
    echo [OK] Remote добавлен: !REMOTE_URL!
) else (
    echo [OK] Remote уже настроен: !REMOTE_URL!
)
echo.

REM -------------------------------------------------------------------- #
REM  Создание .gitignore, если нет
REM -------------------------------------------------------------------- #
if not exist ".gitignore" (
    echo [i] Создаю .gitignore...
    (
        echo # Python
        echo __pycache__/
        echo *.py[cod]
        echo *.so
        echo .Python
        echo.
        echo # Virtual environments
        echo .venv/
        echo venv/
        echo env/
        echo.
        echo # PyInstaller
        echo build/
        echo dist/
        echo dist_old/
        echo *.manifest
        echo.
        echo # IDE
        echo .idea/
        echo .vscode/
        echo *.swp
        echo.
        echo # Данные приложения
        echo *.db
        echo *.log
        echo recordings/
        echo snapshots/
        echo.
        echo # Отладочные файлы
        echo debug_*.jpg
        echo alpr_*.jpg
        echo fastalpr_*.jpg
        echo test_data/
        echo.
        echo # OS
        echo Thumbs.db
        echo .DS_Store
    ) > .gitignore
    echo [OK] .gitignore создан.
) else (
    echo [OK] .gitignore уже существует.
)
echo.

REM -------------------------------------------------------------------- #
REM  Добавление файлов и коммит
REM -------------------------------------------------------------------- #
echo [i] Добавляю файлы в git...
git add .

echo [i] Проверка, есть ли что коммитить...
git diff --cached --quiet
if not errorlevel 1 (
    echo [i] Нет изменений для коммита.
) else (
    echo.
    set /p COMMIT_MSG="Сообщение коммита (Enter = 'Update'): "
    if "!COMMIT_MSG!"=="" set COMMIT_MSG=Update
    git commit -m "!COMMIT_MSG!"
    echo [OK] Коммит создан.
)
echo.

REM -------------------------------------------------------------------- #
REM  Push
REM -------------------------------------------------------------------- #
echo ============================================================
echo   Загрузка на GitHub
echo ============================================================
echo.
echo При первом push Git запросит логин и пароль.
echo.
echo   Username: ваш логин GitHub
echo   Password: Personal Access Token (НЕ обычный пароль!)
echo.
echo Если токена нет — создайте его:
echo   https://github.com/settings/tokens
echo   Scope: repo
echo.
pause

git push -u origin main

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Push не удался.
    echo.
    echo Частые причины:
    echo   1. Неверный логин или токен.
    echo   2. Токен без scope 'repo'.
    echo   3. Репозиторий на GitHub не создан.
    echo   4. В репозитории уже есть файлы (например, README).
    echo      В этом случае выполните:
    echo        git pull origin main --allow-unrelated-histories
    echo        git push -u origin main
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ГОТОВО
echo ============================================================
echo.
echo   Репозиторий: !REMOTE_URL!
echo.
echo   Откройте в браузере и убедитесь, что файлы загружены.
echo.

REM Убираем .git из URL для открытия в браузере
set WEB_URL=!REMOTE_URL:.git=!
start "" "!WEB_URL!"

endlocal
pause
exit /b 0

