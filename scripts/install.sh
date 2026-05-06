#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  MediaFlow — Production Install Script
#  Optimized for 30-50 bots (Dangerous Ultra Fast)
#  Usage: sudo bash scripts/install.sh
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()    { echo -e "\n${CYAN}━━━ $1 ━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYSTEMD_DIR="${SCRIPT_DIR}/systemd"
NGINX_DIR="${SCRIPT_DIR}/nginx"
SYSTEMD_SYSTEM="/etc/systemd/system"

[[ $EUID -ne 0 ]] && log_error "Run with sudo: sudo bash scripts/install.sh"

# ============================================================
log_step "1. System checks"
# ============================================================

command -v systemctl >/dev/null 2>&1 || log_error "systemd not found"

# Check dependencies
MISSING_DEPS=()
command -v postgresql >/dev/null 2>&1 || command -v psql >/dev/null 2>&1 || MISSING_DEPS+=("postgresql")
command -v redis-server >/dev/null 2>&1 || command -v redis-cli >/dev/null 2>&1 || MISSING_DEPS+=("redis")
command -v nginx >/dev/null 2>&1 || MISSING_DEPS+=("nginx")
command -v git >/dev/null 2>&1 || MISSING_DEPS+=("git")
command -v python3.12 >/dev/null 2>&1 || command -v python3.11 >/dev/null 2>&1 || MISSING_DEPS+=("python3.12+")
command -v curl >/dev/null 2>&1 || MISSING_DEPS+=("curl")

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    log_warn "Missing: ${MISSING_DEPS[*]}"
    read -p "Install missing packages? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq && apt-get install -y -qq "${MISSING_DEPS[@]}"
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y "${MISSING_DEPS[@]}"
        fi
        log_success "Dependencies installed"
    else
        log_error "Install missing packages first: ${MISSING_DEPS[*]}"
    fi
fi

[[ ! -f "${PROJECT_DIR}/.env" ]] && log_error ".env not found in ${PROJECT_DIR}"

# Ensure services are running
for SVC in postgresql redis-server redis; do
    systemctl is-active --quiet "$SVC" 2>/dev/null && log_success "$SVC running" || log_warn "$SVC not running"
done

# ============================================================
log_step "2. Read configuration"
# ============================================================

set -a
source "${PROJECT_DIR}/.env"
set +a

SERVICE_USER="${SERVICE_USER:-$(logname 2>/dev/null || echo ${SUDO_USER:-root})}"
DOMAIN="${DOMAIN:-localhost}"
APP_WORKERS="${APP_WORKERS:-$(nproc 2>/dev/null || echo 2)}"

log_info "PROJECT_DIR  = ${PROJECT_DIR}"
log_info "SERVICE_USER = ${SERVICE_USER}"
log_info "DOMAIN       = ${DOMAIN}"
log_info "WORKERS      = ${APP_WORKERS}"

# ============================================================
log_step "3. Create directories"
# ============================================================

mkdir -p "${PROJECT_DIR}/storage/logs"
mkdir -p "${PROJECT_DIR}/storage/temp"
mkdir -p "${PROJECT_DIR}/storage/telegram-bot-api"
mkdir -p "${PROJECT_DIR}/static"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}/storage" 2>/dev/null || true
chmod 750 "${PROJECT_DIR}/storage"

log_success "Directories created"

# ============================================================
log_step "4. Install Python dependencies"
# ============================================================

if [[ ! -f "${PROJECT_DIR}/.venv/bin/python" ]]; then
    log_info "Creating virtual environment..."
    su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && python3 -m venv .venv"
fi

log_info "Syncing dependencies with uv..."
su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && .venv/bin/uv sync --frozen"
log_success "Dependencies installed"

# ============================================================
log_step "5. Database migrations"
# ============================================================

log_info "Running Alembic migrations..."
su - "${SERVICE_USER}" -c "cd ${PROJECT_DIR} && .venv/bin/alembic upgrade head"
log_success "Migrations applied"

# ============================================================
log_step "6. Install systemd services"
# ============================================================

SERVICES=("mediaflow-web" "mediaflow-worker" "mediaflow-scheduler")

for SERVICE in "${SERVICES[@]}"; do
    SRC="${SYSTEMD_DIR}/${SERVICE}.service"
    DST="${SYSTEMD_SYSTEM}/${SERVICE}.service"

    if [[ ! -f "${SRC}" ]]; then
        log_warn "${SRC} not found — skipping"
        continue
    fi

    envsubst '${PROJECT_DIR} ${SERVICE_USER} ${DOMAIN} ${APP_WORKERS}' < "${SRC}" > "${DST}"
    chmod 644 "${DST}"
    log_success "Installed ${SERVICE}.service"
done

# Telegram Bot API (optional — only if configured)
if [[ -n "${TELEGRAM_API_ID:-}" && -n "${TELEGRAM_API_HASH:-}" ]]; then
    if [[ -f "${SYSTEMD_DIR}/telegram-bot-api.service" ]]; then
        envsubst '${PROJECT_DIR} ${SERVICE_USER} ${TELEGRAM_API_ID} ${TELEGRAM_API_HASH}' \
            < "${SYSTEMD_DIR}/telegram-bot-api.service" > "${SYSTEMD_SYSTEM}/telegram-bot-api.service"
        chmod 644 "${SYSTEMD_SYSTEM}/telegram-bot-api.service"
        log_success "Installed telegram-bot-api.service"
    fi
else
    log_warn "TELEGRAM_API_ID/HASH not set — skipping telegram-bot-api"
fi

# ============================================================
log_step "7. Configure nginx"
# ============================================================

if command -v nginx >/dev/null 2>&1; then
    NGINX_DST="/etc/nginx/sites-available/mediaflow.conf"
    NGINX_ENABLED="/etc/nginx/sites-enabled/mediaflow.conf"

    envsubst '${PROJECT_DIR} ${DOMAIN}' < "${NGINX_DIR}/mediaflow.conf" > "${NGINX_DST}"
    ln -sf "${NGINX_DST}" "${NGINX_ENABLED}"

    if nginx -t 2>/dev/null; then
        systemctl reload nginx
        log_success "Nginx configured"
    else
        log_warn "Nginx config test failed — check manually"
    fi
else
    log_warn "Nginx not installed — skipping"
fi

# ============================================================
log_step "8. Setup logrotate"
# ============================================================

LOGROTATE_CONF="/etc/logrotate.d/mediaflow"
cat > "${LOGROTATE_CONF}" << EOF
${PROJECT_DIR}/storage/logs/*.log {
    daily
    rotate 3
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ${SERVICE_USER} ${SERVICE_USER}
    sharedscripts
    postrotate
        systemctl reload-or-restart mediaflow-web >/dev/null 2>&1 || true
    endscript
}
EOF
log_success "Logrotate configured (3 days retention)"

# ============================================================
log_step "9. Enable and start services"
# ============================================================

systemctl daemon-reload

# Order: DB → Redis → Bot API → Web → Worker → Scheduler
log_info "Starting services..."

# Telegram Bot API first (if installed)
if systemctl list-unit-files | grep -q telegram-bot-api; then
    systemctl enable telegram-bot-api 2>/dev/null || true
    systemctl restart telegram-bot-api
    sleep 2
    log_success "telegram-bot-api"
fi

# Web server
systemctl enable mediaflow-web
systemctl restart mediaflow-web
sleep 2
log_success "mediaflow-web"

# Worker
systemctl enable mediaflow-worker
systemctl restart mediaflow-worker
log_success "mediaflow-worker"

# Scheduler
systemctl enable mediaflow-scheduler
systemctl restart mediaflow-scheduler
log_success "mediaflow-scheduler"

# ============================================================
log_step "Status"
# ============================================================

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     MediaFlow Installed Successfully!    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

ALL_OK=true
for SERVICE in telegram-bot-api mediaflow-web mediaflow-worker mediaflow-scheduler; do
    STATUS=$(systemctl is-active "${SERVICE}" 2>/dev/null || echo "not-installed")
    if [[ "${STATUS}" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} ${SERVICE}: ${GREEN}${STATUS}${NC}"
    else
        echo -e "  ${RED}●${NC} ${SERVICE}: ${RED}${STATUS}${NC}"
        ALL_OK=false
    fi
done

echo ""
echo -e "  ${CYAN}Logs:${NC}      journalctl -u mediaflow-web -f"
echo -e "  ${CYAN}Status:${NC}    systemctl status mediaflow-web"
echo -e "  ${CYAN}URL:${NC}       http${DOMAIN:+s}://${DOMAIN:-localhost}"
echo ""

if $ALL_OK; then
    echo -e "  ${GREEN}All services running! 🚀${NC}"
else
    echo -e "  ${YELLOW}Some services need attention. Check logs above.${NC}"
fi
echo ""
