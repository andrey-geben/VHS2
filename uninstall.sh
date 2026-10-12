#!/bin/bash
# ==================================================================== #
#  uninstall.sh — удаление VHS Core + VHS Analytics
# ==================================================================== #
#  Останавливает сервисы, удаляет unit-файлы.
#  НЕ удаляет данные (записи, БД, конфиги) — по умолчанию.
#  Для полного удаления: sudo bash uninstall.sh --purge
# ==================================================================== #

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    err "Запустите скрипт от root: sudo bash uninstall.sh"
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

info() { echo "[i] $1"; }

PURGE=false
if [ "$1" = "--purge" ]; then
    PURGE=true
fi

# Остановка сервисов
info "Останавливаю сервисы..."
systemctl stop vhs-analytics.service 2>/dev/null || true
systemctl stop vhs-core.service 2>/dev/null || true

# Отключение автозапуска
systemctl disable vhs-analytics.service 2>/dev/null || true
systemctl disable vhs-core.service 2>/dev/null || true

# Удаление unit-файлов
info "Удаляю unit-файлы..."
rm -f /etc/systemd/system/vhs-analytics.service
rm -f /etc/systemd/system/vhs-core.service
systemctl daemon-reload

log "Сервисы удалены."

# Опциональное удаление данных
if [ "$PURGE" = true ]; then
    warn "Режим --purge: удаляю данные, БД, записи и конфиги..."
    rm -rf "$SCRIPT_DIR/core/.venv"
    rm -rf "$SCRIPT_DIR/analytics/.venv"
    rm -rf "$SCRIPT_DIR/core/recordings"
    rm -rf "$SCRIPT_DIR/analytics/snapshots"
    rm -f  "$SCRIPT_DIR/core/surveillance.db"
    rm -f  "$SCRIPT_DIR/analytics/analytics.db"
    rm -f  "$SCRIPT_DIR/core/vhs.log"
    rm -f  "$SCRIPT_DIR/analytics/analytics.log"
    log "Данные удалены."
else
    info "Данные сохранены (для полного удаления: --purge)."
fi

echo
echo "Готово."