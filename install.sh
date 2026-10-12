#!/bin/bash
# ==================================================================== #
#  install.sh — установка VHS Core + VHS Analytics на Ubuntu Server
# ==================================================================== #
#  Что делает:
#    1. Проверяет права root и версию Ubuntu
#    2. Устанавливает системные пакеты (Python, ffmpeg, OpenCV-зависимости)
#    3. Создаёт пользователя vhs
#    4. Создаёт venv для core и analytics
#    5. Устанавливает pip-зависимости
#    6. Регистрирует systemd-сервисы
#    7. Запускает сервисы
# ==================================================================== #

set -e  # остановка при любой ошибке

# --- Цветной вывод ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

# -------------------------------------------------------------------- #
#  Проверка: запущено от root?
# -------------------------------------------------------------------- #
if [ "$EUID" -ne 0 ]; then
    err "Запустите скрипт от root: sudo bash install.sh"
    exit 1
fi

# -------------------------------------------------------------------- #
#  Проверка: мы в нужной директории?
# -------------------------------------------------------------------- #
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d "core" ] || [ ! -d "analytics" ]; then
    err "Не найдены папки core/ и analytics/ в $SCRIPT_DIR"
    err "Положите install.sh рядом с папками core и analytics."
    exit 1
fi

info "Каталог установки: $SCRIPT_DIR"

# -------------------------------------------------------------------- #
#  Определение версии Ubuntu
# -------------------------------------------------------------------- #
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    OS_VER=$VERSION_ID
else
    err "Не удалось определить ОС. Ожидается Ubuntu."
    exit 1
fi

info "ОС: $OS $OS_VER"

if [ "$OS" != "ubuntu" ] && [ "$OS" != "debian" ]; then
    warn "Скрипт тестировался на Ubuntu. Ваша ОС ($OS) может работать,"
    warn "но возможны отклонения."
    sleep 3
fi

# -------------------------------------------------------------------- #
#  Установка Python 3.12 (если нужен)
# -------------------------------------------------------------------- #
info "Проверка Python 3.12..."
if ! command -v python3.12 &> /dev/null; then
    warn "Python 3.12 не найден, устанавливаю..."

    if [ "$OS_VER" = "24.04" ]; then
        # Ubuntu 24.04 — 3.12 в стандартных репозиториях
        apt-get update -qq
        apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
    elif [ "$OS_VER" = "22.04" ]; then
        # Ubuntu 22.04 — надо добавить PPA deadsnakes
        warn "Добавляю PPA deadsnakes для Python 3.12..."
        apt-get update -qq
        apt-get install -y -qq software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -qq
        apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
    else
        warn "Версия Ubuntu $OS_VER не тестировалась. Пробую стандартные пакеты."
        apt-get update -qq
        apt-get install -y -qq python3.12 python3.12-venv python3.12-dev || {
            err "Не удалось установить Python 3.12. Установите вручную и повторите."
            exit 1
        }
    fi
fi

PY_VER=$(python3.12 --version 2>&1)
log "Python: $PY_VER"

# -------------------------------------------------------------------- #
#  Системные пакеты
# -------------------------------------------------------------------- #
info "Устанавливаю системные пакеты..."
apt-get install -y -qq \
    ffmpeg \
    curl \
    wget \
    git \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    v4l-utils \
    > /dev/null

log "Системные пакеты установлены."

# -------------------------------------------------------------------- #
#  Пользователь vhs
# -------------------------------------------------------------------- #
if ! id -u vhs &> /dev/null; then
    info "Создаю пользователя vhs..."
    useradd -r -s /bin/false -d "$SCRIPT_DIR" -M vhs
    log "Пользователь vhs создан."
else
    info "Пользователь vhs уже существует."
fi

# -------------------------------------------------------------------- #
#  Права на папку установки
# -------------------------------------------------------------------- #
info "Настраиваю права доступа..."
chown -R vhs:vhs "$SCRIPT_DIR"
chmod -R 755 "$SCRIPT_DIR"
log "Права настроены."

# -------------------------------------------------------------------- #
#  venv для Core
# -------------------------------------------------------------------- #
info "Создаю venv для VHS Core..."
cd "$SCRIPT_DIR/core"

if [ ! -d ".venv" ]; then
    sudo -u vhs python3.12 -m venv .venv
fi

sudo -u vhs .venv/bin/pip install --upgrade pip --quiet
info "Устанавливаю зависимости Core (может занять 2-3 минуты)..."

if [ -f "requirements.txt" ]; then
    sudo -u vhs .venv/bin/pip install -r requirements.txt --quiet
else
    warn "Не найден core/requirements.txt — устанавливаю базовый набор."
    sudo -u vhs .venv/bin/pip install \
        Flask==3.0.3 \
        Flask-Login==0.6.3 \
        Werkzeug==3.0.3 \
        opencv-python==4.10.0.84 \
        PyYAML==6.0.1 \
        requests==2.32.3 \
        python-dateutil==2.9.0.post0 \
        --quiet
fi

log "Зависимости Core установлены."

# -------------------------------------------------------------------- #
#  venv для Analytics
# -------------------------------------------------------------------- #
info "Создаю venv для VHS Analytics..."
cd "$SCRIPT_DIR/analytics"

if [ ! -d ".venv" ]; then
    sudo -u vhs python3.12 -m venv .venv
fi

sudo -u vhs .venv/bin/pip install --upgrade pip --quiet
info "Устанавливаю зависимости Analytics (может занять 3-5 минут)..."

if [ -f "requirements.txt" ]; then
    sudo -u vhs .venv/bin/pip install -r requirements.txt --quiet
else
    warn "Не найден analytics/requirements.txt — устанавливаю базовый набор."
    sudo -u vhs .venv/bin/pip install \
        Flask==3.0.3 \
        Flask-Login==0.6.3 \
        Werkzeug==3.0.3 \
        opencv-python==4.10.0.84 \
        PyYAML==6.0.1 \
        requests==2.32.3 \
        numpy>=2.0 \
        rapidocr-onnxruntime \
        fast-alpr[onnx] \
        onnxruntime \
        --quiet
fi

log "Зависимости Analytics установлены."

# -------------------------------------------------------------------- #
#  Проверка конфигов
# -------------------------------------------------------------------- #
cd "$SCRIPT_DIR"

if [ ! -f "core/config.yaml" ]; then
    warn "Не найден core/config.yaml — сервис не запустится без него!"
fi
if [ ! -f "analytics/config.yaml" ]; then
    warn "Не найден analytics/config.yaml — сервис не запустится без него!"
fi

# Проверка совпадения токена
if [ -f "core/config.yaml" ] && [ -f "analytics/config.yaml" ]; then
    CORE_TOKEN=$(grep -A2 "^analytics:" core/config.yaml | grep "api_token:" | head -1 | sed 's/.*api_token:\s*//' | tr -d '"' | tr -d "'")
    ANALYTICS_TOKEN=$(grep -A3 "^vhs_core:" analytics/config.yaml | grep "api_token:" | head -1 | sed 's/.*api_token:\s*//' | tr -d '"' | tr -d "'")

    if [ "$CORE_TOKEN" != "$ANALYTICS_TOKEN" ]; then
        warn "ВНИМАНИЕ: токены в core/config.yaml и analytics/config.yaml НЕ СОВПАДАЮТ!"
        warn "  Core:      $CORE_TOKEN"
        warn "  Analytics: $ANALYTICS_TOKEN"
        warn "Исправьте перед запуском."
    else
        log "Токены Core и Analytics совпадают."
    fi
fi

# -------------------------------------------------------------------- #
#  Systemd-сервисы
# -------------------------------------------------------------------- #
info "Регистрирую systemd-сервисы..."

# Пути подставляем динамически
cat > /etc/systemd/system/vhs-core.service << EOF
[Unit]
Description=VHS Core — сервер видеонаблюдения (запись, трансляция, архив)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vhs
Group=vhs
WorkingDirectory=$SCRIPT_DIR/core
ExecStart=$SCRIPT_DIR/core/.venv/bin/python $SCRIPT_DIR/core/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/vhs-analytics.service << EOF
[Unit]
Description=VHS Analytics — детекция объектов и номеров
After=network-online.target vhs-core.service
Wants=network-online.target
Requires=vhs-core.service

[Service]
Type=simple
User=vhs
Group=vhs
WorkingDirectory=$SCRIPT_DIR/analytics
ExecStart=$SCRIPT_DIR/analytics/.venv/bin/python $SCRIPT_DIR/analytics/analytics_app.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
log "Сервисы зарегистрированы."

# -------------------------------------------------------------------- #
#  Открытие портов (если ufw активен)
# -------------------------------------------------------------------- #
if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
    info "Открываю порты в ufw..."
    ufw allow 8000/tcp comment "VHS Core"
    ufw allow 8100/tcp comment "VHS Analytics"
    log "Порты 8000 и 8100 открыты."
fi

# -------------------------------------------------------------------- #
#  Запуск
# -------------------------------------------------------------------- #
info "Запускаю сервисы..."
systemctl enable vhs-core.service
systemctl enable vhs-analytics.service

systemctl start vhs-core.service
sleep 3
systemctl start vhs-analytics.service

sleep 3

# -------------------------------------------------------------------- #
#  Итог
# -------------------------------------------------------------------- #
echo
echo "============================================================"
echo -e "  ${GREEN}УСТАНОВКА ЗАВЕРШЕНА${NC}"
echo "============================================================"
echo
echo "  Статус сервисов:"
echo -n "    vhs-core:      "
systemctl is-active vhs-core.service || true
echo -n "    vhs-analytics: "
systemctl is-active vhs-analytics.service || true
echo
echo "  Адреса в браузере:"
echo "    VHS Core:       http://$(hostname -I | awk '{print $1}'):8000"
echo "    VHS Analytics:  http://$(hostname -I | awk '{print $1}'):8100"
echo
echo "  Логин по умолчанию: admin / admin"
echo "  СМЕНИТЕ ПАРОЛЬ после первого входа!"
echo
echo "  Управление сервисами:"
echo "    sudo systemctl status vhs-core"
echo "    sudo systemctl restart vhs-core"
echo "    sudo journalctl -u vhs-core -f"
echo
echo "  Конфиги:"
echo "    $SCRIPT_DIR/core/config.yaml"
echo "    $SCRIPT_DIR/analytics/config.yaml"
echo