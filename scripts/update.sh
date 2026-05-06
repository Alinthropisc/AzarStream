#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  MediaFlow — Zero-Downtime Update Script
#  Usage: sudo bash scripts/update.sh
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
log_step()    { echo -e "\n${CYAN}━━━ $1 ━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYSTEMD_DIR="${SCRIPT_DIR}/systemd"
SYSTEMD_SYSTEM="/etc/systemd/system"

[[ $EUID -ne 0 ]] && log_error "Run with sudo: sudo bash scripts/update.sh"

set -a
source "${PROJECT_DIR}/.env"
set +a

SERVICE_USER="${SERVICE_USER:-$(logname 2>/dev/null || echo ${SUDO_USER:-root})}"
APP_WORKERS="${APP_WORKERS:-$(nproc 2>/dev/null || echo 2)}"

# ============================================================
log_step "Updating MediaFlow"
# ============================================================
echo -e "  ${BLUE}$(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "  ${BLUE}Branch: $(su - ${SERVICE_USER} -c "cd ${PROJECT_DIR} && git branch --show-current" 2>/dev/null || echo 'unknown')${NC}"
echo ""

# ============================================================
log_info "1. Pull latest code..."
# ============================================================
su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && git stash && git pull && git stash pop" 2>/dev/null \
    || su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && git pull"
log_success "Code updated"

# ============================================================
log_info "2. Update dependencies..."
# ============================================================
su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && .venv/bin/uv sync"
log_success "Dependencies updated"

# ============================================================
log_info "3. Run migrations..."
# ============================================================
su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && .venv/bin/alembic upgrade head"
log_success "Migrations applied"

# ============================================================
log_info "4. Update systemd units..."
# ============================================================

SERVICES=("mediaflow-web" "mediaflow-worker" "mediaflow-scheduler")

for SERVICE in "${SERVICES[@]}"; do
    SRC="${SYSTEMD_DIR}/${SERVICE}.service"
    DST="${SYSTEMD_SYSTEM}/${SERVICE}.service"
    if [[ -f "${SRC}" ]]; then
        envsubst '${PROJECT_DIR} ${SERVICE_USER} ${DOMAIN} ${APP_WORKERS}' < "${SRC}" > "${DST}"
        log_success "Updated ${SERVICE}"
    fi
done

# Telegram Bot API
if [[ -n "${TELEGRAM_API_ID:-}" && -n "${TELEGRAM_API_HASH:-}" ]]; then
    if [[ -f "${SYSTEMD_DIR}/telegram-bot-api.service" ]]; then
        envsubst '${PROJECT_DIR} ${SERVICE_USER} ${TELEGRAM_API_ID} ${TELEGRAM_API_HASH}' \
            < "${SYSTEMD_DIR}/telegram-bot-api.service" > "${SYSTEMD_SYSTEM}/telegram-bot-api.service"
        log_success "Updated telegram-bot-api"
    fi
fi

systemctl daemon-reload

# ============================================================
log_info "5. Restart services (zero-downtime)..."
# ============================================================

# Scheduler first (least critical)
systemctl reload-or-restart mediaflow-scheduler 2>/dev/null && log_success "scheduler" || log_warn "scheduler skipped"

# Worker
systemctl reload-or-restart mediaflow-worker 2>/dev/null && log_success "worker" || log_warn "worker skipped"

# Web — Granian supports graceful restart
sleep 1
systemctl reload-or-restart mediaflow-web 2>/dev/null && log_success "web" || log_warn "web skipped"

# Telegram Bot API (only if running — avoid restart unless necessary)
if systemctl is-active --quiet telegram-bot-api 2>/dev/null; then
    # Don't restart telegram-bot-api unless unit file changed
    log_info "telegram-bot-api: running (not restarted — stable)"
fi

# ============================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         Update Complete!                 ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

for SERVICE in telegram-bot-api mediaflow-web mediaflow-worker mediaflow-scheduler; do
    STATUS=$(systemctl is-active "${SERVICE}" 2>/dev/null || echo "—")
    if [[ "${STATUS}" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} ${SERVICE}: ${GREEN}${STATUS}${NC}"
    else
        echo -e "  ${YELLOW}●${NC} ${SERVICE}: ${YELLOW}${STATUS}${NC}"
    fi
done

echo ""
echo -e "  ${CYAN}Logs:${NC} journalctl -u mediaflow-web -f --since '5 min ago'"
echo ""
