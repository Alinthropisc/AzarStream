#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }

[[ $EUID -ne 0 ]] && { echo -e "${RED}Run with sudo${NC}"; exit 1; }

SERVICES=(
    "mediaflow-scheduler"
    "mediaflow-worker"
    "mediaflow-web"
    "telegram-bot-api"
)

log_info "Stopping services..."
for SERVICE in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
        systemctl stop "${SERVICE}"
        log_success "Stopped ${SERVICE}"
    fi

    if systemctl is-enabled --quiet "${SERVICE}" 2>/dev/null; then
        systemctl disable "${SERVICE}"
        log_success "Disabled ${SERVICE}"
    fi

    if [[ -f "/etc/systemd/system/${SERVICE}.service" ]]; then
        rm -f "/etc/systemd/system/${SERVICE}.service"
        log_success "Removed ${SERVICE}.service"
    fi
done

systemctl daemon-reload
log_success "systemd reloaded"

# Nginx
if [[ -f "/etc/nginx/sites-enabled/mediaflow.conf" ]]; then
    rm -f "/etc/nginx/sites-enabled/mediaflow.conf"
    rm -f "/etc/nginx/sites-available/mediaflow.conf"
    nginx -t 2>/dev/null && systemctl reload nginx
    log_success "Nginx config removed"
fi

# Logrotate
rm -f /etc/logrotate.d/mediaflow
log_success "Logrotate config removed"

echo ""
echo -e "${GREEN}MediaFlow services removed${NC}"
echo -e "${YELLOW}Data preserved: storage/, .env, database${NC}"
echo -e "${YELLOW}To remove all data: rm -rf /path/to/MediaFlow${NC}"
echo ""
